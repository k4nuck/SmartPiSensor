#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  sensorToMQTT.py
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

# Handle Getting Sensor data and pushing it on an MQTT
class SmartSensorToMQTT:
	def __init__(self, client, broker_hostname, port, discovery_name,sensor):

		logging.info("SmartSensorMQTT:Init")

		# Create
		self.client = mqtt.Client(client)    
		self.client.on_connect=on_connect

		# Connect to broker
		self.client.connect(broker_hostname, port)
		self.client.loop_start()

		self.sensor = sensor
		self.discovery_name = discovery_name
	 
	def __del__(self):
		logging.info("SmartSensorMQTT:destroyed")
		self.client.loop_stop()

	# Put buffer on the pipe
	def send_buffer_to_mqtt(self,topic, buffer):
		# topic = self.sensor.get_sensor_name()

		logging.info("SmartSensorMQTT:send_to_mqtt:Topic:"+str(topic))
		logging.info("SmartSensorMQTT:send_to_mqtt:Buffer:"+str(buffer))

		# Send to MQTT Pipe
		result = self.client.publish(topic, buffer)
		status = result[0]
		if status == 0:
			logging.info("SmartSensorMQTT:Sent:SUCCESS")
		else:
			logging.critical("SmartSensorMQTT:Sent:FAILED")

	# Return the base topic string
	# Subtype is needed to append to sensor name.  IE Temperature sensors.
	# Need a specific Topic for Temp vs Humidity
	def get_base_topic(self,subType=""):
		# discovery name should be the first part of a topic
		# IE.  Home Assistant uses "homeassistant" as the default 
		discovery_name = self.discovery_name

		# Sensor Type
		sensor_type = self.sensor.get_sensor_type()

		# Sensor Name
		sensor_name = self.sensor.get_sensor_name()

		return discovery_name+"/"+sensor_type+"/"+sensor_name+subType

	# Return Config Topic
	def get_config_topic(self,subType):
		return self.get_base_topic(subType) + "/config"
	
	# Return State Topic
	def get_state_topic(self):
		return self.get_base_topic() + "/state"
	
	# Return configuration payload
	def get_config_payload(self,subType):
		if subType == "T":
			return {"device_class": "temperature",
						"state_topic": self.get_state_topic(),
						"unit_of_measurement": "°F",
						"value_template": "{{ value_json.temperature_f}}",
						"unique_id": "temp01ae",
						"device": {"identifiers": ["Attic01ae"], "name": "Attic" }}
		
		if subType=="H":
			return {"device_class": "humidity",
						"state_topic": self.get_state_topic(),
						"unit_of_measurement": "%",
						"value_template": "{{ value_json.humidity}}",
						"unique_id": "hum01ae",
						"device": {"identifiers": ["Attic01ae"], "name": "Attic" }}
		
		logging.critical("smartSensorMQTT:get_config_payload:Unknown Subtype:"+str(subType))
		return ""

	# Return state payload
	def get_state_payload(self):
		# State Data is the same format as the sensor object data
		data = self.sensor.get_sensor_data()

		logging.debug("smartSensorMQTT:get_state_payload:data:"+str(data))

		payload = {"temperature_f":data["temperature_f"],
			 		"temperature_c":data["temperature_c"],
					"humidity":data["humidity"]}
					
		logging.debug("smartSensorMQTT:get_state_payload:payload:"+str(payload))

		return payload

	
	# Refresh
	# JB - For now we will push both Config and State Topics on all refreshes.
	def refresh(self):
		# JB - Think about how to generalize this more for different sensor types.
		# Construct Config Topic and payload
		# Temperature
		topic = self.get_config_topic("T")
		buffer = json.dumps(self.get_config_payload("T"))
		self.send_buffer_to_mqtt(topic, buffer)

		# Humidity
		topic = self.get_config_topic("H")
		buffer = json.dumps(self.get_config_payload("H"))
		self.send_buffer_to_mqtt(topic, buffer)

		# Construct State Topic and payload
		topic = self.get_state_topic()
		buffer = json.dumps(self.get_state_payload())
		self.send_buffer_to_mqtt(topic, buffer)
