#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  ecobee_control.py
#
# Executes upstate/ecobee/<device_id>/set commands against one paired ecobee and returns the /result payload.
# The command/bounds/timing contract is gergy-incidents#1878; the only module that may call put_characteristics.

import asyncio
import json
import logging
import math
import time

from ecobee_state import discover_layout

# Hard limits on any target Joe's command sets or activates (F). Enforced here; HA mirrors them as defence in depth.
HEAT_BOUNDS_F = (45.0, 78.0)
COOL_BOUNDS_F = (65.0, 90.0)
# The ecobee exposes no deadband characteristic over HomeKit, so the contract's default applies.
DEADBAND_F = 3.0
# Device precision: a readback closer than this to the request counts as matching.
READBACK_TOLERANCE_F = 0.5

COMMAND_WINDOW_SECONDS = 30
MAX_FUTURE_SKEW_SECONDS = 5
RESULT_CACHE_SECONDS = 600
# Bounds each HomeKit round trip so one wedged connection can't hold the per-thermostat lock past HA's 35 s wait.
IO_TIMEOUT_SECONDS = 8

MODES = {"off": 0, "heat": 1, "cool": 2, "auto": 3}
# GERGY - the fan and preset value encodings below are unverified against a real ecobee3 (no hardware in dev);
# check them on the bench/site before trusting set_fan / set_preset, they are isolated here so a fix is one line.
FAN_VALUES = {"auto": 0, "on": 1}
PRESET_VALUES = {"home": 0, "sleep": 1, "away": 2}

OK, REJECTED, PARTIAL, FAILED, UNKNOWN = "ok", "rejected", "partial", "failed", "unknown"

class Rejected(Exception):
	pass

def f_to_c(fahrenheit):
	return round((fahrenheit - 32) * 5 / 9, 1)

def c_to_f(celsius):
	return round(celsius * 9 / 5 + 32, 1)

def _number(value, what):
	if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
		raise Rejected("%s must be a number" % what)
	return float(value)

def _choice(params, key, options):
	value = params.get(key)
	if not isinstance(value, str) or value not in options:
		raise Rejected("%s must be one of %s" % (key, "/".join(options)))
	return value

def _only(params, allowed):
	extra = set(params) - set(allowed)
	if extra:
		raise Rejected("unexpected parameter(s): %s" % ", ".join(sorted(extra)))

def _bounded(label, value, bounds):
	if not bounds[0] <= value <= bounds[1]:
		raise Rejected("%s %.1f F is outside the allowed %g-%g F" % (label, value, bounds[0], bounds[1]))

# Only the pairing methods the command path may reach; kept separate from the poller's read-only facade.
class WritablePairing:
	def __init__(self, pairing):
		self._pairing = pairing

	@property
	def accessories(self):
		return self._pairing.accessories

	async def list_accessories_and_characteristics(self):
		return await self._pairing.list_accessories_and_characteristics()

	async def get_characteristics(self, characteristics):
		return await self._pairing.get_characteristics(characteristics)

	async def put_characteristics(self, characteristics):
		return await self._pairing.put_characteristics(characteristics)

# One planned write plus how to confirm it: check(readback_value) -> bool. check None means the characteristic can't be
# read back (a trigger such as clear-hold), which is only acceptable when trigger is True.
class Step:
	def __init__(self, key, ident, value, applied, read_ident=None, check=None, trigger=False):
		self.key = key
		self.ident = ident
		self.value = value
		self.applied = applied
		self.read_ident = read_ident if read_ident is not None else ident
		self.check = check
		self.trigger = trigger
		self.write = None  # "ok" | "error" | "unknown"
		self.error = None
		self.confirmed = None  # True/False once read back, None if unverified

def _near_f(celsius_requested):
	return lambda actual: actual is not None and abs(c_to_f(actual) - c_to_f(celsius_requested)) <= READBACK_TOLERANCE_F

def _equals(expected):
	return lambda actual: actual == expected

