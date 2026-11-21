import asyncio
import json

import pytest

pytest.importorskip("aiohomekit")

from aiohomekit.model import Accessory, Accessories
from aiohomekit.model.characteristics import CharacteristicsTypes as C
from aiohomekit.model.services import ServicesTypes as S

import ecobee_control
from ecobee_control import Controller, WritablePairing, f_to_c
from ecobee_state import discover_layout

NOW = 1_000_000.0

def _accessories(with_vendor=True, current_mode=True):
	accessories = Accessories()
	t = Accessory.create_with_info(1, "Living Room", "ecobee", "model", "serial", "1.0")
	svc = t.add_service(S.THERMOSTAT)
	for char, value in [(C.TEMPERATURE_CURRENT, 21.0), (C.HEATING_COOLING_TARGET, 3), (C.HEATING_COOLING_CURRENT, 0),
			(C.TEMPERATURE_TARGET, f_to_c(70)), (C.TEMPERATURE_HEATING_THRESHOLD, f_to_c(68)), (C.TEMPERATURE_COOLING_THRESHOLD, f_to_c(76))]:
		svc.add_char(char, value=value)
	if with_vendor:
		vendor = t.add_service("B7DDB9A3-54BB-4572-91D2-F1F5B0510F8C")
		extra = [(C.VENDOR_ECOBEE_CURRENT_MODE, 0)] if current_mode else []
		for char, value in extra + [(C.VENDOR_ECOBEE_FAN_WRITE_SPEED, 0), (C.VENDOR_ECOBEE_FAN_READ_SPEED, 0), (C.VENDOR_ECOBEE_SET_HOLD_SCHEDULE, 0),
				(C.VENDOR_ECOBEE_CLEAR_HOLD, False),
				(C.VENDOR_ECOBEE_HOME_TARGET_HEAT, f_to_c(70)), (C.VENDOR_ECOBEE_HOME_TARGET_COOL, f_to_c(75)),
				(C.VENDOR_ECOBEE_SLEEP_TARGET_HEAT, f_to_c(66)), (C.VENDOR_ECOBEE_SLEEP_TARGET_COOL, f_to_c(78)),
				(C.VENDOR_ECOBEE_AWAY_TARGET_HEAT, f_to_c(40)), (C.VENDOR_ECOBEE_AWAY_TARGET_COOL, f_to_c(92))]:
			vendor.add_char(char, value=value)
	accessories.add_accessory(t)
	return accessories

class FakePairing:
	"""Stateful thermostat: put applies values (so reads see them) unless told to refuse, drift or hang."""
	def __init__(self, accessories=None):
		self.accessories = accessories or _accessories()
		self.state = {(1, c.iid): c.value for s in self.accessories.aid(1).services for c in s.characteristics}
		self.puts = []
		self.refuse = set()      # idents answered with a HAP error
		self.ignore = set()      # idents accepted but not applied (readback mismatch)
		self.raise_on_put = None
		self.put_delay = 0
		self.read_error = False

	async def list_accessories_and_characteristics(self):
		return []

	async def get_characteristics(self, ids):
		if self.read_error:
			raise OSError("link down")
		return {i: {"value": self.state[i]} for i in ids}

	async def put_characteristics(self, chars):
		if self.put_delay:
			await asyncio.sleep(self.put_delay)
		if self.raise_on_put:
			raise self.raise_on_put
		out = {}
		for aid, iid, value in chars:
			ident = (aid, iid)
			self.puts.append((ident, value))
			if ident in self.refuse:
				out[ident] = {"status": -70402, "description": "Unable to communicate"}
				continue
			if ident not in self.ignore:
				self.state[ident] = value
		return out

class Publisher:
	def __init__(self):
		self.results = []

	def publish_result(self, result):
		self.results.append(result)

class Rig:
	def __init__(self, pairing=None, now=NOW):
		self.pairing = pairing or FakePairing()
		self.layout = discover_layout(self.pairing.accessories)
		self.publisher = Publisher()
		self.now = now
		self.republished = 0
		async def republish():
			self.republished += 1
		self.controller = Controller("living-room", WritablePairing(self.pairing), self.publisher, republish, clock=lambda: self.now)

	def t(self, key):
		return self.layout["thermostat"][key]

	def c(self, key):
		return self.layout["control"][key]

	def value(self, ident):
		return self.pairing.state[ident]

	def send(self, command, params=None, id="a", ts=None, raw=None):
		payload = raw if raw is not None else json.dumps({"id": id, "command": command, "parameters": params or {}, "ts": NOW if ts is None else ts})
		asyncio.run(self.controller.handle(payload))
		return self.publisher.results[-1]

