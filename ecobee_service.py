#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  ecobee_service.py
#
# Reads paired ecobee thermostats over HomeKit (read-only) and publishes retained JSON to the home MQTT broker:
#   upstate/ecobee/<device_id>/state         JSON, see ecobee_state.build_state
#   upstate/ecobee/<device_id>/availability  online | offline (LWT)

import asyncio
import json
import logging
import signal
import sys
import threading
import time

import paho.mqtt.client as mqtt

from config import load_config, ConfigError
from ecobee_hk import open_controller
from ecobee_state import discover_layout, read_ids, build_state, thermostat_answered
from ecobee_store import pairings_path, load_pairings

TOPIC_ROOT = "upstate/ecobee"
POLL_INTERVAL_SECONDS = 60
# Bounds one HomeKit read so a wedged connection can't stall the loop past the next poll.
READ_TIMEOUT_SECONDS = 20
# One missed poll is routine on Wi-Fi; two in a row means the thermostat is really gone.
OFFLINE_AFTER_FAILURES = 2
# aiohomekit replaces pairing.accessories when the ecobee advertises a new configuration, but a characteristic
# that vanished only shows up as a HAP status, so also re-learn the tree now and then.
LAYOUT_REFRESH_SECONDS = 600
# systemd gives us 15 s (TimeoutStopSec) to stop; the offline publication must land inside it even if a read hangs.
CLOSE_PUBLISH_WAIT_SECONDS = 3

def state_topic(device_id):
	return "%s/%s/state" % (TOPIC_ROOT, device_id)

def availability_topic(device_id):
	return "%s/%s/availability" % (TOPIC_ROOT, device_id)

# The only pairing methods the poller may reach. Anything that mutates the thermostat or its pairings
# (put_characteristics, add/remove_pairing, identify, ...) is deliberately absent, so a stray call raises AttributeError.
class ReadOnlyPairing:
	def __init__(self, pairing):
		self._pairing = pairing

	@property
	def accessories(self):
		return self._pairing.accessories

	async def list_accessories_and_characteristics(self):
		return await self._pairing.list_accessories_and_characteristics()

	async def get_characteristics(self, characteristics):
		return await self._pairing.get_characteristics(characteristics)

	async def close(self):
		await self._pairing.close()

# One MQTT connection per thermostat: an LWT is per-connection, and each thermostat needs its own availability topic.
# Only the latest state and availability are kept: while the broker is unreachable nothing is handed to paho (its
# queue is unbounded and would replay hours of stale retained states), and on connect the newest of each is sent once.
class MqttPublisher:
	def __init__(self, client_id, host, port, device_id):
		self.device_id = device_id
		self._lock = threading.Lock()
		self._connected = False
		self._latest_state = None
		self._latest_online = None
		self.client = mqtt.Client(client_id)
		self.client.will_set(availability_topic(device_id), "offline", qos=1, retain=True)
		self.client.on_connect = self._on_connect
		self.client.on_disconnect = self._on_disconnect
		# Backstop for messages caught in flight by a drop; the latest-value policy above is what keeps the backlog small.
		self.client.max_queued_messages_set(4)
		self.client.max_inflight_messages_set(4)
		# connect_async + loop_start retries on its own, so a broker/DNS outage at boot doesn't kill the service.
		self.client.reconnect_delay_set(min_delay=1, max_delay=60)
		self.client.connect_async(host, port)
		self.client.loop_start()

	def _on_connect(self, client, userdata, flags, rcode):
		if rcode != 0:
			logging.warning("EcobeeMQTT:%s:connect refused rcode=%s", self.device_id, rcode)
			return
		# The broker drops our retained "online" only via the LWT, so re-assert after every reconnect.
		with self._lock:
			self._connected = True
			if self._latest_state is not None:
				self._send(state_topic(self.device_id), self._latest_state)
			if self._latest_online is not None:
				self._send(availability_topic(self.device_id), "online" if self._latest_online else "offline")

	def _on_disconnect(self, client, userdata, rcode):
		with self._lock:
			self._connected = False

	def _send(self, topic, payload):
		info = self.client.publish(topic, payload, qos=1, retain=True)
		if info.rc != mqtt.MQTT_ERR_SUCCESS:
			logging.warning("EcobeeMQTT:%s:publish to %s returned rc=%s", self.device_id, topic, info.rc)
		return info

	def publish_state(self, state):
		payload = json.dumps(state)
		with self._lock:
			self._latest_state = payload
			if self._connected:
				self._send(state_topic(self.device_id), payload)

	def publish_availability(self, online):
		with self._lock:
			self._latest_online = online
			if self._connected:
				self._send(availability_topic(self.device_id), "online" if online else "offline")

	# Publish "offline" and return the in-flight handle (None if the broker is unreachable, where the LWT/retained
	# offline already covers it); finish_close() waits on the handles under one shared deadline.
	def begin_close(self):
		with self._lock:
			self._latest_online = False
			if not self._connected:
				return None
			return self._send(availability_topic(self.device_id), "offline")

	def finish_close(self):
		self.client.disconnect()
		self.client.loop_stop()

