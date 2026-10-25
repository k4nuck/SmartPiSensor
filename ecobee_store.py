#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  ecobee_store.py
#
# Pairing keys on disk. They let the holder read (and, if the code ever allowed it, control) the thermostat,
# so the directory is 700 and the file 600.

import json
import os
import re

PAIRINGS_FILE = "pairings.json"

def data_dir(cfg):
	# Beside the log so no new setting is needed; the service user already writes there.
	return os.path.join(os.path.dirname(cfg["log_path"]), "ecobee")

def pairings_path(cfg):
	return os.path.join(data_dir(cfg), PAIRINGS_FILE)

# The name doubles as the MQTT topic segment, so it must be a safe one.
def slug(name):
	value = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-")
	if not value:
		raise ValueError("name %r has no usable characters (use letters, digits, - or _)" % name)
	return value

def load_pairings(path):
	try:
		with open(path, encoding="utf-8") as fp:
			return json.load(fp)
	except FileNotFoundError:
		return {}

# Write-then-rename so a crash mid-write never leaves a truncated key file that locks us out of a paired ecobee.
def save_pairings(path, pairings):
	directory = os.path.dirname(path)
	os.makedirs(directory, mode=0o700, exist_ok=True)
	os.chmod(directory, 0o700)
	tmp = path + ".tmp"
	fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
	with os.fdopen(fd, "w", encoding="utf-8") as fp:
		json.dump(pairings, fp, indent=2)
		fp.flush()
		os.fsync(fp.fileno())
	os.chmod(tmp, 0o600)
	os.replace(tmp, path)