def _mode(rig, mode):
	rig.pairing.state[rig.t("mode")] = ecobee_control.MODES[mode]

def _setpoints(rig, heat=None, cool=None, target=None):
	if heat is not None:
		rig.pairing.state[rig.t("heat_threshold")] = f_to_c(heat)
	if cool is not None:
		rig.pairing.state[rig.t("cool_threshold")] = f_to_c(cool)
	if target is not None:
		rig.pairing.state[rig.t("target")] = f_to_c(target)

def _assert_rejected(rig, result, text):
	assert result["outcome"] == "rejected" and text in result["error"], result
	assert rig.pairing.puts == []
	assert rig.republished == 0

def test_layout_exposes_writable_control_ids_without_changing_polled_ids():
	rig = Rig()
	assert {"fan_write", "set_hold", "clear_hold", "home_heat", "away_cool", "current_mode"} <= set(rig.layout["control"])
	assert not set(rig.layout["control"].values()) & set(rig.layout["thermostat"].values())

# -- set_mode / turn_on / turn_off

@pytest.mark.parametrize("mode,code", [("heat", 1), ("cool", 2), ("auto", 3), ("off", 0)])
def test_set_mode_writes_verifies_and_republishes(mode, code):
	rig = Rig()
	_mode(rig, "heat" if mode != "heat" else "cool")
	result = rig.send("set_mode", {"mode": mode})
	assert result == {"id": "a", "outcome": "ok", "error": None, "applied": {"mode": mode}}
	assert rig.pairing.puts == [(rig.t("mode"), code)]
	assert rig.republished == 1

def test_turn_off_and_turn_on_are_deterministic():
	rig = Rig()
	assert rig.send("turn_off", id="1")["outcome"] == "ok" and rig.value(rig.t("mode")) == 0
	assert rig.send("turn_on", id="2")["outcome"] == "ok" and rig.value(rig.t("mode")) == 1

def test_bad_mode_value_is_rejected():
	rig = Rig()
	_assert_rejected(rig, rig.send("set_mode", {"mode": "emergency"}), "mode must be")
	_assert_rejected(rig, rig.send("set_mode", {"mode": ["heat"]}, id="b"), "mode must be")

@pytest.mark.parametrize("target_f,mode", [(80, "heat"), (44, "heat"), (64, "cool"), (91, "cool")])
def test_mode_change_rejected_when_the_target_it_activates_is_out_of_bounds(target_f, mode):
	rig = Rig()
	_mode(rig, "off")
	_setpoints(rig, target=target_f)
	_assert_rejected(rig, rig.send("set_mode", {"mode": mode}), "outside the allowed")

def test_turn_on_rejected_when_current_target_is_hot():
	rig = Rig()
	_mode(rig, "off")
	_setpoints(rig, target=80)
	_assert_rejected(rig, rig.send("turn_on"), "outside the allowed")

def test_auto_activation_checks_thresholds_and_deadband():
	rig = Rig()
	_mode(rig, "heat")
	_setpoints(rig, heat=74, cool=76)
	_assert_rejected(rig, rig.send("set_mode", {"mode": "auto"}), "at least 3 F below")

def test_mode_change_rejected_when_the_target_cannot_be_read():
	rig = Rig()
	rig.pairing.read_error = True
	_assert_rejected(rig, rig.send("turn_on"), "could not read")

# -- set_setpoint

def test_heat_mode_setpoint_writes_target_temperature():
	rig = Rig()
	_mode(rig, "heat")
	result = rig.send("set_setpoint", {"heat_setpoint": 68})
	assert result["outcome"] == "ok" and result["applied"] == {"heat_setpoint": 68.0}
	assert rig.pairing.puts == [(rig.t("target"), f_to_c(68))]

def test_cool_mode_setpoint_writes_target_temperature():
	rig = Rig()
	_mode(rig, "cool")
	assert rig.send("set_setpoint", {"cool_setpoint": 74})["outcome"] == "ok"
	assert rig.pairing.puts == [(rig.t("target"), f_to_c(74))]

def test_param_that_does_not_apply_to_the_mode_is_rejected():
	rig = Rig()
	_mode(rig, "heat")
	_assert_rejected(rig, rig.send("set_setpoint", {"cool_setpoint": 74}), "does not apply in heat mode")
	_mode(rig, "cool")
	_assert_rejected(rig, rig.send("set_setpoint", {"heat_setpoint": 68, "cool_setpoint": 74}, id="b"), "does not apply in cool mode")

