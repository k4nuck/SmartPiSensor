#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  fake_homekit_thermostat.py
#
# Bench-test double for an ecobee: a HAP-python thermostat plus one room sensor, so ecobee_pair.py and
# ecobee_service.py can be exercised on the home LAN before the on-site trip. Needs HAP-python (dev only).
#
# Usage: fake_homekit_thermostat.py [--pin 123-45-678] [--port 51826] [--state-file ./fake_thermostat.state]
# Stop it (Ctrl-C) to see the service flip the availability topic to offline.

import argparse
import logging
import signal

from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_THERMOSTAT

class FakeThermostat(Accessory):
	category = CATEGORY_THERMOSTAT

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		service = self.add_preload_service(
			"Thermostat",
			chars=["CurrentTemperature", "TargetTemperature", "CurrentHeatingCoolingState", "TargetHeatingCoolingState",
				"CurrentRelativeHumidity", "CoolingThresholdTemperature", "HeatingThresholdTemperature"])
		service.configure_char("CurrentTemperature", value=21.5)
		service.configure_char("CurrentRelativeHumidity", value=45)
		service.configure_char("TargetHeatingCoolingState", value=3)
		service.configure_char("CurrentHeatingCoolingState", value=1)
		service.configure_char("HeatingThresholdTemperature", value=20)
		service.configure_char("CoolingThresholdTemperature", value=25)

class FakeRoomSensor(Accessory):
	category = 10  # sensor

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.add_preload_service("TemperatureSensor").configure_char("CurrentTemperature", value=19.0)
		self.add_preload_service("OccupancySensor").configure_char("OccupancyDetected", value=1)

class FakeEcobeeBridge(Bridge):
	category = CATEGORY_THERMOSTAT

def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("--pin", default="123-45-678")
	parser.add_argument("--port", type=int, default=51826)
	parser.add_argument("--state-file", default="fake_thermostat.state")
	args = parser.parse_args()
	logging.basicConfig(level=logging.INFO)

	driver = AccessoryDriver(port=args.port, persist_file=args.state_file, pincode=args.pin.encode())
	# A bridge so the room sensor is its own accessory, as ecobee exposes its sensors.
	bridge = FakeEcobeeBridge(driver, "Fake Ecobee")
	bridge.add_accessory(FakeThermostat(driver, "Fake Living Room"))
	bridge.add_accessory(FakeRoomSensor(driver, "Fake Bedroom Sensor"))
	driver.add_accessory(accessory=bridge)
	signal.signal(signal.SIGTERM, driver.signal_handler)
	driver.start()

if __name__ == "__main__":
	main()
