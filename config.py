#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  config.py
#
# Per-Pi settings live outside the code so the same checkout runs on every Pi.

import configparser
import os
from urllib.parse import urlsplit

CONFIG_ENV = "SMARTSENSOR_CONFIG"
SECTION = "sensor"
DEFAULT_PORT = 1883

# key -> required?  Env var for each key is SMARTSENSOR_<KEY upper>.
KEYS = {
	"sensor_name": True,
	"client_id": True,
	"broker_host": True,
	"broker_port": False,
	"log_path": True,
	"fifo_path": False,
	"device_id": False,
	"device_name": False,
	"temp_unique_id": False,
	"hum_unique_id": False,
	"transport": False,
	"checkin_url": False,
	"checkin_token": False,
	"local_mqtt_host": False,
	"thermostat_name": False,
	"thermostat_unit": False,
	"setpoint_floor_f": False,
	"setpoint_ceiling_f": False,
}

TRANSPORTS = ("mqtt", "checkin")
DEFAULT_TRANSPORT = "mqtt"
DEFAULT_LOCAL_MQTT_HOST = "127.0.0.1"
DEFAULT_THERMOSTAT_NAME = "crawlspace_thermostat"
# zigbee2mqtt reports the TH1124ZB in Celsius unless the Pi's z2m is set otherwise
DEFAULT_THERMOSTAT_UNIT = "c"
DEFAULT_SETPOINT_FLOOR_F = 45.0
DEFAULT_SETPOINT_CEILING_F = 65.0

# The HA discovery identity is per-Pi: two Pis sharing these would overwrite each other's HA entities.
# Defaults are the bedroom Pi's values so an unset key leaves its payloads unchanged.
IDENTITY_DEFAULTS = {
	"device_id": "Attic01ae",
	"device_name": "Attic",
	"temp_unique_id": "temp01ae",
	"hum_unique_id": "hum01ae",
}

class ConfigError(Exception):
	pass

def default_config_path():
	return os.environ.get(CONFIG_ENV) or os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")

# Env vars win over the file so a one-off override doesn't need a file edit.
def load_config(path=None, environ=None):
	environ = os.environ if environ is None else environ
	path = path or default_config_path()

	values = {}
	parser = configparser.ConfigParser()
	if os.path.exists(path):
		parser.read(path)
		if parser.has_section(SECTION):
			values.update(parser[SECTION])

	for key in KEYS:
		env_value = environ.get("SMARTSENSOR_" + key.upper())
		if env_value:
			values[key] = env_value

	transport = (values.get("transport") or DEFAULT_TRANSPORT).strip().lower()
	if transport not in TRANSPORTS:
		raise ConfigError("transport must be one of %s, got %r" % ("/".join(TRANSPORTS), transport))
	checkin = transport == "checkin"

	# check-in mode talks to a local broker and a gateway, so the remote broker and its client id are unused
	required_keys = {k for k, required in KEYS.items() if required}
	if checkin:
		required_keys -= {"broker_host", "client_id"}
		# device_id doubles as the HA discovery identity in mqtt mode, whose bedroom default would
		# silently misidentify this Pi to the gateway, so check-in mode refuses to default it.
		required_keys |= {"checkin_url", "checkin_token", "device_id"}
	missing = [k for k in KEYS if k in required_keys and not values.get(k)]
	if missing:
		raise ConfigError("Missing config %s (looked in %s and SMARTSENSOR_* env vars)" % (", ".join(missing), path))

	try:
		port = int(values.get("broker_port") or DEFAULT_PORT)
	except ValueError:
		raise ConfigError("broker_port must be an integer, got %r" % values["broker_port"])

	try:
		floor = float(values.get("setpoint_floor_f") or DEFAULT_SETPOINT_FLOOR_F)
		ceiling = float(values.get("setpoint_ceiling_f") or DEFAULT_SETPOINT_CEILING_F)
	except ValueError:
		raise ConfigError("setpoint_floor_f and setpoint_ceiling_f must be numbers")
	if not floor < ceiling:
		raise ConfigError("setpoint_floor_f (%s) must be below setpoint_ceiling_f (%s)" % (floor, ceiling))

	unit = (values.get("thermostat_unit") or DEFAULT_THERMOSTAT_UNIT).strip().lower()
	if unit not in ("c", "f"):
		raise ConfigError("thermostat_unit must be c or f, got %r" % unit)

	if checkin:
		url = values["checkin_url"].strip()
		host = urlsplit(url).hostname or ""
		# The bearer token rides on this request; plain http is only tolerable on the loopback.
		if not (url.startswith("https://") or (url.startswith("http://") and host in ("localhost", "127.0.0.1"))):
			raise ConfigError("checkin_url must be https:// (got %r)" % url.split("?")[0])

	log_path = values["log_path"]
	return {
		"sensor_name": values["sensor_name"],
		"client_id": values.get("client_id"),
		"broker_host": values.get("broker_host"),
		"broker_port": port,
		"transport": transport,
		"checkin_url": (values.get("checkin_url") or "").strip(),
		"checkin_token": values.get("checkin_token") or "",
		"local_mqtt_host": values.get("local_mqtt_host") or DEFAULT_LOCAL_MQTT_HOST,
		"thermostat_name": values.get("thermostat_name") or DEFAULT_THERMOSTAT_NAME,
		"thermostat_unit": unit,
		"setpoint_floor_f": floor,
		"setpoint_ceiling_f": ceiling,
		"log_path": log_path,
		# The FIFO sits beside the log because both need a directory the service user can write.
		"fifo_path": values.get("fifo_path") or os.path.join(os.path.dirname(log_path), "temp.fifo"),
		**{k: values.get(k) or default for k, default in IDENTITY_DEFAULTS.items()},
	}