def test_setpoint_in_off_mode_is_rejected():
	rig = Rig()
	_mode(rig, "off")
	_assert_rejected(rig, rig.send("set_setpoint", {"heat_setpoint": 68}), "thermostat is off")

@pytest.mark.parametrize("params", [{"heat_setpoint": 44}, {"heat_setpoint": 79}, {"cool_setpoint": 64}, {"cool_setpoint": 91}])
def test_direct_setpoint_bounds_rejected(params):
	rig = Rig()
	_assert_rejected(rig, rig.send("set_setpoint", params), "outside the allowed")

def test_setpoint_bounds_are_inclusive():
	rig = Rig()
	assert rig.send("set_setpoint", {"heat_setpoint": 45, "cool_setpoint": 90}, id="1")["outcome"] == "ok"
	assert rig.send("set_setpoint", {"heat_setpoint": 78, "cool_setpoint": 90}, id="2")["outcome"] == "ok"

@pytest.mark.parametrize("params", [{}, {"heat_setpoint": "68"}, {"heat_setpoint": True}, {"heat_setpoint": float("nan")}, {"heat": 68}])
def test_malformed_setpoint_params_rejected(params):
	rig = Rig()
	assert rig.send("set_setpoint", params)["outcome"] == "rejected"
	assert rig.pairing.puts == []

def test_auto_deadband_violation_rejected_using_the_unchanged_side():
	rig = Rig()
	_setpoints(rig, heat=68, cool=76)
	_assert_rejected(rig, rig.send("set_setpoint", {"heat_setpoint": 74}), "at least 3 F below")

def test_auto_raising_writes_cool_first_and_never_breaks_the_deadband():
	rig = Rig()
	_setpoints(rig, heat=68, cool=72)
	seen = []
	original = rig.pairing.put_characteristics
	async def spy(chars):
		out = await original(chars)
		seen.append((c_to_f_state(rig, "heat_threshold"), c_to_f_state(rig, "cool_threshold")))
		return out
	rig.pairing.put_characteristics = spy
	result = rig.send("set_setpoint", {"heat_setpoint": 74, "cool_setpoint": 78})
	assert result["outcome"] == "ok"
	assert [ident for ident, _ in rig.pairing.puts] == [rig.t("cool_threshold"), rig.t("heat_threshold")]
	assert all(c - h >= 3 - 0.2 for h, c in seen)

def test_auto_lowering_writes_heat_first_and_never_breaks_the_deadband():
	rig = Rig()
	_setpoints(rig, heat=70, cool=76)
	seen = []
	original = rig.pairing.put_characteristics
	async def spy(chars):
		out = await original(chars)
		seen.append((c_to_f_state(rig, "heat_threshold"), c_to_f_state(rig, "cool_threshold")))
		return out
	rig.pairing.put_characteristics = spy
	assert rig.send("set_setpoint", {"heat_setpoint": 60, "cool_setpoint": 66})["outcome"] == "ok"
	assert [ident for ident, _ in rig.pairing.puts] == [rig.t("heat_threshold"), rig.t("cool_threshold")]
	assert all(c - h >= 3 - 0.2 for h, c in seen)

def c_to_f_state(rig, key):
	return ecobee_control.c_to_f(rig.value(rig.t(key)))

def test_auto_pair_stops_at_first_failure_and_reports_partial():
	rig = Rig()
	_setpoints(rig, heat=68, cool=72)
	rig.pairing.refuse = {rig.t("heat_threshold")}
	result = rig.send("set_setpoint", {"heat_setpoint": 74, "cool_setpoint": 78})
	assert result["outcome"] == "partial" and result["applied"] == {"cool_setpoint": 78.0}
	assert "device refused" in result["error"]
	assert len(rig.pairing.puts) == 2 and rig.republished == 1

def test_first_write_refused_is_failed_and_still_republishes():
	rig = Rig()
	_mode(rig, "heat")
	rig.pairing.refuse = {rig.t("target")}
	result = rig.send("set_setpoint", {"heat_setpoint": 68})
	assert result["outcome"] == "failed" and result["applied"] == {}
	assert rig.republished == 1

