#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  checkin.py
#
# Outbound-only HTTPS check-in transport (gergy-incidents#1558, parent #1555): reports the DHT22 and a
# local zigbee2mqtt thermostat to gergy-edge and applies the setpoint the reply asks for.

import json
import logging
import math
import threading
import time
import urllib.request
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

# A DHT22 read older than this is reported valid=false rather than passed off as current
DHT_FRESH_SECONDS = 180
POST_TIMEOUT_SECONDS = 10
# How long a published setpoint may go unechoed by zigbee2mqtt before it is reported failed
CONFIRM_TIMEOUT_SECONDS = 90
MAX_REPLY_BYTES = 64 * 1024
# TH1124ZB setpoint resolution
SETPOINT_STEP_C = 0.5

class CheckinError(Exception):
	pass

def c_to_f(c):
	return c * 9 / 5 + 32

def f_to_c(f):
	return (f - 32) * 5 / 9

def iso_utc(epoch):
	return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# JSON numbers only: bool is an int subclass and NaN/inf survive json.loads, neither is a temperature
def number(value):
	if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
		return None
	return value

def whole_if_integral(value):
	return int(value) if float(value).is_integer() else value

# zigbee2mqtt's optional last_seen: epoch (ms or s) or ISO 8601. Returns epoch seconds or None.
def parse_last_seen(value):
	if isinstance(value, (int, float)) and not isinstance(value, bool):
		return value / 1000 if value > 1e11 else float(value)
	if isinstance(value, str):
		try:
			return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
		except ValueError:
			return None
	return None

# The bearer token rides in a header that urllib errors don't echo, but scrub anyway so no log path can leak it
def scrub(text, token):
	return text.replace(token, "***") if token else text

class _NoRedirect(urllib.request.HTTPRedirectHandler):
	# urllib would replay the Authorization header to wherever a redirect points
	def redirect_request(self, *args, **kwargs):
		return None

_opener = urllib.request.build_opener(_NoRedirect)

# Returns the parsed JSON reply; raises on anything but a 200 with a JSON body
def http_post(url, token, body, timeout=POST_TIMEOUT_SECONDS):
	request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
		headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
	with _opener.open(request, timeout=timeout) as response:
		if response.status != 200:
			raise CheckinError("HTTP %s" % response.status)
		raw = response.read(MAX_REPLY_BYTES + 1)
	if len(raw) > MAX_REPLY_BYTES:
		raise CheckinError("reply too large")
	return json.loads(raw)

# Latest thermostat state from zigbee2mqtt. Written from the paho network thread, read from the main loop.
class ThermostatState:
	def __init__(self, unit="c"):
		self.unit = unit
		self._lock = threading.Lock()
		# field -> (value, epoch it was last present in a message). A field a message omits keeps its
		# old age, so a linkquality-only update can't make a stale temperature look current.
		self._fields = {}
		self._available = None

	def to_f(self, value):
		return c_to_f(value) if self.unit == "c" else value

	def to_device_units(self, f):
		return f_to_c(f) if self.unit == "c" else f

	def on_state(self, payload, retained, now):
		try:
			data = json.loads(payload)
		except (ValueError, TypeError):
			logging.warning("Checkin: unparseable thermostat state message")
			return
		if not isinstance(data, dict):
			return
		at = parse_last_seen(data.get("last_seen"))
		if at is None or at > now:
			# A retained message replayed at subscribe time says nothing about when the thermostat last
			# reported, and reading_at must never be refreshed from a cache, so it is dropped.
			if retained:
				return
			at = now
		with self._lock:
			for key, value in data.items():
				self._fields[key] = (value, at)

	def on_availability(self, payload):
		state = None
		try:
			data = json.loads(payload)
			state = data.get("state") if isinstance(data, dict) else data
		except (ValueError, TypeError):
			state = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
		if state in ("online", "offline"):
			with self._lock:
				self._available = state == "online"

	# Without the local broker we can't hear availability changes, so what we last heard is no longer evidence
	def invalidate_availability(self):
		with self._lock:
			self._available = None

	def is_available(self):
		with self._lock:
			return self._available is True

	# The setpoint (in device units) if a message carrying it arrived at or after `since`, else None
	def setpoint_since(self, since):
		with self._lock:
			entry = self._fields.get("occupied_heating_setpoint")
		if entry is None or entry[1] < since:
			return None
		return number(entry[0])

	# The contract's "thermostat" stream, or None when nothing has been heard from it
	def snapshot(self):
		with self._lock:
			fields, available = dict(self._fields), self._available

		def value(key):
			entry = fields.get(key)
			return entry[0] if entry else None
		reported = [fields[k][1] for k in ("local_temperature", "occupied_heating_setpoint", "system_mode",
			"running_state", "pi_heating_demand") if k in fields]
		if not reported:
			return None
		# Unknown availability is reported unavailable: HA must not act on a thermostat we can't vouch for
		out = {"available": available is True}
		local = number(value("local_temperature"))
		if local is not None:
			out["local_temperature_f"] = round(self.to_f(local), 1)
		setpoint = number(value("occupied_heating_setpoint"))
		if setpoint is not None:
			out["setpoint_f"] = round(self.to_f(setpoint), 1)
		for key in ("system_mode", "running_state"):
			if isinstance(value(key), str):
				out[key] = value(key)
		demand = number(value("pi_heating_demand"))
		if demand is not None:
			out["heating_demand_pct"] = whole_if_integral(demand)
		# The room temperature is what HA acts on, so its age is the stream's age; a fresher setpoint
		# or mode must not vouch for it. Without one, fall back to the newest field.
		out["reading_at"] = iso_utc(fields["local_temperature"][1] if "local_temperature" in fields else max(reported))
		return out

