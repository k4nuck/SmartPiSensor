#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  config.py
#
# Per-Pi settings live outside the code so the same checkout runs on every Pi.

import configparser
import os

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

	missing = [k for k, required in KEYS.items() if required and not values.get(k)]
	if missing:
		raise ConfigError("Missing config %s (looked in %s and SMARTSENSOR_* env vars)" % (", ".join(missing), path))

	try:
		port = int(values.get("broker_port") or DEFAULT_PORT)
	except ValueError:
		raise ConfigError("broker_port must be an integer, got %r" % values["broker_port"])

	log_path = values["log_path"]
	return {
		"sensor_name": values["sensor_name"],
		"client_id": values["client_id"],
		"broker_host": values["broker_host"],
		"broker_port": port,
		"log_path": log_path,
		# The FIFO sits beside the log because both need a directory the service user can write.
		"fifo_path": values.get("fifo_path") or os.path.join(os.path.dirname(log_path), "temp.fifo"),
	}