def test_stop_after_first_failure_does_not_attempt_second_write():
	rig = Rig()
	_setpoints(rig, heat=68, cool=72)
	rig.pairing.refuse = {rig.t("cool_threshold")}
	result = rig.send("set_setpoint", {"heat_setpoint": 74, "cool_setpoint": 78})
	assert result["outcome"] == "failed"
	assert [ident for ident, _ in rig.pairing.puts] == [rig.t("cool_threshold")]

def test_readback_mismatch_is_not_ok():
	rig = Rig()
	_mode(rig, "heat")
	rig.pairing.ignore = {rig.t("target")}
	result = rig.send("set_setpoint", {"heat_setpoint": 68})
	assert result["outcome"] == "failed" and "readback mismatch" in result["error"]

def test_readback_within_device_precision_is_ok():
	rig = Rig()
	_mode(rig, "heat")
	original = rig.pairing.put_characteristics
	async def drift(chars):
		out = await original(chars)
		rig.pairing.state[rig.t("target")] = f_to_c(68.3)
		return out
	rig.pairing.put_characteristics = drift
	assert rig.send("set_setpoint", {"heat_setpoint": 68})["outcome"] == "ok"

def test_readback_failure_after_a_good_write_is_unknown_not_failed():
	rig = Rig()
	_mode(rig, "heat")
	original = rig.pairing.put_characteristics
	async def then_link_drops(chars):
		out = await original(chars)
		rig.pairing.read_error = True
		return out
	rig.pairing.put_characteristics = then_link_drops
	assert rig.send("set_setpoint", {"heat_setpoint": 68})["outcome"] == "unknown"

def test_write_that_raises_is_unknown_and_republishes():
	rig = Rig()
	_mode(rig, "heat")
	rig.pairing.raise_on_put = OSError("reset")
	result = rig.send("set_setpoint", {"heat_setpoint": 68})
	assert result["outcome"] == "unknown" and "not confirmed" in result["error"]
	assert rig.republished == 1

def test_write_that_hangs_times_out_as_unknown(monkeypatch):
	monkeypatch.setattr(ecobee_control, "IO_TIMEOUT_SECONDS", 0.05)
	rig = Rig()
	_mode(rig, "heat")
	rig.pairing.put_delay = 5
	assert rig.send("set_setpoint", {"heat_setpoint": 68})["outcome"] == "unknown"

# -- fan, preset, resume

# The fake follows aiohomekit's documented encoding (100 on, 0 off/auto) instead of mirroring whatever it is given, and the
# expected values are literals, so a wrong constant fails here rather than being echoed back.
def _documented_fan(rig):
	original = rig.pairing.put_characteristics
	async def device(chars):
		out = await original(chars)
		rig.pairing.state[rig.c("fan_read")] = 100 if rig.value(rig.c("fan_write")) == 100 else 0
		return out
	rig.pairing.put_characteristics = device

@pytest.mark.parametrize("fan,written", [("on", 100), ("auto", 0)])
def test_set_fan_uses_the_documented_encoding(fan, written):
	rig = Rig()
	_documented_fan(rig)
	rig.pairing.state[rig.c("fan_read")] = 0 if fan == "on" else 100
	rig.pairing.state[rig.c("fan_write")] = 0 if fan == "on" else 100
	result = rig.send("set_fan", {"fan": fan})
	assert result["outcome"] == "ok" and result["applied"] == {"fan": fan}
	assert rig.pairing.puts == [(rig.c("fan_write"), written)]

def test_fan_on_is_not_confirmed_by_a_nonzero_wrong_speed():
	rig = Rig()
	original = rig.pairing.put_characteristics
	async def odd_device(chars):
		out = await original(chars)
		rig.pairing.state[rig.c("fan_read")] = 1
		return out
	rig.pairing.put_characteristics = odd_device
	assert rig.send("set_fan", {"fan": "on"})["outcome"] == "failed"

def test_set_fan_rejects_other_values():
	rig = Rig()
	_assert_rejected(rig, rig.send("set_fan", {"fan": "high"}), "fan must be")

def _preset_rig():
	rig = Rig()
	original = rig.pairing.put_characteristics
	async def mirror(chars):
		out = await original(chars)
		rig.pairing.state[rig.c("current_mode")] = rig.value(rig.c("set_hold"))
		return out
	rig.pairing.put_characteristics = mirror
	return rig

def test_set_preset_in_bounds():
	rig = _preset_rig()
	result = rig.send("set_preset", {"preset": "sleep"})
	assert result["outcome"] == "ok" and result["applied"] == {"preset": "sleep"}
	assert rig.pairing.puts == [(rig.c("set_hold"), 1)]

