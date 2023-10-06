#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  main.py

import multiprocessing 
import time
import board
import os
import paho.mqtt.client as mqtt 
import logging
import json
from logging.handlers import RotatingFileHandler
from smartsensor import *
from smartsensorToMQTT import *

# Process for sending commands to the server from command line
def fifo_worker(mainLoopQueue):
	
	logging.info("FIFO Worker Spawned")
	
	# Create FIFO File if needed
	path = "/home/k4nuck/projects/dht22/temp.fifo"
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
	logging.info("TIMER Worker Spawned")
	
	#Sleep and then notify parent
	while True:
		time.sleep(60)
		mainLoopQueue.put({'cmd':"Time", 'data':None})

# Main
def main():
	# Setup Logging
	log_level = logging.INFO
	logging.basicConfig(format='%(asctime)-15s %(levelname)-8s %(message)s', level=log_level)
	hdlr = RotatingFileHandler("/home/k4nuck/projects/dht22/dh22.log", maxBytes=(1048576*5), backupCount=5)
	logger = logging.getLogger("")
	formatter = logging.Formatter('%(asctime)-15s %(levelname)-8s %(message)s')
	hdlr.setFormatter(formatter)
	logger.addHandler(hdlr)
	
	logging.info( "Smart Temp Started")

	# Create Sensor
	sensor = SmartSensor(board.D4,False,"sensor","sensorAttic")

	# Create SmartSensorToMQTT  
	sensor_to_MQTT = SmartSensorToMQTT("PiSensorClient","k4nuck-ubuntu",1883,"homeassistant",sensor)

	logging.info("Main:Sensor Data:"+str(sensor.get_sensor_data()))

	# Create queue
	mainLoopQueue = multiprocessing.Queue()

	# Setup Threads
	fifo_thread = multiprocessing.Process(target=fifo_worker, args=(mainLoopQueue,))
	timer_thread = multiprocessing.Process(target=timer_worker, args=(mainLoopQueue,))
	
	logging.info( "Smart Temp: Kicking off threads")

	# Kick off threads
	fifo_thread.start()
	timer_thread.start()

	#Main Loop
	while True:
		obj= mainLoopQueue.get()
		logging.debug("Smart Temp: Main Loop:Item:%s" % obj["cmd"])	

		# Handle Timer Interupt
		if obj["cmd"]=="Time":
			logging.info("Main Loop:Sensor Data:"+str(sensor.get_sensor_data()))
			
			# Send sensor data to pipe
			sensor_to_MQTT.refresh()


		# Handle Exit
		if obj["cmd"]=="exit":
			logging.info( "Smart Pump: Quitting")
			fifo_thread.terminate()
			timer_thread.terminate()
			return

if __name__ == '__main__':
	main()
