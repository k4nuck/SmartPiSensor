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
import time

import paho.mqtt.client as mqtt

from config import load_config, ConfigError
from ecobee_hk import open_controller
from ecobee_state import discover_layout, read_ids, build_state
from ecobee_store import pairings_path, load_pairings

TOPIC_ROOT = "upstate/ecobee"
POLL_INTERVAL_SECONDS = 60
# Bounds one HomeKit read so a wedged connection can't stall the loop past the next poll.
READ_TIMEOUT_SECONDS = 20
# One missed poll is routine on Wi-Fi; two in a row means the thermostat is really gone.
OFFLINE_AFTER_FAILURES = 2

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
class MqttPublisher:
	def __init__(self, client_id, host, port, device_id):
		self.device_id = device_id
		self._online = False
		self.client = mqtt.Client(client_id)
		self.client.will_set(availability_topic(device_id), "offline", qos=1, retain=True)
		self.client.on_connect = self._on_connect
		# connect_async + loop_start retries on its own, so a broker/DNS outage at boot doesn't kill the service.
		self.client.reconnect_delay_set(min_delay=1, max_delay=60)
		self.client.connect_async(host, port)
		self.client.loop_start()

	def _on_connect(self, client, userdata, flags, rcode):
		if rcode != 0:
			logging.warning("EcobeeMQTT:%s:connect refused rcode=%s", self.device_id, rcode)
			return
		# The broker drops our retained "online" only via the LWT, so re-assert after every reconnect.
		if self._online:
			self.publish_availability(True)

	def publish_state(self, state):
		self.client.publish(state_topic(self.device_id), json.dumps(state), qos=1, retain=True)

	def publish_availability(self, online):
		self._online = online
		self.client.publish(availability_topic(self.device_id), "online" if online else "offline", qos=1, retain=True)

	def close(self):
		info = self.client.publish(availability_topic(self.device_id), "offline", qos=1, retain=True)
		try:
			info.wait_for_publish(timeout=3)
		except (ValueError, RuntimeError):
			pass
		self.client.disconnect()
		self.client.loop_stop()

class Poller:
	def __init__(self, device_id, name, pairing, publisher, clock=time.time):
		self.device_id = device_id
		self.name = name
		self.pairing = pairing
		self.publisher = publisher
		self.clock = clock
		self.layout = None
		self.failures = 0

	async def _ensure_layout(self):
		if self.layout is None:
			await self.pairing.list_accessories_and_characteristics()
			self.layout = discover_layout(self.pairing.accessories)
			if not self.layout["thermostat"]:
				self.layout = None
				raise RuntimeError("no thermostat service on this accessory")

	async def poll_once(self):
		try:
			await asyncio.wait_for(self._ensure_layout(), READ_TIMEOUT_SECONDS)
			values = await asyncio.wait_for(self.pairing.get_characteristics(read_ids(self.layout)), READ_TIMEOUT_SECONDS)
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

	async def run(self, stop, interval=POLL_INTERVAL_SECONDS):
		while not stop.is_set():
			await self.poll_once()
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
	try:
		async with open_controller() as controller:
			tasks = []
			for alias, data in pairings.items():
				pairing = ReadOnlyPairing(controller.load_pairing(alias, data))
				publisher = MqttPublisher("%s-ecobee-%s" % (cfg["client_id"], alias), cfg["broker_host"], cfg["broker_port"], alias)
				publishers.append(publisher)
				tasks.append(asyncio.create_task(Poller(alias, _device_name(alias), pairing, publisher).run(stop)))
			await asyncio.gather(*tasks)
	finally:
		for publisher in publishers:
			publisher.close()

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