def test_set_preset_rejected_when_its_stored_targets_are_out_of_bounds():
	rig = _preset_rig()
	_assert_rejected(rig, rig.send("set_preset", {"preset": "away"}), "preset heat target")
	rig.pairing.state[rig.c("away_heat")] = f_to_c(60)
	_assert_rejected(rig, rig.send("set_preset", {"preset": "away"}, id="b"), "preset cool target")

def test_set_preset_rejected_when_stored_targets_are_unreadable():
	rig = _preset_rig()
	rig.pairing.read_error = True
	_assert_rejected(rig, rig.send("set_preset", {"preset": "home"}), "could not read")

def test_set_preset_without_a_readback_characteristic_is_unknown_not_ok():
	rig = Rig(FakePairing(_accessories(current_mode=False)))
	assert "current_mode" not in rig.layout["control"]
	result = rig.send("set_preset", {"preset": "home"})
	assert result["outcome"] == "unknown" and len(rig.pairing.puts) == 1

def test_unsupported_preset_rejected():
	rig = _preset_rig()
	_assert_rejected(rig, rig.send("set_preset", {"preset": "vacation"}), "preset must be")

def test_resume_schedule_needs_no_bounds_check():
	rig = Rig()
	_mode(rig, "heat")
	_setpoints(rig, target=95)   # would fail any bounds check; resume hands back to the in-laws' program
	result = rig.send("resume_schedule")
	assert result["outcome"] == "ok" and result["applied"] == {"resume_schedule": True}
	assert rig.pairing.puts == [(rig.c("clear_hold"), True)]

def test_missing_vendor_characteristics_reject_instead_of_crashing():
	rig = Rig(FakePairing(_accessories(with_vendor=False)))
	for command, params in [("set_fan", {"fan": "on"}), ("set_preset", {"preset": "home"}), ("resume_schedule", {})]:
		assert rig.send(command, params, id=command)["outcome"] == "rejected"
	assert rig.pairing.puts == []

# -- timing

def test_expired_command_is_rejected_without_writing():
	rig = Rig(now=NOW + 21)
	_assert_rejected(rig, rig.send("turn_off", ts=NOW), "expired")

def test_command_at_the_edge_of_the_window_still_runs():
	rig = Rig(now=NOW + 20)
	assert rig.send("turn_off", ts=NOW)["outcome"] == "ok"

def test_future_skewed_ts_is_rejected():
	rig = Rig(now=NOW - 6)
	_assert_rejected(rig, rig.send("turn_off", ts=NOW), "future")

def test_small_future_skew_is_tolerated():
	rig = Rig(now=NOW - 5)
	assert rig.send("turn_off", ts=NOW)["outcome"] == "ok"

def test_deadline_is_rechecked_right_before_the_first_write():
	rig = Rig()
	_mode(rig, "heat")
	original = rig.pairing.get_characteristics
	async def slow_preread(ids):
		rig.now = NOW + 31       # the pre-read ate the window
		return await original(ids)
	rig.pairing.get_characteristics = slow_preread
	_assert_rejected(rig, rig.send("set_setpoint", {"heat_setpoint": 68}), "expired")

@pytest.mark.parametrize("ts", [None, "now", True])
def test_missing_or_bad_ts_rejected(ts):
	rig = Rig()
	payload = {"id": "a", "command": "turn_off", "parameters": {}}
	if ts is not None:
		payload["ts"] = ts
	_assert_rejected(rig, rig.send("x", raw=json.dumps(payload)), "ts")

# -- envelope, duplicates, serialization

@pytest.mark.parametrize("raw", [b"not json", b"[1]", json.dumps({"command": "turn_off", "ts": NOW})])
def test_malformed_envelope_rejected(raw):
	rig = Rig()
	result = rig.send("x", raw=raw)
	assert result["outcome"] == "rejected" and result["id"] is None
	assert rig.pairing.puts == []

def test_unknown_command_rejected():
	rig = Rig()
	_assert_rejected(rig, rig.send("reboot"), "unknown command")
	_assert_rejected(rig, rig.send("__init__", id="b"), "unknown command")

def test_duplicate_id_returns_cached_result_without_a_second_write():
	rig = Rig()
	first = rig.send("turn_off", id="dup")
	_mode(rig, "heat")
	second = rig.send("turn_off", id="dup")
	assert second == first and len(rig.pairing.puts) == 1
	assert len(rig.publisher.results) == 2