class CheckinBridge:
	def __init__(self, config, sensor, post=http_post, clock=time.time, mqtt_client=None):
		self.sensor = sensor
		self.device_id = config["device_id"]
		self.url = config["checkin_url"]
		self.token = config["checkin_token"]
		self.floor = config["setpoint_floor_f"]
		self.ceiling = config["setpoint_ceiling_f"]
		self.thermostat = ThermostatState(config["thermostat_unit"])
		self.state_topic = "zigbee2mqtt/" + config["thermostat_name"]
		self.availability_topic = self.state_topic + "/availability"
		self.set_topic = self.state_topic + "/set"
		self.post = post
		self.clock = clock
		# Epoch-seeded so seq keeps rising across restarts without persisting anything
		self.seq = int(clock())
		# Reported on the next check-in, cleared only once a POST carrying it succeeds
		self.pending_applied = None
		# (version, result) of the last desired change acted on; a repeat of it is not re-applied
		self.handled = None
		# (version, device-unit setpoint, written_at) of a write the thermostat hasn't echoed back yet
		self.unconfirmed = None

		self.client = mqtt_client or mqtt.Client((config.get("client_id") or "smartsensor") + "-checkin")
		self.client.on_connect = self.on_connect
		self.client.on_message = self.on_message
		self.client.on_disconnect = self.on_disconnect
		# connect_async + loop_start retries in the background, so a down local broker never blocks startup
		self.client.connect_async(config["local_mqtt_host"], config["broker_port"])
		self.client.loop_start()

	def close(self):
		try:
			self.client.loop_stop()
			self.client.disconnect()
		except Exception:
			logging.warning("Checkin: local MQTT shutdown failed", exc_info=True)

	# Subscribing here (not once at init) restores the subscriptions after every reconnect
	def on_connect(self, client, userdata, flags, rcode):
		if rcode == 0:
			client.subscribe([(self.state_topic, 0), (self.availability_topic, 0)])
			logging.info("Checkin: local MQTT connected")
		else:
			self.thermostat.invalidate_availability()
			logging.warning("Checkin: local MQTT connect failed, rcode %s" % rcode)

	# Fresh availability evidence has to arrive again after a reconnect
	def on_disconnect(self, client, userdata, rcode):
		self.thermostat.invalidate_availability()
		logging.warning("Checkin: local MQTT disconnected, rcode %s" % rcode)

	def on_message(self, client, userdata, message):
		try:
			if message.topic == self.state_topic:
				self.thermostat.on_state(message.payload, bool(message.retain), self.clock())
			elif message.topic == self.availability_topic:
				self.thermostat.on_availability(message.payload)
		except Exception:
			logging.warning("Checkin: bad thermostat message", exc_info=True)

	def dht22_reading(self, now):
		read_at = self.sensor.get_last_read_at()
		if not read_at:
			return None
		data = self.sensor.get_sensor_data().copy()
		age = now - read_at
		return {"temperature_f": data["temperature_f"], "temperature_c": data["temperature_c"],
			"humidity": whole_if_integral(data["humidity"]), "reading_at": iso_utc(read_at),
			# A negative age means the clock moved; trust nothing about that read
			"valid": 0 <= age <= DHT_FRESH_SECONDS}

	def build_body(self, now):
		readings = {}
		dht22 = self.dht22_reading(now)
		if dht22 is not None:
			readings["dht22"] = dht22
		thermostat = self.thermostat.snapshot()
		if thermostat is not None:
			readings["thermostat"] = thermostat
		body = {"device_id": self.device_id, "seq": self.seq, "ts": iso_utc(now), "readings": readings}
		if self.pending_applied is not None:
			body["applied"] = dict(self.pending_applied)
		return body

	# Hard bound on every write, independent of whatever HA believes the bounds are
	def setpoint_in_bounds(self, setpoint):
		return self.floor <= setpoint <= self.ceiling

	# Round to the thermostat's step, nudged back inside the bounds if rounding crossed one
	def device_setpoint(self, setpoint_f):
		if self.thermostat.unit != "c":
			return round(setpoint_f, 1)
		value = round(f_to_c(setpoint_f) / SETPOINT_STEP_C) * SETPOINT_STEP_C
		while c_to_f(value) < self.floor:
			value += SETPOINT_STEP_C
		while c_to_f(value) > self.ceiling:
			value -= SETPOINT_STEP_C
		return value

	# Returns the device-unit target once the publish is handed to the local broker, else None. That is NOT
	# the thermostat applying it: the message is QoS 0 and vanishes if zigbee2mqtt is down, so
	# confirm_write() waits for the thermostat's own echo.
	def write_setpoint(self, setpoint_f):
		try:
			target = self.device_setpoint(setpoint_f)
			if self.client.publish(self.set_topic, json.dumps({"occupied_heating_setpoint": target})).rc != 0:
				return None
			return target
		except Exception:
			logging.warning("Checkin: setpoint publish failed", exc_info=True)
			return None

	# Turns a write awaiting its echo into a result: zigbee2mqtt publishing the new setpoint is the only proof
	def confirm_write(self, now):
		if self.unconfirmed is None:
			return
		version, target, written_at = self.unconfirmed
		echoed = self.thermostat.setpoint_since(written_at)
		if echoed is not None and abs(echoed - target) < 0.01:
			result = "applied"
		elif now - written_at >= CONFIRM_TIMEOUT_SECONDS:
			result = "failed"
		else:
			return
		self.unconfirmed = None
		# A failed write stays unhandled so a re-offer retries it
		self.handled = (version, result) if result != "failed" else None
		self.pending_applied = {"setpoint_version": version, "result": result}
		logging.info("Checkin: desired setpoint v%s %s" % (version, result))

	def apply_reply(self, reply):
		if not isinstance(reply, dict) or reply.get("ok") is not True:
			logging.warning("Checkin: gateway reply not ok, ignoring")
			return
		desired = reply.get("desired")
		# No desired change means the thermostat is never touched
		if desired is None:
			return
		version = desired.get("version") if isinstance(desired, dict) else None
		if isinstance(version, bool) or not isinstance(version, int):
			logging.warning("Checkin: desired has no integer version, ignoring")
			return

		# Still waiting on the echo for this version: nothing to report yet, and never write twice
		if self.unconfirmed is not None and self.unconfirmed[0] == version:
			return
		# HA keeps offering the same version until it sees our report; acting twice would also
		# overwrite a manual change made at the thermostat in between. Re-offering means the
		# earlier report was lost, so report again without writing.
		if self.handled is not None and self.handled[0] == version:
			self.pending_applied = {"setpoint_version": version, "result": self.handled[1]}
			return

		setpoint = number(desired.get("setpoint_f"))
		if setpoint is None or not self.setpoint_in_bounds(setpoint):
			logging.warning("Checkin: desired setpoint v%s rejected (outside %s-%s F)" % (version, self.floor, self.ceiling))
			self.handled = (version, "rejected_bounds")
			self.pending_applied = {"setpoint_version": version, "result": "rejected_bounds"}
			return
		# Unavailable means the QoS 0 write would vanish, so fail now instead of waiting out the timeout
		target = self.write_setpoint(setpoint) if self.thermostat.is_available() else None
		if target is None:
			logging.warning("Checkin: desired setpoint v%s failed (thermostat unavailable or publish refused)" % version)
			self.handled = None
			self.unconfirmed = None
			self.pending_applied = {"setpoint_version": version, "result": "failed"}
			return
		# A newer version replaces an earlier unconfirmed one: its echo can't be told apart from this one's
		self.unconfirmed = (version, target, self.clock())
		self.pending_applied = None

	# One check-in. A failure just waits for the next tick; nothing is queued.
	def tick(self):
		now = self.clock()
		self.confirm_write(now)
		body = self.build_body(now)
		self.seq += 1
		try:
			reply = self.post(self.url, self.token, body)
		except Exception as error:
			logging.warning("Checkin: POST failed (%s: %s)" % (type(error).__name__, scrub(str(error), self.token)))
			return
		# Only an ok reply proves HA received the report; anything else leaves it to be re-sent
		if isinstance(reply, dict) and reply.get("ok") is True:
			self.pending_applied = None
		self.apply_reply(reply)
