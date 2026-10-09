#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  main.py

import multiprocessing 
import queue
import signal
import sys
import time
import board
import os
import paho.mqtt.client as mqtt 
import logging
import json
from logging.handlers import RotatingFileHandler
from config import load_config
from smartsensor import *
from smartsensorToMQTT import *

# Process for sending commands to the server from command line
def fifo_worker(mainLoopQueue, path):
	signal.signal(signal.SIGTERM, signal.SIG_DFL)
	logging.info("FIFO Worker Spawned")
	
	# Create FIFO File if needed
	if not os.path.exists(path):
		os.mkfifo(path)
	
	#Wait for Commands
	while True:
		fifo = open(path, "r")
		for line in fifo:
			logging.info( "FIFO;Received: (" + line + ")")
			
			if line=="exit":
				mainLoopQueue.put({'cmd':line, 'data':None})
					
		fifo.close()

# Process for notifying server of delta time has passed
def timer_worker(mainLoopQueue):
	signal.signal(signal.SIGTERM, signal.SIG_DFL)
	logging.info("TIMER Worker Spawned")
	
	#Sleep and then notify parent
	while True:
		time.sleep(60)
		mainLoopQueue.put({'cmd':"Time", 'data':None})

# How often the main loop wakes to check on its children when no message arrives
HEALTH_CHECK_SECONDS = 5
CHILD_STOP_SECONDS = 2

# Systemd sends SIGTERM; raising SystemExit unwinds main()'s finally block, which stops the children.
def handle_sigterm(signum, frame):
	raise SystemExit(0)

# Non-daemon children would otherwise block interpreter exit, which is how the service hung "active".
def stop_children(children):
	for child in children:
		if child.is_alive():
			child.terminate()
	for child in children:
		child.join(CHILD_STOP_SECONDS)
		if child.is_alive():
			child.kill()
			child.join(CHILD_STOP_SECONDS)

# Returns a reason string if any child (or the sensor's Manager) is dead, else None
def find_failure(children, sensor):
	for name, child in children.items():
		if not child.is_alive():
			return "%s process died (exitcode %s)" % (name, child.exitcode)
	ok, reason = sensor.check_health()
	if not ok:
		return reason
	return None

# Main. Returns the process exit code: non-zero means a child died and systemd should restart us.
def main(config_path=None):
	config = load_config(config_path)

	# Setup Logging
	log_level = logging.INFO
	logging.basicConfig(format='%(asctime)-15s %(levelname)-8s %(message)s', level=log_level)
	hdlr = RotatingFileHandler(config["log_path"], maxBytes=(1048576*5), backupCount=5)
	logger = logging.getLogger("")
	formatter = logging.Formatter('%(asctime)-15s %(levelname)-8s %(message)s')
	hdlr.setFormatter(formatter)
	logger.addHandler(hdlr)
	
	logging.info( "Smart Temp Started")

	# Installed before anything that can block (broker connect retries) so SIGTERM always works
	signal.signal(signal.SIGTERM, handle_sigterm)

	sensor = None
	children = {}
	try:
		# Create Sensor
		sensor = SmartSensor(board.D4,False,"sensor",config["sensor_name"])

		# Create SmartSensorToMQTT
		sensor_to_MQTT_prod = SmartSensorToMQTT(config["client_id"],config["broker_host"],config["broker_port"],"homeassistant",sensor)

		# Create queue
		mainLoopQueue = multiprocessing.Queue()

		# Setup Threads
		children["fifo"] = multiprocessing.Process(target=fifo_worker, args=(mainLoopQueue, config["fifo_path"]))
		children["timer"] = multiprocessing.Process(target=timer_worker, args=(mainLoopQueue,))
		
		logging.info( "Smart Temp: Kicking off threads")

		# Kick off threads
		for child in children.values():
			child.start()

		#Main Loop
		while True:
			failure = find_failure(children, sensor)
			if failure:
				logging.critical("Smart Temp: %s; exiting so systemd restarts us" % failure)
				return 1

			try:
				obj = mainLoopQueue.get(timeout=HEALTH_CHECK_SECONDS)
			except queue.Empty:
				continue
			logging.debug("Smart Temp: Main Loop:Item:%s" % obj["cmd"])	

			# Handle Timer Interupt
			if obj["cmd"]=="Time":
				logging.info("Main Loop:Sensor Data:"+str(sensor.get_sensor_data()))
				
				# Send sensor data to pipe
				sensor_to_MQTT_prod.refresh()

			# Handle Exit
			if obj["cmd"]=="exit":
				logging.info( "Smart Pump: Quitting")
				return 0
	except SystemExit:
		logging.info("Smart Temp: SIGTERM received, shutting down")
		return 0
	except Exception:
		# Anything unexpected must still end the process non-zero, never leave it half-alive
		logging.critical("Smart Temp: fatal error", exc_info=True)
		return 1
	finally:
		stop_children(list(children.values()))
		if sensor is not None:
			sensor.shutdown()

if __name__ == '__main__':
	sys.exit(main())