def test_cached_rejection_is_also_replayed():
	rig = Rig(now=NOW + 99)
	rig.send("turn_off", id="dup")
	rig.now = NOW
	assert rig.send("turn_off", id="dup")["outcome"] == "rejected"
	assert rig.pairing.puts == []

def test_cache_entry_expires_after_ten_minutes():
	rig = Rig()
	clock = [0.0]
	rig.controller.monotonic = lambda: clock[0]
	rig.send("turn_off", id="x")
	clock[0] = 601
	rig.send("turn_off", id="x", ts=rig.now)
	assert len(rig.pairing.puts) == 2

def test_concurrent_redelivery_writes_once_and_commands_serialize():
	rig = Rig()
	rig.pairing.put_delay = 0.05
	active, peak = [0], [0]
	original = rig.pairing.put_characteristics
	async def tracked(chars):
		active[0] += 1
		peak[0] = max(peak[0], active[0])
		try:
			return await original(chars)
		finally:
			active[0] -= 1
	rig.pairing.put_characteristics = tracked
	def cmd(i, command):
		return json.dumps({"id": i, "command": command, "parameters": {}, "ts": NOW})
	async def go():
		await asyncio.gather(*(rig.controller.handle(p) for p in
			[cmd("a", "turn_off"), cmd("a", "turn_off"), cmd("b", "turn_on"), cmd("c", "turn_off")]))
	asyncio.run(go())
	assert peak[0] == 1
	assert [v for _, v in rig.pairing.puts] == [0, 1, 0]
	assert len(rig.publisher.results) == 4
	assert rig.publisher.results[0] == rig.publisher.results[1]

def test_a_stopping_service_rejects_new_commands():
	rig = Rig()
	rig.controller.closing = True
	_assert_rejected(rig, rig.send("turn_off"), "stopping")

def test_drain_lets_an_inflight_command_finish_then_cancels_stragglers():
	rig = Rig()
	rig.pairing.put_delay = 0.05
	async def go():
		task = asyncio.create_task(rig.controller.handle(json.dumps({"id": "a", "command": "turn_off", "parameters": {}, "ts": NOW})))
		await asyncio.sleep(0.01)
		await rig.controller.drain(2)
		assert task.done() and not task.cancelled()
	asyncio.run(go())
	assert rig.publisher.results[0]["outcome"] == "ok"

def test_republish_failure_does_not_lose_the_result():
	rig = Rig()
	async def boom():
		raise RuntimeError("mqtt")
	rig.controller.republish = boom
	assert rig.send("turn_off")["outcome"] == "ok"

def test_controller_is_the_only_module_that_writes():
	import ast, os
	repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
	writers = []
	for fname in os.listdir(repo):
		if fname.endswith(".py"):
			tree = ast.parse(open(os.path.join(repo, fname)).read())
			if any(isinstance(n, ast.Attribute) and n.attr == "put_characteristics" for n in ast.walk(tree)):
				writers.append(fname)
	assert writers == ["ecobee_control.py"]


# -- rework of the first review (#1878)

def _spy_pairs(rig):
	seen = []
	original = rig.pairing.put_characteristics
	async def spy(chars):
		out = await original(chars)
		seen.append((c_to_f_state(rig, "heat_threshold"), c_to_f_state(rig, "cool_threshold")))
		return out
	rig.pairing.put_characteristics = spy
	return seen

def test_ignored_first_write_stops_before_the_dependent_write():
	rig = Rig()
	_setpoints(rig, heat=68, cool=72)
	rig.pairing.ignore = {rig.t("cool_threshold")}   # acked, not applied
	result = rig.send("set_setpoint", {"heat_setpoint": 74, "cool_setpoint": 78})
	assert [ident for ident, _ in rig.pairing.puts] == [rig.t("cool_threshold")]
	assert result["outcome"] == "failed" and "readback mismatch" in result["error"]
	assert c_to_f_state(rig, "cool_threshold") - c_to_f_state(rig, "heat_threshold") >= 3.9
	assert rig.republished == 1

