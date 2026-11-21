#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  shield_service.py
#
# Turns the upstate SHIELD's Tailscale exit node on/off over adb, driven by MQTT (gergy-incidents#1893):
#   upstate/shield/<slug>/set           {"id","command":"turn_on"|"turn_off","ts"} in, not retained
#   upstate/shield/<slug>/result        outcome JSON out, not retained
#   upstate/shield/<slug>/state         retained {"exit_node": true|false|null, "online": bool, "ts"}
#   upstate/shield/<slug>/availability  online | offline (LWT)

import asyncio
import json
import logging
import re
import shlex
import signal
import sys
import threading
import time

import paho.mqtt.client as mqtt

from config import load_config, ConfigError

TOPIC_ROOT = "upstate/shield"
STATE_INTERVAL_SECONDS = 180
ADB_TIMEOUT_SECONDS = 8
# After a broadcast the route flips asynchronously; poll the route this long before calling the command unconfirmed.
VERIFY_SECONDS = 10
VERIFY_POLL_SECONDS = 1
# Same window as the ecobee commands so HA has one timing contract for both.
COMMAND_WINDOW_SECONDS = 20
MAX_FUTURE_SKEW_SECONDS = 5
RESULT_CACHE_SECONDS = 600
COMMAND_DRAIN_SECONDS = 5
CLOSE_PUBLISH_WAIT_SECONDS = 3
RESULT_SEND_ATTEMPTS = 5
RESULT_RETRY_SECONDS = 0.5
# Any address outside the LAN works; the route to it is what shows whether the exit node is carrying traffic.
PROBE_ADDRESS = "1.1.1.1"

OK, REJECTED, FAILED, UNKNOWN = "ok", "rejected", "failed", "unknown"
PROBE = object()

def _topic(slug, leaf):
	return "%s/%s/%s" % (TOPIC_ROOT, slug, leaf)

# Tailscale's VPN interface is a tun device; with the exit node off the route goes out the LAN interface.
# None when the output is unreadable: unknown must never be reported as "off".
def parse_route(output):
	if not isinstance(output, str):
		return None
	match = re.search(r"\bdev\s+(\S+)", output)
	if not match:
		return None
	return match.group(1).startswith("tun")

def build_state(exit_node, online, ts):
	return {"exit_node": exit_node, "online": online, "ts": int(ts)}

def _result(cmd_id, outcome, error=None, exit_node=None):
	return {"id": cmd_id, "outcome": outcome, "error": error, "exit_node": exit_node}

class AdbError(Exception):
	pass

