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
import tempfile

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

# Called BEFORE pairing: once the thermostat accepts the setup code the new keys exist only in memory, and an
# ecobee that is paired without our keys can only be recovered by a physical HomeKit reset. So prove up front that
# the directory is creatable, private and writable (a probe file, created and removed the way save_pairings will).
def prepare_storage(path):
	directory = os.path.dirname(path)
	os.makedirs(directory, mode=0o700, exist_ok=True)
	os.chmod(directory, 0o700)
	fd, probe = tempfile.mkstemp(dir=directory, prefix=".probe-")
	os.close(fd)
	os.unlink(probe)

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

# Last resort when save_pairings fails after a successful pairing: park just this pairing's keys in a private file
# elsewhere (O_EXCL, 600) so an operator can merge them into pairings.json instead of resetting the thermostat.
def write_recovery(alias, pairing_data, candidate_dirs):
	for directory in candidate_dirs:
		path = os.path.join(directory, "ecobee-pairing-%s.recovery.json" % alias)
		try:
			fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
			with os.fdopen(fd, "w", encoding="utf-8") as fp:
				json.dump({alias: pairing_data}, fp, indent=2)
				fp.flush()
				os.fsync(fp.fileno())
			return path
		except OSError:
			continue
	return None