def test_deadband_is_rechecked_against_the_value_the_device_actually_holds():
	rig = Rig()
	_setpoints(rig, heat=68, cool=72)
	original = rig.pairing.put_characteristics
	async def rounds_down(chars):
		out = await original(chars)
		rig.pairing.state[rig.t("cool_threshold")] = f_to_c(77.7)   # within readback tolerance of 78, but only 2.7 F above heat 75
		return out
	rig.pairing.put_characteristics = rounds_down
	result = rig.send("set_setpoint", {"heat_setpoint": 75, "cool_setpoint": 78})
	assert [ident for ident, _ in rig.pairing.puts] == [rig.t("cool_threshold")]
	assert result["outcome"] == "partial" and "second write not issued" in result["error"]
	assert result["applied"] == {"cool_setpoint": 78.0}

def test_unverifiable_first_write_does_not_issue_the_second():
	rig = Rig()
	_setpoints(rig, heat=68, cool=72)
	original = rig.pairing.put_characteristics
	async def then_link_drops(chars):
		out = await original(chars)
		rig.pairing.read_error = True
		return out
	rig.pairing.put_characteristics = then_link_drops
	result = rig.send("set_setpoint", {"heat_setpoint": 74, "cool_setpoint": 78})
	assert len(rig.pairing.puts) == 1 and result["outcome"] == "unknown"

def test_78f_survives_off_then_on():
	rig = Rig()
	_mode(rig, "heat")
	assert rig.send("set_setpoint", {"heat_setpoint": 78}, id="1")["outcome"] == "ok"
	assert rig.send("turn_off", id="2")["outcome"] == "ok"
	assert rig.send("turn_on", id="3")["outcome"] == "ok"
	assert rig.value(rig.t("mode")) == 1

@pytest.mark.parametrize("f", [78.04, 44.96])
def test_requests_are_judged_after_quantizing_to_the_device_step(f):
	rig = Rig()
	_mode(rig, "heat")
	assert rig.send("set_setpoint", {"heat_setpoint": f})["outcome"] == "ok"

@pytest.mark.parametrize("f", [78.2, 44.8])
def test_requests_that_quantize_outside_the_bounds_are_rejected(f):
	rig = Rig()
	_mode(rig, "heat")
	_assert_rejected(rig, rig.send("set_setpoint", {"heat_setpoint": f}), "outside the allowed")

@pytest.mark.parametrize("heat", [66, 67, 68, 69, 70.5, 71])
def test_exact_deadband_survives_quantization(heat):
	rig = Rig()
	_setpoints(rig, heat=60, cool=90)
	assert rig.send("set_setpoint", {"heat_setpoint": heat, "cool_setpoint": heat + 3})["outcome"] == "ok"

def test_deadband_just_under_three_degrees_is_rejected():
	rig = Rig()
	_setpoints(rig, heat=60, cool=90)
	_assert_rejected(rig, rig.send("set_setpoint", {"heat_setpoint": 68, "cool_setpoint": 70.5}), "at least 3 F below")

def test_auto_preset_violating_the_deadband_is_rejected_before_any_write():
	rig = _preset_rig()
	rig.pairing.state[rig.c("home_heat")] = f_to_c(70)
	rig.pairing.state[rig.c("home_cool")] = f_to_c(72)
	_assert_rejected(rig, rig.send("set_preset", {"preset": "home"}), "at least 3 F below")

def test_preset_deadband_is_not_applied_outside_auto():
	rig = _preset_rig()
	_mode(rig, "heat")
	rig.pairing.state[rig.c("home_heat")] = f_to_c(70)
	rig.pairing.state[rig.c("home_cool")] = f_to_c(72)
	assert rig.send("set_preset", {"preset": "home"})["outcome"] == "ok"

def test_discovery_failure_is_a_rejection_that_keeps_the_command_id():
	pairing = FakePairing()
	rig = Rig(pairing)
	async def broken():
		raise OSError("not connected")
	rig.pairing.accessories = []
	rig.pairing.list_accessories_and_characteristics = broken
	result = rig.send("turn_off", id="keepme")
	assert result["id"] == "keepme" and result["outcome"] == "rejected" and "discover" in result["error"]
	assert rig.pairing.puts == [] and rig.republished == 0
	assert rig.send("turn_off", id="keepme") == result   # cached, correlated

def test_discovery_timeout_is_a_rejection(monkeypatch):
	monkeypatch.setattr(ecobee_control, "IO_TIMEOUT_SECONDS", 0.05)
	rig = Rig()
	async def hang():
		await asyncio.sleep(5)
	rig.pairing.accessories = []
	rig.pairing.list_accessories_and_characteristics = hang
	result = rig.send("turn_off", id="t")
	assert result["id"] == "t" and result["outcome"] == "rejected"