def close_publishers(publishers, wait=CLOSE_PUBLISH_WAIT_SECONDS):
	pending = [(p, p.begin_close()) for p in publishers]
	deadline = time.monotonic() + wait
	for _, info in pending:
		if info is None:
			continue
		try:
			info.wait_for_publish(timeout=max(0.0, deadline - time.monotonic()))
		except (ValueError, RuntimeError):
			pass
	for publisher, _ in pending:
		publisher.finish_close()

class Poller:
	def __init__(self, device_id, name, pairing, publisher, clock=time.time, monotonic=time.monotonic):
		self.device_id = device_id
		self.name = name
		self.pairing = pairing
		self.publisher = publisher
		self.clock = clock
		self.monotonic = monotonic
		self.layout = None
		self._layout_source = None
		self._layout_at = 0
		self.failures = 0

	def _layout_stale(self):
		return (self.layout is None
			or self.pairing.accessories is not self._layout_source
			or self.monotonic() - self._layout_at >= LAYOUT_REFRESH_SECONDS)

	async def _ensure_layout(self):
		if self._layout_stale():
			await self.pairing.list_accessories_and_characteristics()
			layout = discover_layout(self.pairing.accessories)
			if not layout["thermostat"]:
				self.layout = None
				raise RuntimeError("no thermostat service on this accessory")
			self.layout = layout
			self._layout_source = self.pairing.accessories
			self._layout_at = self.monotonic()

	async def _read(self):
		await self._ensure_layout()
		values = await self.pairing.get_characteristics(read_ids(self.layout))
		if not thermostat_answered(self.layout, values):
			raise RuntimeError("every thermostat characteristic read returned a HAP error: %r" % (list(values.values())[:1],))
		return values

	async def poll_once(self):
		try:
			values = await asyncio.wait_for(self._read(), READ_TIMEOUT_SECONDS)
		except Exception as error:
			self.failures += 1
			# Re-learn the accessory tree on the next try: the ecobee may have gained/lost a room sensor.
			self.layout = None
			logging.warning("Ecobee:%s:poll failed (%d in a row): %r", self.device_id, self.failures, error)
			if self.failures == OFFLINE_AFTER_FAILURES:
				self.publisher.publish_availability(False)
			return False

		self.failures = 0
		self.publisher.publish_state(build_state(self.name, self.layout, values, int(self.clock())))
		self.publisher.publish_availability(True)
		return True

	# A poll is cancelled the moment stop is set: a wedged HomeKit read must not eat the stop deadline.
	async def run(self, stop, interval=POLL_INTERVAL_SECONDS):
		while not stop.is_set():
			poll = asyncio.ensure_future(self.poll_once())
			stopped = asyncio.ensure_future(stop.wait())
			try:
				await asyncio.wait({poll, stopped}, return_when=asyncio.FIRST_COMPLETED)
			finally:
				stopped.cancel()
				if not poll.done():
					poll.cancel()
					await asyncio.gather(poll, return_exceptions=True)
			try:
				await asyncio.wait_for(stop.wait(), interval)
			except asyncio.TimeoutError:
				pass

def _device_name(alias):
	return alias.replace("-", " ").replace("_", " ").title()

async def run_service(cfg, stop):
	pairings = load_pairings(pairings_path(cfg))
	if not pairings:
		raise ConfigError("No paired ecobees in %s; run ecobee_pair.py <name> <setup-code> first" % pairings_path(cfg))

	publishers = []
	async with open_controller() as controller:
		try:
			tasks = []
			for alias, data in pairings.items():
				pairing = ReadOnlyPairing(controller.load_pairing(alias, data))
				publisher = MqttPublisher("%s-ecobee-%s" % (cfg["client_id"], alias), cfg["broker_host"], cfg["broker_port"], alias)
				publishers.append(publisher)
				tasks.append(asyncio.create_task(Poller(alias, _device_name(alias), pairing, publisher).run(stop)))
			await asyncio.gather(*tasks)
		finally:
			# Offline first: it is the promise to HA, and controller teardown below can be slow.
			close_publishers(publishers)

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
