#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  ecobee_control.py
#
# Executes upstate/ecobee/<device_id>/set commands against one paired ecobee and returns the /result payload.
# The command/bounds/timing contract is gergy-incidents#1878; the only module that may call put_characteristics.

import asyncio
import contextvars
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

# Deadline is ts + 20 s, checked right before the first write. HA waits 25 s for /result (under the 30 s MCP proxy timeout).
COMMAND_WINDOW_SECONDS = 20
MAX_FUTURE_SKEW_SECONDS = 5
RESULT_CACHE_SECONDS = 600
# One budget shared by discovery, pre-read, writes and readbacks, counted from receipt, so /result is published well
# inside the 20 s window even when several round trips are slow. The state refresh is outside it and runs after the result.
COMMAND_BUDGET_SECONDS = 15
# Caps a single HomeKit round trip; the shared budget caps the sum.
IO_TIMEOUT_SECONDS = 8
# A write needs room for its own round trip plus a readback; below this, stop rather than start one that can't be confirmed.
MIN_BUDGET_TO_WRITE_SECONDS = 5
# After the drain deadline, how long cancelled commands get to unwind before shutdown moves on regardless.
CANCEL_GRACE_SECONDS = 1

MODES = {"off": 0, "heat": 1, "cool": 2, "auto": 3}
# Fan write speed per aiohomekit's VENDOR_ECOBEE_FAN_WRITE_SPEED documentation: 100 for on, 0 for off/auto.
FAN_VALUES = {"auto": 0, "on": 100}
# GERGY - the preset encoding is unverified against a real ecobee3 (no hardware in dev); check it on site before
# trusting set_preset. It is isolated here so a fix is one line.
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

# The device stores Celsius in 0.1 steps. Every bounds and deadband decision is made on that quantized value (integer
# tenths of C), the same value that is written and read back, so a target that was accepted never reads back as out of bounds.
def _tenths(celsius):
	return int(round(celsius * 10))

def _quantize(fahrenheit):
	return _tenths(f_to_c(fahrenheit))

def _deadband_tenths():
	# Floor, not round: two targets exactly DEADBAND_F apart can lose up to one step each to quantization.
	return int(DEADBAND_F * 5 / 9 * 10)

def _bounded(label, tenths, bounds):
	if not _quantize(bounds[0]) <= tenths <= _quantize(bounds[1]):
		raise Rejected("%s %.1f F is outside the allowed %g-%g F" % (label, c_to_f(tenths / 10), bounds[0], bounds[1]))

class _Budget:
	def __init__(self, monotonic, total):
		self._monotonic = monotonic
		self._deadline = monotonic() + total

	def remaining(self):
		return self._deadline - self._monotonic()