def test_result_is_published_before_the_state_refresh_and_not_delayed_by_it():
	rig = Rig()
	order = []
	original = rig.publisher.publish_result
	rig.publisher.publish_result = lambda r: (order.append("result"), original(r))
	async def slow_refresh():
		order.append("refresh-start")
		await asyncio.sleep(0.2)
		order.append("refresh-end")
	rig.controller.republish = slow_refresh
	rig.send("turn_off")
	assert order == ["result", "refresh-start", "refresh-end"]

def test_rejected_and_cached_answers_trigger_no_refresh():
	rig = Rig()
	rig.send("turn_off", id="x")
	rig.send("turn_off", id="x")
	rig.send("set_mode", {"mode": "bad"}, id="y")
	assert rig.republished == 1

def test_refresh_is_skipped_once_the_service_is_stopping():
	rig = Rig()
	original = rig.controller._execute
	async def execute_then_stop(*a):
		out = await original(*a)
		rig.controller.closing = True
		return out
	rig.controller._execute = execute_then_stop
	assert rig.send("turn_off")["outcome"] == "ok"
	assert rig.republished == 0

def test_shared_budget_bounds_the_result_across_many_slow_round_trips(monkeypatch):
	import time
	monkeypatch.setattr(ecobee_control, "COMMAND_BUDGET_SECONDS", 0.6)
	monkeypatch.setattr(ecobee_control, "IO_TIMEOUT_SECONDS", 0.5)
	monkeypatch.setattr(ecobee_control, "MIN_BUDGET_TO_WRITE_SECONDS", 0.1)
	rig = Rig()
	_setpoints(rig, heat=68, cool=72)
	original_get = rig.pairing.get_characteristics
	original_put = rig.pairing.put_characteristics
	async def slow_get(ids):
		await asyncio.sleep(0.2)
		return await original_get(ids)
	async def slow_put(chars):
		await asyncio.sleep(0.2)
		return await original_put(chars)
	rig.pairing.get_characteristics, rig.pairing.put_characteristics = slow_get, slow_put
	started = time.monotonic()
	result = rig.send("set_setpoint", {"heat_setpoint": 74, "cool_setpoint": 78})
	assert time.monotonic() - started < 0.9     # five round trips at 0.2 s would be 1.0 s unbudgeted
	assert result["outcome"] in ("partial", "unknown", "failed")

def test_a_command_with_no_budget_left_is_rejected_before_writing(monkeypatch):
	monkeypatch.setattr(ecobee_control, "MIN_BUDGET_TO_WRITE_SECONDS", 999)
	rig = Rig()
	_assert_rejected(rig, rig.send("turn_off"), "time budget")

def test_drain_cancels_a_command_stuck_in_readback_without_republishing():
	import time
	rig = Rig()
	_mode(rig, "heat")
	original = rig.pairing.get_characteristics
	calls = [0]
	async def readback_hangs(ids):
		calls[0] += 1
		if calls[0] >= 2:       # first call is the planner's pre-read; the next is the readback
			await asyncio.sleep(30)
		return await original(ids)
	rig.pairing.get_characteristics = readback_hangs
	async def go():
		task = asyncio.create_task(rig.controller.handle(json.dumps(
			{"id": "a", "command": "set_setpoint", "parameters": {"heat_setpoint": 68}, "ts": NOW})))
		await asyncio.sleep(0.05)
		rig.controller.closing = True
		started = time.monotonic()
		await rig.controller.drain(0.1)
		assert time.monotonic() - started < 0.1 + ecobee_control.CANCEL_GRACE_SECONDS + 0.5
		assert task.cancelled()
	asyncio.run(go())
	assert rig.republished == 0

def test_drain_gives_up_on_a_task_that_ignores_cancellation(monkeypatch):
	import time
	monkeypatch.setattr(ecobee_control, "CANCEL_GRACE_SECONDS", 0.1)
	rig = Rig()
	async def stubborn():
		while True:
			try:
				await asyncio.sleep(30)
			except asyncio.CancelledError:
				pass
	async def go():
		task = asyncio.ensure_future(stubborn())
		rig.controller._inflight.add(task)
		started = time.monotonic()
		await rig.controller.drain(0.05)
		assert time.monotonic() - started < 0.5
		task.cancel()
		task.add_done_callback(lambda t: None)
	loop = asyncio.new_event_loop()
	try:
		loop.run_until_complete(go())
	finally:
		for t in asyncio.all_tasks(loop):
			t.cancel()
		loop.close()
