#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  smartsensor.py
#
#

import multiprocessing 
import logging
import time
import adafruit_dht

# Handle Custom Smart Sensors

class SmartSensor:

	def __init__(self, pin, use_pulseio):
		logging.info("SmartSensor:Init:Pin:"+str(pin)+":pulseio:"+str(use_pulseio))
		self.mainQueue = multiprocessing.Queue()

		# Default Values for Sensor Data
		self.sensor_data = multiprocessing.Manager().dict({"temperature_f":0.0, "temperature_c":0.0, "humidity":0.0})
		self.pin = pin
		self.use_pulseio = use_pulseio

		# Kick off worker thread
		self.worker_thread = multiprocessing.Process(target=self.worker)
		self.worker_thread.start()

		# Sensor
		self.type = "PiSensor"

	def __del__(self):
		logging.info("SmartSensor:destroyed")
		self.worker_thread.terminate()

	# Return object with current temp (c/f) and humidity (as a percentage)	
	# This is an internal function	
	def __get_temp_from_sensor(self):
		dhtDevice = adafruit_dht.DHT22(self.pin, use_pulseio=self.use_pulseio)
	
		#Keep trying until we get a proper temperature
		while True:
			try:
				temperature_c = dhtDevice.temperature
				humidity = dhtDevice.humidity

				if (temperature_c ==None or humidity == None):
					logging.critical("SmartSensor:get_temp_from_sensor:failed to get temperature/humidity")
					continue

				temperature_f = temperature_c * (9 / 5) + 32
			
				logging.debug("SmartSensor:Before:get_temp_from_sensor:"+str(self.sensor_data))

				self.sensor_data["temperature_f"] = temperature_f
				self.sensor_data["temperature_c"] = temperature_c
				self.sensor_data["humidity"] = humidity

				logging.debug("SmartSensor:After:get_temp_from_sensor:"+str(self.sensor_data))

				# Cleanup
				dhtDevice.exit()
				return 

			except RuntimeError as error:
				# This is expected from time to time and 2 seconds needs to elapse to clear the error
				logging.debug("SmartSensor:get_temp_from_sensor:Runtime Error:"+str(error))
				time.sleep(2.0)
				continue
			except Exception as error:
				# We shouldn't get here.
				logging.critical("SmartSensor:get_temp_from_sensor:Exception:"+str(error))
				dhtDevice.exit()
				raise error
	
	# Handle Refresh
	def refresh(self):
		logging.debug("SmartSensor:Refresh")
		self.mainQueue.put({'cmd':"Refresh", 'data':None})

	# Get Sensor Data
	def get_sensor_data(self):
		return self.sensor_data

	# Returns sensor type
	def get_sensor_type(self):
		return self.type

	# Worker thread so that getting Sensor data doesn't block
	def worker(self):
		logging.info("Smart Sensor worker thread started:")

		#Main Loop
		while True:
			obj= self.mainQueue.get()

			# Handle Refresh
			if obj["cmd"]=="Refresh":
				logging.debug("SmartSensor:Main Loop:Refresh")
				self.__get_temp_from_sensor()


				 