class Controller:
	def __init__(self, device_id, pairing, publisher, republish, clock=time.time, monotonic=time.monotonic):
		self.device_id = device_id
		self.pairing = pairing
		self.publisher = publisher
		self.republish = republish
		self.clock = clock
		self.monotonic = monotonic
		self.closing = False
		self._lock = asyncio.Lock()
		self._results = {}  # id -> (monotonic expiry, result)
		self._inflight = set()

	# Entry point for the MQTT thread's payload; always ends in exactly one /result publication.
	async def handle(self, payload):
		task = asyncio.current_task()
		self._inflight.add(task)
		try:
			result = await self._handle(payload)
		except asyncio.CancelledError:
			raise
		except Exception as error:
			logging.exception("Ecobee:%s:command crashed", self.device_id)
			result = _result(None, UNKNOWN, "internal error: %r" % (error,))
		finally:
			self._inflight.discard(task)
		self.publisher.publish_result(result)

	async def drain(self, timeout):
		pending = set(self._inflight)
		if not pending:
			return
		_, still = await asyncio.wait(pending, timeout=timeout)
		for task in still:
			task.cancel()
		await asyncio.gather(*still, return_exceptions=True)

	def _purge(self):
		now = self.monotonic()
		for key in [k for k, (expiry, _) in self._results.items() if expiry <= now]:
			del self._results[key]

	async def _handle(self, payload):
		try:
			command = json.loads(payload)
			if not isinstance(command, dict):
				raise ValueError("not an object")
		except (ValueError, TypeError):
			return _result(None, REJECTED, "command is not a JSON object")
		cmd_id = command.get("id")
		if not isinstance(cmd_id, str) or not cmd_id:
			return _result(None, REJECTED, "missing command id")

		# Under the lock so a redelivery that arrives mid-flight waits, then finds the cached answer instead of writing again.
		async with self._lock:
			self._purge()
			cached = self._results.get(cmd_id)
			if cached:
				return cached[1]
			try:
				if self.closing:
					raise Rejected("service is stopping")
				result = await self._execute(cmd_id, command)
			except Rejected as error:
				result = _result(cmd_id, REJECTED, str(error))
			self._results[cmd_id] = (self.monotonic() + RESULT_CACHE_SECONDS, result)
			return result

	def _check_time(self, ts):
		now = self.clock()
		if ts - now > MAX_FUTURE_SKEW_SECONDS:
			raise Rejected("ts is %.0f s in the future (clock skew)" % (ts - now))
		if now > ts + COMMAND_WINDOW_SECONDS:
			raise Rejected("command expired (%.0f s old, limit %d s)" % (now - ts, COMMAND_WINDOW_SECONDS))

	async def _io(self, awaitable):
		return await asyncio.wait_for(awaitable, IO_TIMEOUT_SECONDS)

	async def _layout(self):
		if not self.pairing.accessories:
			await self._io(self.pairing.list_accessories_and_characteristics())
		layout = discover_layout(self.pairing.accessories)
		if not layout["thermostat"]:
			raise Rejected("no thermostat service on this accessory")
		return layout

	# Fresh values for the named layout entries (celsius/raw); a missing or errored one is None.
	async def _read(self, idents):
		try:
			values = await self._io(self.pairing.get_characteristics(set(idents.values())))
		except Exception as error:
			raise Rejected("could not read the thermostat: %r" % (error,))
		out = {}
		for key, ident in idents.items():
			entry = values.get(ident)
			out[key] = entry["value"] if entry and "status" not in entry and "value" in entry else None
		return out

	async def _execute(self, cmd_id, command):
		ts = command.get("ts")
		ts = _number(ts, "ts")
		name = command.get("command")
		params = command.get("parameters") or {}
		if not isinstance(params, dict):
			raise Rejected("parameters must be an object")
		self._check_time(ts)
		planner = getattr(self, "_plan_%s" % name, None) if isinstance(name, str) and name.isidentifier() else None
		if planner is None:
			raise Rejected("unknown command %r" % (name,))
		layout = await self._layout()
		steps = await planner(layout, params)
		if not steps:
			raise Rejected("nothing to do")
		return await self._run(cmd_id, ts, layout, steps)

	# -- planners: validate, pre-read what activation depends on, return ordered Steps; raise Rejected before any write --

	def _need(self, mapping, key, label):
		if key not in mapping:
			raise Rejected("this thermostat does not expose %s" % label)
		return mapping[key]

	async def _plan_set_mode(self, layout, params):
		_only(params, ["mode"])
		return await self._mode_steps(layout, _choice(params, "mode", MODES))

	async def _plan_turn_off(self, layout, params):
		_only(params, [])
		return await self._mode_steps(layout, "off")

	async def _plan_turn_on(self, layout, params):
		_only(params, [])
		return await self._mode_steps(layout, "heat")

	# Switching mode activates whatever target the thermostat already holds for the new mode, so that target must be in bounds.
	async def _mode_steps(self, layout, mode):
		t = layout["thermostat"]
		mode_id = self._need(t, "mode", "a mode control")
		if mode == "heat":
			now = await self._read({"target": self._need(t, "target", "a target temperature")})
			self._require_target("heat target", now["target"], HEAT_BOUNDS_F)
		elif mode == "cool":
			now = await self._read({"target": self._need(t, "target", "a target temperature")})
			self._require_target("cool target", now["target"], COOL_BOUNDS_F)
		elif mode == "auto":
			now = await self._read({
				"heat": self._need(t, "heat_threshold", "a heating threshold"),
				"cool": self._need(t, "cool_threshold", "a cooling threshold")})
			self._require_target("heat threshold", now["heat"], HEAT_BOUNDS_F)
			self._require_target("cool threshold", now["cool"], COOL_BOUNDS_F)
			self._require_deadband(c_to_f(now["heat"]), c_to_f(now["cool"]))
		return [Step("mode", mode_id, MODES[mode], mode, check=_equals(MODES[mode]))]

	def _require_target(self, label, celsius, bounds):
		if celsius is None:
			raise Rejected("cannot read the current %s to check it against the limits" % label)
		_bounded("current " + label, c_to_f(celsius), bounds)

	def _require_deadband(self, heat_f, cool_f):
		if heat_f > cool_f - DEADBAND_F:
			raise Rejected("heat %.1f F must be at least %g F below cool %.1f F" % (heat_f, DEADBAND_F, cool_f))

	async def _plan_set_setpoint(self, layout, params):
		_only(params, ["heat_setpoint", "cool_setpoint"])
		if not params:
			raise Rejected("give heat_setpoint and/or cool_setpoint")
		heat = _number(params["heat_setpoint"], "heat_setpoint") if "heat_setpoint" in params else None
		cool = _number(params["cool_setpoint"], "cool_setpoint") if "cool_setpoint" in params else None
		t = layout["thermostat"]
		idents = {"mode": self._need(t, "mode", "a mode control")}
		for key, ident in (("target", "target"), ("heat", "heat_threshold"), ("cool", "cool_threshold")):
			if ident in t:
				idents[key] = t[ident]
		now = await self._read(idents)
		mode = {v: k for k, v in MODES.items()}.get(now["mode"])
		if mode is None:
			raise Rejected("cannot read the current mode")
		if mode == "off":
			raise Rejected("thermostat is off")
		if heat is not None:
			_bounded("heat_setpoint", heat, HEAT_BOUNDS_F)
		if cool is not None:
			_bounded("cool_setpoint", cool, COOL_BOUNDS_F)

		if mode in ("heat", "cool"):
			wrong, right = ("cool", "heat") if mode == "heat" else ("heat", "cool")
			if (cool if mode == "heat" else heat) is not None:
				raise Rejected("%s_setpoint does not apply in %s mode" % (wrong, mode))
			value = heat if mode == "heat" else cool
			target_id = self._need(t, "target", "a target temperature")
			return [Step("%s_setpoint" % right, target_id, f_to_c(value), value, check=_near_f(f_to_c(value)))]

		heat_id = self._need(t, "heat_threshold", "a heating threshold")
		cool_id = self._need(t, "cool_threshold", "a cooling threshold")
		if now["heat"] is None or now["cool"] is None:
			raise Rejected("cannot read the current auto setpoints")
		old_h, old_c = c_to_f(now["heat"]), c_to_f(now["cool"])
		new_h = old_h if heat is None else heat
		new_c = old_c if cool is None else cool
		self._require_deadband(new_h, new_c)
		heat_step = Step("heat_setpoint", heat_id, f_to_c(heat), heat, check=_near_f(f_to_c(heat))) if heat is not None else None
		cool_step = Step("cool_setpoint", cool_id, f_to_c(cool), cool, check=_near_f(f_to_c(cool))) if cool is not None else None
		if heat_step and cool_step:
			# Cool first keeps (old heat, new cool) apart; otherwise heat first keeps (new heat, old cool) apart.
			cool_first = old_h <= new_c - DEADBAND_F
			return [cool_step, heat_step] if cool_first else [heat_step, cool_step]
		return [heat_step or cool_step]

	async def _plan_set_fan(self, layout, params):
		_only(params, ["fan"])
		fan = _choice(params, "fan", FAN_VALUES)
		c = layout["control"]
		write_id = self._need(c, "fan_write", "a fan control")
		value = FAN_VALUES[fan]
		check = (lambda actual: actual == 0) if fan == "auto" else (lambda actual: actual is not None and actual > 0)
		return [Step("fan", write_id, value, fan, read_ident=c.get("fan_read"), check=check if "fan_read" in c else None)]

	# Activating a preset activates the targets it stores, so those are checked first.
	async def _plan_set_preset(self, layout, params):
		_only(params, ["preset"])
		preset = _choice(params, "preset", PRESET_VALUES)
		c = layout["control"]
		hold_id = self._need(c, "set_hold", "a preset control")
		heat_id = self._need(c, "%s_heat" % preset, "the %s preset heat target" % preset)
		cool_id = self._need(c, "%s_cool" % preset, "the %s preset cool target" % preset)
		now = await self._read({"heat": heat_id, "cool": cool_id})
		self._require_target("%s preset heat target" % preset, now["heat"], HEAT_BOUNDS_F)
		self._require_target("%s preset cool target" % preset, now["cool"], COOL_BOUNDS_F)
		value = PRESET_VALUES[preset]
		return [Step("preset", hold_id, value, preset, read_ident=c.get("current_mode"),
			check=_equals(value) if "current_mode" in c else None)]

	# Allowed without a bounds check on purpose: it hands control back to the in-laws' own program, not a target Joe chose.
	async def _plan_resume_schedule(self, layout, params):
		_only(params, [])
		clear_id = self._need(layout["control"], "clear_hold", "a clear-hold control")
		return [Step("resume_schedule", clear_id, True, True, trigger=True)]

	# -- execution --

	async def _run(self, cmd_id, ts, layout, steps):
		# Re-checked here, after the pre-reads, because the window is measured to the first write, not to receipt.
		self._check_time(ts)
		wrote_any = False
		try:
			for step in steps:
				try:
					statuses = await self._io(self.pairing.put_characteristics([(step.ident[0], step.ident[1], step.value)]))
				except Exception as error:
					# Timeout or dropped link: the write may or may not have landed.
					step.write, step.error = "unknown", "write not confirmed: %r" % (error,)
					wrote_any = True
					break
				wrote_any = True
				status = (statuses or {}).get(step.ident)
				if status and status.get("status", 0) != 0:
					step.write, step.error = "error", "device refused %s: %s" % (step.key, status.get("description") or status.get("status"))
					break
				step.write = "ok"
			await self._verify(steps)
		finally:
			# Even a partial or unknown write changed what HA should show.
			if wrote_any:
				await self._republish()
		return _outcome(cmd_id, steps)

	async def _verify(self, steps):
		attempted = [s for s in steps if s.write in ("ok", "unknown")]
		reads = {i: s.read_ident for i, s in enumerate(attempted) if s.read_ident is not None and s.check is not None}
		values = {}
		if reads:
			try:
				got = await self._io(self.pairing.get_characteristics(set(reads.values())))
			except Exception as error:
				logging.warning("Ecobee:%s:readback failed: %r", self.device_id, error)
				got = {}
			values = got
		for i, step in enumerate(attempted):
			if step.check is None:
				step.confirmed = True if (step.trigger and step.write == "ok") else None
				continue
			entry = values.get(step.read_ident)
			if not entry or "status" in entry or "value" not in entry:
				step.confirmed = None
				continue
			step.confirmed = bool(step.check(entry["value"]))
			if not step.confirmed and step.write == "ok":
				step.error = "readback mismatch on %s" % step.key

	async def _republish(self):
		try:
			await self.republish()
		except Exception as error:
			logging.warning("Ecobee:%s:state republish failed: %r", self.device_id, error)

def _result(cmd_id, outcome, error=None, applied=None):
	return {"id": cmd_id, "outcome": outcome, "error": error, "applied": applied or {}}

def _outcome(cmd_id, steps):
	applied = {s.key: s.applied for s in steps if s.confirmed}
	errors = [s.error for s in steps if s.error]
	if len(applied) == len(steps):
		return _result(cmd_id, OK, None, applied)
	error = "; ".join(errors) or "effect could not be confirmed"
	if applied:
		return _result(cmd_id, PARTIAL, error, applied)
	# Nothing confirmed. Unknown if any attempted write might still have landed or could not be read back.
	unsure = any(s.write == "unknown" or (s.write == "ok" and s.confirmed is None) for s in steps)
	return _result(cmd_id, UNKNOWN if unsure else FAILED, error, applied)
