#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  ecobee_pair.py
#
# Usage: ecobee_pair.py <name> <setup-code> [--device-id AA:BB:CC:DD:EE:FF]
# Run on site, on the same Wi-Fi as the thermostat. Pairs this Pi as the thermostat's HomeKit controller and
# stores the keys. With no --device-id it picks the single unpaired thermostat it can see; it refuses to guess
# between several.

import argparse
import asyncio
import logging
import sys

from config import load_config, ConfigError
from ecobee_hk import open_controller
from ecobee_store import pairings_path, load_pairings, save_pairings, slug

DISCOVER_SECONDS = 8
THERMOSTAT_CATEGORY = 9

class PairError(Exception):
	pass

def _normalize_code(code):
	digits = code.replace("-", "").strip()
	if len(digits) != 8 or not digits.isdigit():
		raise PairError("setup code must be 8 digits (XXX-XX-XXX), got %r" % code)
	return "%s-%s-%s" % (digits[:3], digits[3:5], digits[5:])

async def _find_unpaired(controller, device_id):
	if device_id:
		try:
			return await controller.async_find(device_id, timeout=DISCOVER_SECONDS)
		except Exception as error:
			raise PairError("no accessory %s found on this network: %r" % (device_id, error))

	await asyncio.sleep(DISCOVER_SECONDS)
	candidates = [d for d in controller.discoveries.values()
		if d.description.category == THERMOSTAT_CATEGORY and d.paired is False]
	if not candidates:
		raise PairError("no unpaired thermostat found in %ss; is this Pi on the thermostat's Wi-Fi, and is the ecobee in HomeKit setup mode?" % DISCOVER_SECONDS)
	if len(candidates) > 1:
		raise PairError("several unpaired thermostats found (%s); pass --device-id" % ", ".join(d.description.id for d in candidates))
	return candidates[0]

async def pair(cfg, name, code, device_id=None):
	alias = slug(name)
	code = _normalize_code(code)
	path = pairings_path(cfg)
	pairings = load_pairings(path)
	if alias in pairings:
		raise PairError("%r is already paired (keys in %s); remove its entry first to re-pair" % (alias, path))

	async with open_controller() as controller:
		discovery = await _find_unpaired(controller, device_id)
		if discovery.description.id in [p.get("AccessoryPairingID") for p in pairings.values()]:
			raise PairError("that thermostat is already paired under another name")
		try:
			finish = await discovery.async_start_pairing(alias)
			pairing = await finish(code)
		except Exception as error:
			raise PairError("pairing with %s failed (wrong setup code?): %r" % (discovery.description.id, error))
		pairings[alias] = pairing.pairing_data
		save_pairings(path, pairings)
		return alias, discovery.description.id

def main(argv=None):
	parser = argparse.ArgumentParser(description="Pair this Pi as a HomeKit controller of an ecobee (read-only use).")
	parser.add_argument("name", help="short name, becomes the MQTT device_id (e.g. living-room)")
	parser.add_argument("setup_code", help="HomeKit setup code from the ecobee screen, XXX-XX-XXX")
	parser.add_argument("--device-id", help="HomeKit device id, only needed if several unpaired thermostats are visible")
	args = parser.parse_args(argv)
	logging.basicConfig(level=logging.WARNING)
	try:
		alias, found = asyncio.run(pair(load_config(), args.name, args.setup_code, args.device_id))
	except (PairError, ConfigError, ValueError) as error:
		print("ecobee-pair FAILED: %s" % error, file=sys.stderr)
		return 1
	print("Paired %s as %r. Restart the service: sudo systemctl restart ecobee-reader" % (found, alias))
	return 0

if __name__ == "__main__":
	sys.exit(main())