class Adb:
	def __init__(self, target, exit_node_name):
		self.target = target
		self.exit_node_name = exit_node_name

	# deadline is an absolute time.monotonic() value. Each call gets min(ADB_TIMEOUT, time left), so a connect-and-retry
	# sequence can never add up to more than the caller's budget.
	async def _run(self, *args, deadline=None):
		timeout = ADB_TIMEOUT_SECONDS
		if deadline is not None:
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				raise AdbError("adb %s not run: deadline passed" % args[0])
			timeout = min(timeout, remaining)
		try:
			proc = await asyncio.create_subprocess_exec(
				"adb", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
		except OSError as error:
			raise AdbError("adb failed to run: %r" % (error,))
		try:
			out, _ = await asyncio.wait_for(proc.communicate(), timeout)
		except asyncio.TimeoutError:
			proc.kill()
			await proc.wait()
			raise AdbError("adb %s timed out" % args[0])
		except asyncio.CancelledError:
			proc.kill()
			raise
		text = out.decode("utf-8", "replace")
		if proc.returncode != 0:
			raise AdbError("adb exited %s: %s" % (proc.returncode, text.strip()[:200]))
		return text

	# adb connect exits 0 even when it fails, so judge by its text. Needed after every SHIELD or Pi reboot.
	async def connect(self, deadline=None):
		text = await self._run("connect", self.target, deadline=deadline)
		if "connected to" not in text:
			raise AdbError("adb connect: %s" % text.strip()[:200])

	# The deadline is rechecked before the resend, so a retry after a slow reconnect cannot run a command that has since expired.
	async def _shell(self, command, deadline=None):
		try:
			return await self._run("-s", self.target, "shell", command, deadline=deadline)
		except AdbError:
			await self.connect(deadline)
			return await self._run("-s", self.target, "shell", command, deadline=deadline)

	async def route_uses_exit_node(self, deadline=None):
		return parse_route(await self._shell("ip route get %s" % PROBE_ADDRESS, deadline))

	# adb joins shell args into one remote command line, so the empty value for "no exit node" has to be quoted into it
	# or the remote shell drops it.
	async def broadcast(self, node, deadline=None):
		command = "am broadcast -a com.tailscale.ipn.USE_EXIT_NODE -n com.tailscale.ipn/.IPNReceiver --es exitNode %s --ez allowLanAccess true" % shlex.quote(node)
		text = await self._shell(command, deadline)
		# am exits 0 even when nothing received the broadcast.
		if "Broadcast completed" not in text:
			raise AdbError("broadcast not delivered: %s" % text.strip()[:200])

# Mirrors ecobee_service.MqttPublisher's policy: latest-value only, nothing queued while the broker is down.
class MqttPublisher:
	def __init__(self, client_id, host, port, slug):
		self.slug = slug
		self._lock = threading.Lock()
		self._connected = False
		self._latest_state = None
		self._latest_online = None
		self._command_handler = None
		self.client = mqtt.Client(client_id)
		self.client.will_set(_topic(slug, "availability"), "offline", qos=1, retain=True)
		self.client.on_connect = self._on_connect
		self.client.on_disconnect = self._on_disconnect
		self.client.on_message = self._on_message
		self.client.max_queued_messages_set(4)
		self.client.max_inflight_messages_set(4)
		# connect_async + loop_start retries on its own, so a broker/DNS outage at boot doesn't kill the service.
		self.client.reconnect_delay_set(min_delay=1, max_delay=60)
		self.client.connect_async(host, port)
		self.client.loop_start()

	def _on_connect(self, client, userdata, flags, rcode):
		if rcode != 0:
			logging.warning("ShieldMQTT:connect refused rcode=%s", rcode)
			return
		with self._lock:
			self._connected = True
			if self._latest_state is not None:
				self._send(_topic(self.slug, "state"), self._latest_state)
			if self._latest_online is not None:
				self._send(_topic(self.slug, "availability"), "online" if self._latest_online else "offline")
			# Clean session: the broker forgets the subscription on every drop.
			if self._command_handler is not None:
				self.client.subscribe(_topic(self.slug, "set"), qos=1)

	def set_command_handler(self, handler):
		with self._lock:
			self._command_handler = handler
			if self._connected:
				self.client.subscribe(_topic(self.slug, "set"), qos=1)

	def _on_message(self, client, userdata, message):
		# A retained command is a replay of something already acted on or long expired.
		if message.retain or self._command_handler is None:
			return
		try:
			self._command_handler(message.payload)
		except Exception:
			logging.exception("ShieldMQTT:command handler failed")

	def _on_disconnect(self, client, userdata, rcode):
		with self._lock:
			self._connected = False

	def _send(self, topic, payload):
		info = self.client.publish(topic, payload, qos=1, retain=True)
		if info.rc != mqtt.MQTT_ERR_SUCCESS:
			logging.warning("ShieldMQTT:publish to %s returned rc=%s", topic, info.rc)
		return info

	# Not retained: a stale result would answer the wrong future command. Dropped while disconnected; HA times out to UNKNOWN.
	# Returns False when it was not queued (broker down or client queue full) so the caller can retry.
	def publish_result(self, result):
		with self._lock:
			if not self._connected:
				logging.warning("ShieldMQTT:broker down, result for %s not sent", result.get("id"))
				return False
			info = self.client.publish(_topic(self.slug, "result"), json.dumps(result), qos=1, retain=False)
			if info.rc != mqtt.MQTT_ERR_SUCCESS:
				logging.warning("ShieldMQTT:result publish returned rc=%s", info.rc)
				return False
			return True

	def publish_state(self, state):
		payload = json.dumps(state)
		with self._lock:
			self._latest_state = payload
			if self._connected:
				return self._send(_topic(self.slug, "state"), payload).rc == mqtt.MQTT_ERR_SUCCESS
			# Not connected: _on_connect sends the latest value, so nothing is lost.
			return True

	def publish_availability(self, online):
		with self._lock:
			self._latest_online = online
			if self._connected:
				return self._send(_topic(self.slug, "availability"), "online" if online else "offline").rc == mqtt.MQTT_ERR_SUCCESS
			return True

	def begin_close(self):
		with self._lock:
			self._latest_online = False
			if not self._connected:
				return None
			return self._send(_topic(self.slug, "availability"), "offline")

	def finish_close(self):
		self.client.disconnect()
		self.client.loop_stop()

def close_publisher(publisher, wait=CLOSE_PUBLISH_WAIT_SECONDS):
	info = publisher.begin_close()
	if info is not None:
		try:
			info.wait_for_publish(timeout=wait)
		except (ValueError, RuntimeError):
			pass
	publisher.finish_close()

class Shield:
	def __init__(self, adb, publisher, clock=time.time, monotonic=time.monotonic, verify_seconds=None, verify_poll=None):
		self.adb = adb
		self.publisher = publisher
		self.clock = clock
		self.monotonic = monotonic
		self.verify_seconds = VERIFY_SECONDS if verify_seconds is None else verify_seconds
		self.verify_poll = VERIFY_POLL_SECONDS if verify_poll is None else verify_poll
		self.closing = False
		self._last = object()
		# One adb conversation at a time: a poll racing a broadcast could publish the pre-change route as current.
		self._lock = asyncio.Lock()
		self._results = {}
		self._inflight = set()

	# -- state --

	async def read_state(self):
		try:
			exit_node = await self.adb.route_uses_exit_node()
			online = True
		except AdbError as error:
			logging.warning("Shield:route check failed: %s", error)
			exit_node, online = None, False
		return exit_node, online

	def _publish(self, exit_node, online, force=False):
		key = (exit_node, online)
		if force or key != self._last:
			# A refused publish leaves _last unset so the next poll sends it again instead of waiting for the forced refresh.
			if self.publisher.publish_state(build_state(exit_node, online, self.clock())) is not False:
				self._last = key
			else:
				self._last = object()
		self.publisher.publish_availability(online)

	async def poll_once(self, force=False):
		async with self._lock:
			exit_node, online = await self.read_state()
			self._publish(exit_node, online, force)
			return exit_node, online

	# Publishes on change at once and refreshes the timestamp every interval so HA can tell a live Pi from a dead one.
	async def run(self, stop, interval=STATE_INTERVAL_SECONDS, check_every=15):
		last_forced = None
		while not stop.is_set():
			now = self.monotonic()
			force = last_forced is None or now - last_forced >= interval
			if force:
				last_forced = now
			poll = asyncio.ensure_future(self.poll_once(force))
			stopped = asyncio.ensure_future(stop.wait())
			try:
				await asyncio.wait({poll, stopped}, return_when=asyncio.FIRST_COMPLETED)
			finally:
				stopped.cancel()
				if not poll.done():
					poll.cancel()
					await asyncio.gather(poll, return_exceptions=True)
			try:
				await asyncio.wait_for(stop.wait(), check_every)
			except asyncio.TimeoutError:
				pass

	# -- commands --

	async def handle(self, payload):
		task = asyncio.current_task()
		self._inflight.add(task)
		try:
			try:
				result, sent = await self._handle(payload)
			except asyncio.CancelledError:
				raise
			except Exception as error:
				logging.exception("Shield:command crashed")
				result, sent = _result(None, UNKNOWN, "internal error: %r" % (error,)), False
			if not sent:
				await self._send_result(result)
		finally:
			self._inflight.discard(task)

	# The result is what the HA caller is waiting on, so it goes out before any state and is retried if the client queue
	# is momentarily full of unacknowledged state publishes.
	async def _send_result(self, result):
		for attempt in range(RESULT_SEND_ATTEMPTS):
			if self.publisher.publish_result(result) is not False:
				return True
			if attempt + 1 < RESULT_SEND_ATTEMPTS:
				await asyncio.sleep(RESULT_RETRY_SECONDS)
		logging.error("Shield:result for %s could not be published", result.get("id"))
		return False

	async def drain(self, timeout):
		pending = set(self._inflight)
		if not pending:
			return
		_, still = await asyncio.wait(pending, timeout=timeout)
		for task in still:
			task.cancel()
		if still:
			await asyncio.wait(still, timeout=1)

	def _purge(self):
		now = self.monotonic()
		for key in [k for k, (expiry, _) in self._results.items() if expiry <= now]:
			del self._results[key]

	def _check_ts(self, ts):
		if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts != ts or ts in (float("inf"), float("-inf")):
			return "ts must be a number"
		now = self.clock()
		if ts - now > MAX_FUTURE_SKEW_SECONDS:
			return "ts is %.0f s in the future (clock skew)" % (ts - now)
		if now > ts + COMMAND_WINDOW_SECONDS:
			return "command expired (%.0f s old, limit %d s)" % (now - ts, COMMAND_WINDOW_SECONDS)
		return None

	async def _handle(self, payload):
		try:
			command = json.loads(payload)
			if not isinstance(command, dict):
				raise ValueError("not an object")
		except (ValueError, TypeError):
			return _result(None, REJECTED, "command is not a JSON object"), False
		cmd_id = command.get("id")
		if not isinstance(cmd_id, str) or not cmd_id:
			return _result(None, REJECTED, "missing command id"), False

		# Under the lock so a redelivery arriving mid-flight waits and then gets the cached answer instead of broadcasting again.
		async with self._lock:
			self._purge()
			cached = self._results.get(cmd_id)
			if cached:
				return cached[1], False
			if self.closing:
				return _result(cmd_id, REJECTED, "service is stopping"), False
			name = command.get("command")
			observed = None
			if name not in ("turn_on", "turn_off"):
				result = _result(cmd_id, REJECTED, "unknown command %r" % (name,))
			else:
				ts = command.get("ts")
				error = self._check_ts(ts)
				if error:
					result = _result(cmd_id, REJECTED, error)
				else:
					# Wall-clock expiry converted once, so the adb layer can enforce it on monotonic time.
					expires = self.monotonic() + (ts + COMMAND_WINDOW_SECONDS - self.clock())
					result, observed = await self._apply(cmd_id, name == "turn_on", expires)
			self._results[cmd_id] = (self.monotonic() + RESULT_CACHE_SECONDS, result)
			sent = False
			if observed is not None:
				# Result first, then state, so queued state can never crowd the answer out.
				sent = await self._send_result(result)
				if observed is PROBE:
					observed = (None, await self._reachable())
				self._publish(observed[0], observed[1], force=True)
			return result, sent

	# After a failed broadcast the SHIELD may or may not have changed, so the route is unknown, but whether adb can still
	# reach it is knowable and decides availability.
	async def _reachable(self):
		try:
			await self.adb.route_uses_exit_node(deadline=self.monotonic() + ADB_TIMEOUT_SECONDS)
			return True
		except AdbError:
			return False

	# Returns (result, observed): observed is (exit_node, online) to publish after the result, PROBE to publish unknown
	# with probed reachability, or None when nothing was attempted.
	async def _apply(self, cmd_id, want, expires):
		try:
			await self.adb.broadcast(self.adb.exit_node_name if want else "", deadline=expires)
		except AdbError as error:
			# A timeout may still have delivered the broadcast, so only a clean refusal is "failed".
			logging.warning("Shield:broadcast failed: %s", error)
			return _result(cmd_id, UNKNOWN, str(error)), PROBE
		# One budget for every read, including each read's own reconnect and retry.
		deadline = self.monotonic() + self.verify_seconds
		actual = None
		while True:
			try:
				actual = await self.adb.route_uses_exit_node(deadline=deadline)
			except AdbError as error:
				logging.warning("Shield:verify route check failed: %s", error)
				actual = None
			if actual is want or self.monotonic() >= deadline:
				break
			await asyncio.sleep(min(self.verify_poll, max(0, deadline - self.monotonic())))
		observed = (actual, actual is not None)
		if actual is want:
			return _result(cmd_id, OK, None, actual), observed
		if actual is None:
			return _result(cmd_id, UNKNOWN, "broadcast sent but the route could not be read back"), observed
		return _result(cmd_id, FAILED, "broadcast sent but the route did not switch within %d s" % self.verify_seconds, actual), observed

def _dispatch(loop, shield):
	def handler(payload):
		try:
			asyncio.run_coroutine_threadsafe(shield.handle(payload), loop)
		except RuntimeError:
			logging.warning("Shield:loop closed, command dropped")
	return handler

async def run_service(cfg, stop):
	slug = cfg["shield_slug"]
	publisher = MqttPublisher("%s-shield-%s" % (cfg["client_id"], slug), cfg["broker_host"], cfg["broker_port"], slug)
	shield = Shield(Adb(cfg["shield_adb"], cfg["shield_exit_node"]), publisher)
	publisher.set_command_handler(_dispatch(asyncio.get_running_loop(), shield))
	try:
		await shield.run(stop)
	finally:
		shield.closing = True
		await shield.drain(COMMAND_DRAIN_SECONDS)
		close_publisher(publisher)

async def _main():
	stop = asyncio.Event()
	loop = asyncio.get_running_loop()
	for sig in (signal.SIGTERM, signal.SIGINT):
		loop.add_signal_handler(sig, stop.set)
	await run_service(load_config(), stop)

def main():
	logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
	try:
		asyncio.run(_main())
	except ConfigError as error:
		logging.critical("%s", error)
		return 1
	return 0

if __name__ == "__main__":
	sys.exit(main())
