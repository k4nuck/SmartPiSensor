#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  sensorToMTQQ.py
#
#

import paho.mqtt.client as mqtt 
import logging
from smartsensor import *
import json

# Handle MqTT connection
def on_connect(client, userdata, flags, rcode):
	if rcode == 0:
		logging.info("SmartSensorMQTT:Connected")
	else:
		logging.info("SmartSensorMQTT:Failed Connect:rcode:", rcode)

# Handle Getting Sensor data and pushing it on an MTQQ
class SmartSensorToMTQQ:
	def __init__(self, client, broker_hostname, port, sensor):

		logging.info("SmartSensorMTQQ:Init")

		# Create
		self.client = mqtt.Client(client)    
		self.client.on_connect=on_connect

		# Connect to broker
		self.client.connect(broker_hostname, port)
		self.client.loop_start()

		self.sensor = sensor
	 
	def __del__(self):
		logging.info("SmartSensorMTQQ:destroyed")
		self.client.loop_stop()

	# Put buffer on the pipe
	def send_buffer_to_mqtt(self,buffer):
		topic = self.sensor.get_sensor_type()

		logging.info("SmartSensorMTQQ:send_to_mqtt:Topic:"+str(topic))
		logging.info("SmartSensorMTQQ:send_to_mqtt:Buffer:"+str(buffer))

		# Send to MQTT Pipe
		result = self.client.publish(topic, buffer)
		status = result[0]
		if status == 0:
			logging.info("SmartSensorMTQQ:Sent:SUCCESS")
		else:
			logging.critical("SmartSensorMTQQ:Sent:FAIED")

	
	# Refresh
	def refresh(self):
		data = self.sensor.get_sensor_data()

		logging.info("SmartSensorMTQQ:Refresh:"+str(data))
		buffer = json.dumps({"temperature_f":data["temperature_f"],"temperature_c":data["temperature_c"],"humidity":data["humidity"]})

		self.send_buffer_to_mqtt(buffer)
