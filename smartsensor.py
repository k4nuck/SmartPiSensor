#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  smartsensor.py
#
#

import multiprocessing 
import logging
import signal
import threading
import time
import adafruit_dht
from multiprocessing.managers import SyncManager
from procutil import stop_processes

# Upper bound on one Manager round trip; a stopped or hung Manager would otherwise block forever
MANAGER_PROBE_SECONDS = 3

# The Manager server forks from a process whose SIGTERM handler raises SystemExit; it must die on SIGTERM instead.
def _manager_init():
	signal.signal(signal.SIGTERM, signal.SIG_DFL)

# Handle Custom Smart Sensors

class SmartSensor:

	def __init__(self, pin, use_pulseio, type, name):
		logging.info("SmartSensor:Init:Pin:"+str(pin)+":pulseio:"+str(use_pulseio)+":type:"+str(type)+":name:"+str(name))

		# Set before anything that can fail so shutdown() is safe on a half-built object
		self.manager = None
		self.worker_thread = None
		self.timer_thread = None
		self._closed = False

		self.pin = pin
		self.use_pulseio = use_pulseio

		# Sensor
		self.type = type
		self.name = name

		# A SIGTERM or start() failure mid-construction would otherwise leak already-started non-daemon children,
		# because the caller never receives the object to shut down.
		try:
			self.mainQueue = multiprocessing.Queue()

			# Default Values for Sensor Data
			self.manager = SyncManager()
			self.manager.start(_manager_init)
			self.sensor_data = self.manager.dict({"temperature_f":0.0, "temperature_c":0.0, "humidity":0.0})

			# Kick off worker and timer thread
			self.worker_thread = multiprocessing.Process(target=self.worker)
			self.timer_thread = multiprocessing.Process(target=self.timer_worker)
			self.worker_thread.start()
			self.timer_thread.start()
		except BaseException:
			self.shutdown()
			raise

	def __del__(self):
		logging.info("SmartSensor:destroyed")
		self.shutdown()

	# Non-daemon children keep the interpreter alive at exit, so they must be stopped explicitly.
	def shutdown(self):
		if self._closed:
			return
		self._closed = True
		stop_processes([self.worker_thread, self.timer_thread])
		if self.manager is not None:
			# manager.shutdown() sends an RPC first, which never returns if the Manager is stopped.
			# Killing the server process first makes its finalizer skip the RPC and just clean up.
			stop_processes([getattr(self.manager, "_process", None)])
			try:
				self.manager.shutdown()
			except Exception:
				logging.warning("SmartSensor:Manager shutdown failed", exc_info=True)

	# Returns (ok, reason). A dead child or Manager leaves the process half-working, so the
	# supervisor in main.py uses this to decide to exit and let systemd restart us.
	def check_health(self):
		for name, proc in (("worker", self.worker_thread), ("timer", self.timer_thread),
				("manager", getattr(self.manager, "_process", None))):
			if proc is not None and not proc.is_alive():
				return False, "sensor %s process died (exitcode %s)" % (name, proc.exitcode)

		# A proxy call blocks indefinitely on a stopped Manager, so probe from a thread we can abandon
		outcome = {}
		def probe():
			try:
				self.sensor_data.copy()
			except Exception as error:
				outcome["error"] = error
		prober = threading.Thread(target=probe, daemon=True)
		prober.start()
		prober.join(MANAGER_PROBE_SECONDS)
		if prober.is_alive():
			return False, "sensor data Manager unresponsive for %ss" % MANAGER_PROBE_SECONDS
		if "error" in outcome:
			return False, "sensor data Manager unusable: %r" % (outcome["error"],)
		return True, ""

	# Return object with current temp (c/f) and humidity (as a percentage)	
	# This is an internal function	
	def __get_temp_from_sensor(self):
		dhtDevice = adafruit_dht.DHT22(self.pin, use_pulseio=self.use_pulseio)
	
		#Keep trying until we get a proper temperature
		while True:
			# JB
			#temperature_c = 100
			#temperature_f = temperature_c * (9 / 5) + 32
			#humidity = 100

			#self.sensor_data["temperature_f"] = round(temperature_f,1)
			#self.sensor_data["temperature_c"] = round(temperature_c,1)
			#self.sensor_data["humidity"] = round(humidity,0)
			#return

			try:
				temperature_c = dhtDevice.temperature
				humidity = dhtDevice.humidity

				if (temperature_c ==None or humidity == None):
					logging.critical("SmartSensor:get_temp_from_sensor:failed to get temperature/humidity")
					continue

				temperature_f = temperature_c * (9 / 5) + 32
			
				logging.debug("SmartSensor:Before:get_temp_from_sensor:"+str(self.sensor_data))

				self.sensor_data["temperature_f"] = round(temperature_f,1)
				self.sensor_data["temperature_c"] = round(temperature_c,1)
				self.sensor_data["humidity"] = round(humidity,0)

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
				logging.critical("SmartSensor:get_temp_from_sensor:Exception:DEAD:"+str(error))

				#temperature_c = 100
				#temperature_f = temperature_c * (9 / 5) + 32
				#humidity = 100

				#self.sensor_data["temperature_f"] = round(temperature_f,1)
				#self.sensor_data["temperature_c"] = round(temperature_c,1)
				#self.sensor_data["humidity"] = round(humidity,0)

				dhtDevice.exit()
				#return
				raise error
				#time.sleep(2.0)
				#continue
			
	# Process for notifying server of delta time has passed
	def timer_worker(self):
		signal.signal(signal.SIGTERM, signal.SIG_DFL)
		logging.info("SmartSensor:TIMER Worker Spawned")
	
		#Sleep and then notify parent
		while True:
			self.refresh()
			time.sleep(10)
			
	
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

	# Returns Sensor Name
	def get_sensor_name(self):
		return self.name

	# Worker thread so that getting Sensor data doesn't block
	def worker(self):
		signal.signal(signal.SIGTERM, signal.SIG_DFL)
		logging.info("Smart Sensor worker thread started:")

		#Main Loop
		while True:
			obj= self.mainQueue.get()

			# Handle Refresh
			if obj["cmd"]=="Refresh":
				logging.debug("SmartSensor:Main Loop:Refresh")
				self.__get_temp_from_sensor()


				 

