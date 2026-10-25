#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  ecobee_state.py
#
# Maps a paired HomeKit thermostat's accessory tree to the upstate/ecobee/<device_id>/state payload.
# Pure functions: no network, no writes.

from aiohomekit.model.characteristics import CharacteristicsTypes as C
from aiohomekit.model.services import ServicesTypes as S

# HomeKit HEATING_COOLING_TARGET / _CURRENT enums
HVAC_MODES = {0: "off", 1: "heat", 2: "cool", 3: "heat_cool"}
HVAC_ACTIONS = {0: "idle", 1: "heating", 2: "cooling"}

def _first_char(accessory, service_type, char_type):
	for service in accessory.services.filter(service_type=service_type):
		if service.has(char_type):
			return service[char_type]
	return None

def _id(char):
	return None if char is None else (char.service.accessory.aid, char.iid)

# Returns {"thermostat": {field: (aid, iid)}, "sensors": [{"name", "temperature_f", "occupancy"}]} where each
# value is the (aid, iid) to read. Room sensors are the non-thermostat accessories that expose a temperature.
def discover_layout(accessories):
	layout = {"thermostat": {}, "sensors": []}
	for accessory in accessories:
		if accessory.services.first(service_type=S.THERMOSTAT) is not None:
			if layout["thermostat"]:
				continue
			chars = {
				"temperature_f": _first_char(accessory, S.THERMOSTAT, C.TEMPERATURE_CURRENT),
				"humidity": _first_char(accessory, S.THERMOSTAT, C.RELATIVE_HUMIDITY_CURRENT)
					or _first_char(accessory, S.HUMIDITY_SENSOR, C.RELATIVE_HUMIDITY_CURRENT),
				"mode": _first_char(accessory, S.THERMOSTAT, C.HEATING_COOLING_TARGET),
				"action": _first_char(accessory, S.THERMOSTAT, C.HEATING_COOLING_CURRENT),
				"target": _first_char(accessory, S.THERMOSTAT, C.TEMPERATURE_TARGET),
				"heat_threshold": _first_char(accessory, S.THERMOSTAT, C.TEMPERATURE_HEATING_THRESHOLD),
				"cool_threshold": _first_char(accessory, S.THERMOSTAT, C.TEMPERATURE_COOLING_THRESHOLD),
			}
			layout["thermostat"] = {k: _id(v) for k, v in chars.items() if v is not None}
		else:
			temperature = _first_char(accessory, S.TEMPERATURE_SENSOR, C.TEMPERATURE_CURRENT)
			if temperature is None:
				continue
			layout["sensors"].append({
				"name": accessory.name or "sensor %s" % accessory.aid,
				"temperature_f": _id(temperature),
				"occupancy": _id(_first_char(accessory, S.OCCUPANCY_SENSOR, C.OCCUPANCY_DETECTED)),
			})
	return layout

def read_ids(layout):
	ids = set(layout["thermostat"].values())
	for sensor in layout["sensors"]:
		ids.add(sensor["temperature_f"])
		if sensor["occupancy"]:
			ids.add(sensor["occupancy"])
	return ids

def c_to_f(celsius):
	return None if celsius is None else round(celsius * 9 / 5 + 32, 1)

# values: {(aid, iid): {"value": x}} as get_characteristics returns; an entry with "status" (HAP error) reads as None.
def _val(values, ident):
	if ident is None:
		return None
	entry = values.get(ident)
	if not entry or "status" in entry:
		return None
	return entry.get("value")

def build_state(name, layout, values, ts):
	t = layout["thermostat"]
	get = lambda key: _val(values, t.get(key))

	mode = HVAC_MODES.get(get("mode"))
	target = c_to_f(get("target"))
	# Auto mode reports heat/cool thresholds; single-setpoint modes report only the one target.
	target_heat = c_to_f(get("heat_threshold"))
	target_cool = c_to_f(get("cool_threshold"))
	if target_heat is None and mode == "heat":
		target_heat = target
	if target_cool is None and mode == "cool":
		target_cool = target

	occupancy_of = lambda ident: None if ident is None or _val(values, ident) is None else bool(_val(values, ident))
	return {
		"name": name,
		"temperature_f": c_to_f(get("temperature_f")),
		"humidity": get("humidity"),
		"hvac_mode": mode,
		"hvac_action": HVAC_ACTIONS.get(get("action")),
		"target_heat_f": target_heat,
		"target_cool_f": target_cool,
		"sensors": [
			{"name": s["name"], "temperature_f": c_to_f(_val(values, s["temperature_f"])), "occupancy": occupancy_of(s["occupancy"])}
			for s in layout["sensors"]
		],
		"ts": ts,
	}