# Per command task, so the planners and I/O helpers share one budget without threading it through every signature.
_BUDGET = contextvars.ContextVar("ecobee_command_budget", default=None)

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
	def __init__(self, key, ident, value, applied, read_ident=None, check=None, trigger=False, guard=None):
		self.key = key
		self.ident = ident
		self.value = value
		self.applied = applied
		self.read_ident = read_ident if read_ident is not None else ident
		self.check = check
		self.trigger = trigger
		# (idents, fn): before this step is written, re-read idents and fn(values) returns a reason to stop, or None.
		self.guard = guard
		self.write = None  # "ok" | "error" | "unknown" | "skipped"
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

	# Entry point for the MQTT thread's payload; always ends in exactly one /result publication (unless cancelled at shutdown).
	async def handle(self, payload):
		task = asyncio.current_task()
		self._inflight.add(task)
		token = _BUDGET.set(_Budget(self.monotonic, COMMAND_BUDGET_SECONDS))
		try:
			try:
				result, refresh = await self._handle(payload)
			except asyncio.CancelledError:
				raise
			except Exception as error:
				logging.exception("Ecobee:%s:command crashed", self.device_id)
				result, refresh = _result(None, UNKNOWN, "internal error: %r" % (error,)), False
			# The verified result goes out before the slow state refresh, which must never delay it.
			self.publisher.publish_result(result)
			if refresh and not self.closing:
				await self._refresh()
		finally:
			_BUDGET.reset(token)
			self._inflight.discard(task)

	# Bounded end to end: wait, cancel what is left, then wait only CANCEL_GRACE_SECONDS for the unwind. Cancellation
	# starts no new work (the refresh is skipped once closing), so there is nothing else to wait for.
	async def drain(self, timeout):
		pending = set(self._inflight)
		if not pending:
			return
		_, still = await asyncio.wait(pending, timeout=timeout)
		for task in still:
			task.cancel()
		if still:
			await asyncio.wait(still, timeout=CANCEL_GRACE_SECONDS)

	def _purge(self):
		now = self.monotonic()
		for key in [k for k, (expiry, _) in self._results.items() if expiry <= now]:
			del self._results[key]

	# Returns (result, refresh): refresh is True when a write may have changed what HA should show.
	async def _handle(self, payload):
		try:
			command = json.loads(payload)
			if not isinstance(command, dict):
				raise ValueError("not an object")
		except (ValueError, TypeError):
			return _result(None, REJECTED, "command is not a JSON object"), False
		cmd_id = command.get("id")
		if not isinstance(cmd_id, str) or not cmd_id:
			return _result(None, REJECTED, "missing command id"), False

		# Under the lock so a redelivery that arrives mid-flight waits, then finds the cached answer instead of writing again.
		async with self._lock:
			self._purge()
			cached = self._results.get(cmd_id)
			if cached:
				return cached[1], False
			refresh = False
			try:
				if self.closing:
					raise Rejected("service is stopping")
				result, refresh = await self._execute(cmd_id, command)
			except Rejected as error:
				result = _result(cmd_id, REJECTED, str(error))
			except asyncio.CancelledError:
				raise
			except Exception as error:
				# Keep the id so HA can correlate; unknown because a write may have landed before the failure.
				logging.exception("Ecobee:%s:command %s crashed", self.device_id, cmd_id)
				result, refresh = _result(cmd_id, UNKNOWN, "internal error: %r" % (error,)), True
			self._results[cmd_id] = (self.monotonic() + RESULT_CACHE_SECONDS, result)
			return result, refresh

	def _check_time(self, ts):
		now = self.clock()
		if ts - now > MAX_FUTURE_SKEW_SECONDS:
			raise Rejected("ts is %.0f s in the future (clock skew)" % (ts - now))
		if now > ts + COMMAND_WINDOW_SECONDS:
			raise Rejected("command expired (%.0f s old, limit %d s)" % (now - ts, COMMAND_WINDOW_SECONDS))

	def _remaining(self):
		budget = _BUDGET.get()
		return IO_TIMEOUT_SECONDS if budget is None else budget.remaining()

	# Takes a factory, not a coroutine, so an exhausted budget never leaves an un-awaited coroutine behind.
	async def _io(self, call):
		timeout = min(IO_TIMEOUT_SECONDS, self._remaining())
		if timeout <= 0:
			raise asyncio.TimeoutError("command time budget used up")
		return await asyncio.wait_for(call(), timeout)

	async def _layout(self):
		# A failure here is before any write, so it is a rejection that keeps the command id, not a crash.
		try:
			if not self.pairing.accessories:
				await self._io(self.pairing.list_accessories_and_characteristics)
			layout = discover_layout(self.pairing.accessories)
		except Exception as error:
			raise Rejected("could not discover the thermostat: %r" % (error,))
		if not layout["thermostat"]:
			raise Rejected("no thermostat service on this accessory")
		return layout

	# Fresh values for the named layout entries (celsius/raw); a missing or errored one is None.
	async def _read(self, idents):
		try:
			values = await self._io(lambda: self.pairing.get_characteristics(set(idents.values())))
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
		return await self._run(cmd_id, ts, steps)

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
			self._require_deadband(_tenths(now["heat"]), _tenths(now["cool"]))
		return [Step("mode", mode_id, MODES[mode], mode, check=_equals(MODES[mode]))]

	def _require_target(self, label, celsius, bounds):
		if celsius is None:
			raise Rejected("cannot read the current %s to check it against the limits" % label)
		_bounded("current " + label, _tenths(celsius), bounds)

	# Both arguments are quantized tenths of C.
	def _require_deadband(self, heat_q, cool_q):
		if cool_q - heat_q < _deadband_tenths():
			raise Rejected("heat %.1f F must be at least %g F below cool %.1f F" % (c_to_f(heat_q / 10), DEADBAND_F, c_to_f(cool_q / 10)))

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
		heat_q = None if heat is None else _quantize(heat)
		cool_q = None if cool is None else _quantize(cool)
		if heat is not None:
			_bounded("heat_setpoint", heat_q, HEAT_BOUNDS_F)
		if cool is not None:
			_bounded("cool_setpoint", cool_q, COOL_BOUNDS_F)

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
		old_h, old_c = _tenths(now["heat"]), _tenths(now["cool"])
		new_h = old_h if heat_q is None else heat_q
		new_c = old_c if cool_q is None else cool_q
		self._require_deadband(new_h, new_c)
		heat_step = Step("heat_setpoint", heat_id, f_to_c(heat), heat, check=_near_f(f_to_c(heat))) if heat is not None else None
		cool_step = Step("cool_setpoint", cool_id, f_to_c(cool), cool, check=_near_f(f_to_c(cool))) if cool is not None else None
		if heat_step and cool_step:
			# Cool first keeps (old heat, new cool) apart; otherwise heat first keeps (new heat, old cool) apart.
			cool_first = old_h <= new_c - _deadband_tenths()
			first, second = (cool_step, heat_step) if cool_first else (heat_step, cool_step)
			# The order only protects the deadband if the first write actually took; the device may ack and ignore it.
			second.guard = ({"heat": heat_id, "cool": cool_id}, self._pair_guard(second is heat_step, new_h, new_c))
			return [first, second]
		return [heat_step or cool_step]

	# Before the second write of a pair, the final target for that side meets the other side as the device holds it now.
	def _pair_guard(self, second_is_heat, new_h, new_c):
		def guard(values):
			if values["heat"] is None or values["cool"] is None:
				return "cannot read the current auto setpoints before the second write"
			heat_q, cool_q = (new_h, _tenths(values["cool"])) if second_is_heat else (_tenths(values["heat"]), new_c)
			try:
				self._require_deadband(heat_q, cool_q)
			except Rejected as error:
				return "second write not issued: %s" % error
			return None
		return guard

	async def _plan_set_fan(self, layout, params):
		_only(params, ["fan"])
		fan = _choice(params, "fan", FAN_VALUES)
		c = layout["control"]
		write_id = self._need(c, "fan_write", "a fan control")
		value = FAN_VALUES[fan]
		check = _equals(value)
		return [Step("fan", write_id, value, fan, read_ident=c.get("fan_read"), check=check if "fan_read" in c else None)]

	# Activating a preset activates the targets it stores, so those are checked first.
	async def _plan_set_preset(self, layout, params):
		_only(params, ["preset"])
		preset = _choice(params, "preset", PRESET_VALUES)
		c = layout["control"]
		hold_id = self._need(c, "set_hold", "a preset control")
		heat_id = self._need(c, "%s_heat" % preset, "the %s preset heat target" % preset)
		cool_id = self._need(c, "%s_cool" % preset, "the %s preset cool target" % preset)
		now = await self._read({"mode": self._need(layout["thermostat"], "mode", "a mode control"), "heat": heat_id, "cool": cool_id})
		self._require_target("%s preset heat target" % preset, now["heat"], HEAT_BOUNDS_F)
		self._require_target("%s preset cool target" % preset, now["cool"], COOL_BOUNDS_F)
		mode = {v: k for k, v in MODES.items()}.get(now["mode"])
		if mode is None:
			raise Rejected("cannot read the current mode to check the preset against it")
		# In auto the preset's heat and cool are both live, so they must keep the deadband; in other modes only one applies.
		if mode == "auto":
			self._require_deadband(_tenths(now["heat"]), _tenths(now["cool"]))
		value = PRESET_VALUES[preset]
		return [Step("preset", hold_id, value, preset, read_ident=c.get("current_mode"),
			check=_equals(value) if "current_mode" in c else None)]

	# Allowed without a bounds check on purpose: it hands control back to the in-laws' own program, not a target Joe chose.
	async def _plan_resume_schedule(self, layout, params):
		_only(params, [])
		clear_id = self._need(layout["control"], "clear_hold", "a clear-hold control")
		return [Step("resume_schedule", clear_id, True, True, trigger=True)]

	# -- execution --

	# Returns (result, refresh). Steps run in order; each dependent write waits until the one before it is verified.
	async def _run(self, cmd_id, ts, steps):
		# Re-checked here, after the pre-reads, because the window is measured to the first write, not to receipt.
		self._check_time(ts)
		if self._remaining() < MIN_BUDGET_TO_WRITE_SECONDS:
			raise Rejected("not enough of the command time budget left to write and confirm")
		wrote_any = False
		for index, step in enumerate(steps):
			if index:
				problem = await self._gate(steps[:index], step)
				if problem is None and self._remaining() < MIN_BUDGET_TO_WRITE_SECONDS:
					problem = "out of time before the next write"
				if problem:
					step.write, step.error = "skipped", problem
					break
			try:
				statuses = await self._io(lambda: self.pairing.put_characteristics([(step.ident[0], step.ident[1], step.value)]))
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
		return _outcome(cmd_id, steps), wrote_any

	# Proves the earlier writes took (readback) and re-checks the deadband against what the device holds now.
	# Returns a reason to stop, or None to go ahead.
	async def _gate(self, done, step):
		if any(s.write != "ok" for s in done):
			return "previous write did not succeed"
		wanted = {s.read_ident for s in done if s.read_ident is not None and s.check is not None}
		guard_idents, guard = step.guard if step.guard else ({}, None)
		wanted |= set(guard_idents.values())
		try:
			got = await self._io(lambda: self.pairing.get_characteristics(wanted))
		except Exception as error:
			logging.warning("Ecobee:%s:could not verify before the next write: %r", self.device_id, error)
			return "could not verify the previous write, so the next was not issued: %r" % (error,)
		_judge(done, got)
		if not all(s.confirmed for s in done):
			return "previous write was not confirmed, so the next was not issued"
		if guard is None:
			return None
		values = {}
		for key, ident in guard_idents.items():
			entry = got.get(ident)
			values[key] = entry["value"] if entry and "status" not in entry and "value" in entry else None
		return guard(values)

	async def _verify(self, steps):
		attempted = [s for s in steps if s.write in ("ok", "unknown") and s.confirmed is None]
		reads = {s.read_ident for s in attempted if s.read_ident is not None and s.check is not None}
		got = {}
		if reads:
			try:
				got = await self._io(lambda: self.pairing.get_characteristics(reads))
			except Exception as error:
				logging.warning("Ecobee:%s:readback failed: %r", self.device_id, error)
		_judge(attempted, got)

	# Runs after the result is published; it only refreshes what HA shows, so a failure costs nothing.
	async def _refresh(self):
		try:
			await self.republish()
		except Exception as error:
			logging.warning("Ecobee:%s:state republish failed: %r", self.device_id, error)

# Sets confirmed/error on each step from a get_characteristics answer; anything unreadable stays None (unverified).
def _judge(steps, got):
	for step in steps:
		if step.check is None:
			step.confirmed = True if (step.trigger and step.write == "ok") else None
			continue
		entry = got.get(step.read_ident)
		if not entry or "status" in entry or "value" not in entry:
			step.confirmed = None
			continue
		step.confirmed = bool(step.check(entry["value"]))
		if not step.confirmed and step.write == "ok":
			step.error = "readback mismatch on %s" % step.key

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
