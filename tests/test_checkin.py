import json
import logging
from unittest import mock

import pytest
import checkin
from checkin import CheckinBridge, ThermostatState, c_to_f
from config import load_config, ConfigError

NOW = 1_800_000_000.0
TOKEN = "s3cret-device-token"

NAME = "crawlspace_thermostat"
SETTINGS = dict(floor_f=45.0, ceiling_f=65.0, unit="c", allowed_modes=["heat"])
CFG = dict(device_id="crawlspace", checkin_url="https://gergy-edge.example/devices/checkin", checkin_token=TOKEN,
	thermostats={NAME: SETTINGS}, local_mqtt_host="127.0.0.1", broker_port=1883, client_id=None)

class FakeSensor:
	def __init__(self, read_at):
		self.read_at = read_at
	def get_last_read_at(self): return self.read_at
	def get_sensor_data(self): return {"temperature_f": 41.2, "temperature_c": 5.1, "humidity": 63.0}

class Clock:
	def __init__(self, t=NOW): self.t = t
	def __call__(self): return self.t

def make(read_at=NOW - 5, post=None, unit=None, **cfg):
	if unit:
		cfg["thermostats"] = {NAME: {**SETTINGS, "unit": unit}}
	clock = Clock()
	client = mock.MagicMock()
	client.publish.return_value.rc = 0
	bridge = CheckinBridge({**CFG, **cfg}, FakeSensor(read_at), post=post or mock.Mock(return_value={"ok": True}),
		clock=clock, mqtt_client=client)
	return bridge, client, clock

def state_msg(bridge, payload, topic=None, retain=False):
	m = mock.Mock(topic=topic or "zigbee2mqtt/" + NAME, payload=json.dumps(payload).encode(), retain=retain)
	bridge.on_message(None, None, m)

def online(bridge, name=NAME):
	bridge.thermostats[name].state.on_availability(b"online")

def desired(version=7, **fields):
	return {"ok": True, "desired": {NAME: {"version": version, **fields}}}

def applied(version, result):
	return {NAME: {"version": version, "result": result}}

# The thermostat echoing its new setpoint, as zigbee2mqtt does after a /set
def echo(bridge, clock, setpoint_c, dt=1):
	clock.t += dt
	state_msg(bridge, {"occupied_heating_setpoint": setpoint_c})

THERMO = {"local_temperature": 7.0, "occupied_heating_setpoint": 7.5, "system_mode": "heat",
	"running_state": "heat", "pi_heating_demand": 40}

# ---- config

BASE = "[sensor]\nsensor_name=s\nlog_path=/tmp/x/l.log\n"
CHECKIN = BASE + "transport=checkin\ncheckin_url=https://h/devices/checkin\ncheckin_token=t\ndevice_id=crawlspace\n"

def cfg_from(tmp_path, text, env=None):
	p = tmp_path / "c.ini"; p.write_text(text)
	return load_config(str(p), environ=env or {})

def test_mqtt_mode_unchanged_and_still_requires_broker(tmp_path):
	with pytest.raises(ConfigError, match="broker_host"):
		cfg_from(tmp_path, BASE + "client_id=c\n")
	cfg = cfg_from(tmp_path, BASE + "client_id=c\nbroker_host=h\n")
	assert cfg["transport"] == "mqtt" and cfg["device_id"] == "Attic01ae"

def test_checkin_mode_needs_no_broker_but_needs_url_token_device(tmp_path):
	cfg = cfg_from(tmp_path, CHECKIN)
	assert cfg["transport"] == "checkin" and cfg["broker_host"] is None
	assert cfg["local_mqtt_host"] == "127.0.0.1"
	assert cfg["thermostats"] == {NAME: {"floor_f": 45.0, "ceiling_f": 65.0, "unit": "c", "allowed_modes": ["heat"]}}
	with pytest.raises(ConfigError) as e:
		cfg_from(tmp_path, BASE + "transport=checkin\n")
	assert all(k in str(e.value) for k in ("checkin_url", "checkin_token", "device_id"))

def test_checkin_device_id_has_no_bedroom_default(tmp_path):
	with pytest.raises(ConfigError, match="device_id"):
		cfg_from(tmp_path, CHECKIN.replace("device_id=crawlspace\n", ""))

def test_token_from_env_wins(tmp_path):
	assert cfg_from(tmp_path, CHECKIN, {"SMARTSENSOR_CHECKIN_TOKEN": "envtok"})["checkin_token"] == "envtok"

def test_bad_config_values_raise(tmp_path):
	with pytest.raises(ConfigError, match="transport"):
		cfg_from(tmp_path, CHECKIN, {"SMARTSENSOR_TRANSPORT": "carrier-pigeon"})
	for bad, match in (("setpoint_floor_f=hot\n", "setpoint"),
			("setpoint_floor_f=70\n", "below"), ("thermostat_unit=k\n", "thermostat_unit"),
			("thermostat_allowed_modes=heat,turbo\n", "allowed_modes"), ("thermostat_allowed_modes=,\n", "allowed_modes")):
		with pytest.raises(ConfigError, match=match):
			cfg_from(tmp_path, CHECKIN + bad)
	with pytest.raises(ConfigError, match="https"):
		cfg_from(tmp_path, CHECKIN.replace("https://h", "http://h"))
	assert cfg_from(tmp_path, CHECKIN.replace("https://h", "http://127.0.0.1:5050"))["transport"] == "checkin"

def test_per_thermostat_sections_override_the_defaults(tmp_path):
	cfg = cfg_from(tmp_path, CHECKIN + "thermostat_name=a, b\nsetpoint_floor_f=50\n"
		"[thermostat:b]\nsetpoint_ceiling_f=80\nthermostat_allowed_modes=heat,off\nthermostat_unit=f\n")
	assert cfg["thermostats"] == {"a": {"floor_f": 50.0, "ceiling_f": 65.0, "unit": "c", "allowed_modes": ["heat"]},
		"b": {"floor_f": 50.0, "ceiling_f": 80.0, "unit": "f", "allowed_modes": ["heat", "off"]}}

# ---- body shape and freshness

def test_body_shape_matches_contract():
	bridge, _, _ = make()
	state_msg(bridge, THERMO)
	online(bridge)
	body = bridge.build_body(NOW)
	assert body["device_id"] == "crawlspace" and isinstance(body["seq"], int)
	assert body["ts"].endswith("Z") and "applied" not in body
	dht = body["readings"]["dht22"]
	assert dht == {"temperature_f": 41.2, "temperature_c": 5.1, "humidity": 63, "valid": True,
		"reading_at": checkin.iso_utc(NOW - 5)}
	assert body["readings"]["thermostats"][NAME] == {"available": True, "local_temperature_f": 44.6, "setpoint_f": 45.5,
		"system_mode": "heat", "running_state": "heat", "heating_demand_pct": 40, "reading_at": checkin.iso_utc(NOW)}
	json.dumps(body)

def test_stale_dht22_is_marked_invalid_with_its_own_reading_at():
	bridge, _, _ = make(read_at=NOW - 181)
	dht = bridge.build_body(NOW)["readings"]["dht22"]
	assert dht["valid"] is False and dht["reading_at"] == checkin.iso_utc(NOW - 181)
	bridge.sensor.read_at = NOW - 180
	assert bridge.build_body(NOW)["readings"]["dht22"]["valid"] is True

def test_dht22_and_thermostat_omitted_without_a_reading():
	bridge, _, _ = make(read_at=0.0)
	assert bridge.build_body(NOW)["readings"] == {}

def test_future_dht_read_is_invalid():
	bridge, _, _ = make(read_at=NOW + 3600)
	assert bridge.build_body(NOW)["readings"]["dht22"]["valid"] is False

def test_unknown_availability_reported_unavailable():
	bridge, _, _ = make()
	state_msg(bridge, THERMO)
	assert bridge.build_body(NOW)["readings"]["thermostats"][NAME]["available"] is False
	bridge.on_message(None, None, mock.Mock(topic="zigbee2mqtt/" + NAME + "/availability", payload=b'{"state":"online"}', retain=True))
	assert bridge.build_body(NOW)["readings"]["thermostats"][NAME]["available"] is True
	bridge.on_message(None, None, mock.Mock(topic="zigbee2mqtt/" + NAME + "/availability", payload=b"offline", retain=True))
	assert bridge.build_body(NOW)["readings"]["thermostats"][NAME]["available"] is False

def test_retained_state_without_last_seen_is_not_used_as_a_reading():
	bridge, _, _ = make()
	state_msg(bridge, THERMO, retain=True)
	assert "thermostats" not in bridge.build_body(NOW)["readings"]
	state_msg(bridge, {**THERMO, "last_seen": "2026-10-24T01:00:00Z"}, retain=True)
	assert bridge.build_body(NOW)["readings"]["thermostats"][NAME]["reading_at"] == "2026-10-24T01:00:00Z"

def test_partial_update_does_not_refresh_the_age_of_omitted_measurements():
	bridge, _, clock = make()
	state_msg(bridge, THERMO)
	clock.t = NOW + 3600
	state_msg(bridge, {"linkquality": 90})
	t = bridge.build_body(clock.t)["readings"]["thermostats"][NAME]
	assert t["local_temperature_f"] == 44.6 and t["setpoint_f"] == 45.5 and t["reading_at"] == checkin.iso_utc(NOW)

def test_partial_update_merges_and_refreshes_only_what_it_carries():
	bridge, _, clock = make()
	state_msg(bridge, THERMO)
	clock.t = NOW + 30
	state_msg(bridge, {"local_temperature": 8.0})
	t = bridge.build_body(clock.t)["readings"]["thermostats"][NAME]
	assert t["local_temperature_f"] == 46.4 and t["setpoint_f"] == 45.5 and t["reading_at"] == checkin.iso_utc(NOW + 30)

def test_fresh_setpoint_does_not_vouch_for_a_stale_temperature():
	bridge, _, clock = make()
	state_msg(bridge, THERMO)
	clock.t = NOW + 600
	state_msg(bridge, {"occupied_heating_setpoint": 8.0})
	t = bridge.build_body(clock.t)["readings"]["thermostats"][NAME]
	assert t["setpoint_f"] == 46.4 and t["reading_at"] == checkin.iso_utc(NOW)

def test_state_with_no_measurement_fields_is_not_a_reading():
	bridge, _, _ = make()
	state_msg(bridge, {"linkquality": 90})
	assert "thermostats" not in bridge.build_body(NOW)["readings"]

# ---- units

def test_celsius_to_fahrenheit_and_back():
	assert c_to_f(0) == 32 and round(c_to_f(5.1), 1) == 41.2
	bridge, _, _ = make()
	state_msg(bridge, {"local_temperature": 20.0})
	assert bridge.build_body(NOW)["readings"]["thermostats"][NAME]["local_temperature_f"] == 68.0

def test_fahrenheit_thermostat_passes_through():
	bridge, client, _ = make(unit="f")
	online(bridge)
	state_msg(bridge, {"local_temperature": 44.6, "occupied_heating_setpoint": 50})
	t = bridge.build_body(NOW)["readings"]["thermostats"][NAME]
	assert (t["local_temperature_f"], t["setpoint_f"]) == (44.6, 50)
	bridge.apply_reply(desired(1, setpoint_f=50))
	assert json.loads(client.publish.call_args[0][1]) == {"occupied_heating_setpoint": 50}

# ---- downlink

def published(client):
	return [(c[0][0], json.loads(c[0][1])) for c in client.publish.call_args_list]

def test_in_bounds_desired_publishes_celsius_and_reports_applied_only_after_the_echo():
	bridge, client, clock = make(post=mock.Mock(return_value=desired(7, setpoint_f=50)))
	online(bridge)
	bridge.tick()
	assert published(client) == [("zigbee2mqtt/crawlspace_thermostat/set", {"occupied_heating_setpoint": 10.0})]
	assert bridge.pending_applied == {}
	bridge.post = mock.Mock(return_value={"ok": True})
	bridge.tick()
	assert "applied" not in bridge.post.call_args[0][2]
	echo(bridge, clock, 10.0)
	bridge.tick()
	assert bridge.post.call_args[0][2]["applied"] == applied(7, "applied")
	bridge.tick()
	assert "applied" not in bridge.post.call_args[0][2]

def test_publish_alone_is_never_reported_applied_and_times_out_as_failed():
	# mosquitto accepts the QoS 0 publish while zigbee2mqtt is stopped, so nothing echoes
	bridge, client, clock = make()
	online(bridge)
	offer = desired(7, setpoint_f=50)
	bridge.apply_reply(offer)
	clock.t += checkin.CONFIRM_TIMEOUT_SECONDS - 1
	bridge.confirm_write(clock())
	assert bridge.pending_applied == {}
	clock.t += 1
	bridge.confirm_write(clock())
	assert bridge.pending_applied == applied(7, "failed")
	# not remembered as handled, so recovered zigbee2mqtt gets the write on the next offer
	bridge.apply_reply(offer)
	assert len(published(client)) == 2

def test_echo_of_a_different_setpoint_does_not_confirm():
	bridge, client, clock = make()
	online(bridge)
	bridge.apply_reply(desired(7, setpoint_f=50))
	echo(bridge, clock, 12.0)
	bridge.confirm_write(clock())
	assert bridge.pending_applied == {}

def test_setpoint_seen_before_the_write_does_not_confirm_it():
	bridge, client, clock = make()
	online(bridge)
	state_msg(bridge, {"occupied_heating_setpoint": 10.0})
	clock.t += 5
	bridge.apply_reply(desired(7, setpoint_f=50))
	bridge.confirm_write(clock())
	assert bridge.pending_applied == {}

def test_unavailable_thermostat_is_not_written_and_fails_immediately():
	bridge, client, _ = make()
	bridge.apply_reply(desired(7, setpoint_f=50))
	client.publish.assert_not_called()
	assert bridge.pending_applied == applied(7, "failed")

def test_unconfirmed_version_reoffered_is_not_written_twice():
	bridge, client, clock = make()
	online(bridge)
	offer = desired(7, setpoint_f=50)
	bridge.apply_reply(offer); bridge.apply_reply(offer)
	assert len(published(client)) == 1 and bridge.pending_applied == {}

def test_bounds_edges_are_accepted_and_rounding_never_leaves_them():
	for f in (45, 65):
		bridge, client, _ = make()
		online(bridge)
		bridge.apply_reply(desired(1, setpoint_f=f))
		value = published(client)[0][1]["occupied_heating_setpoint"]
		assert 45 <= c_to_f(value) <= 65, value

@pytest.mark.parametrize("setpoint", [44.9, 65.1, 0, 200, None, "50", float("nan"), True])
def test_out_of_bounds_or_malformed_setpoint_never_writes(setpoint):
	bridge, client, _ = make()
	bridge.apply_reply(desired(3, setpoint_f=setpoint))
	client.publish.assert_not_called()
	assert bridge.pending_applied == applied(3, "rejected_bounds")

def test_no_desired_never_touches_thermostat():
	for reply in ({"ok": True}, {"ok": True, "desired": None}, {"ok": False, "desired": {NAME: {"setpoint_f": 50, "version": 1}}},
			"junk", None, {"ok": True, "desired": {"setpoint_f": 50}}, {"ok": True, "desired": {"setpoint_f": 50, "version": "1"}},
			{"ok": True, "desired": "x"}):
		bridge, client, _ = make()
		bridge.apply_reply(reply)
		client.publish.assert_not_called()
		assert bridge.pending_applied == {}

def test_same_version_not_reapplied_but_new_version_is():
	bridge, client, clock = make()
	online(bridge)
	reply = desired(7, setpoint_f=50)
	bridge.apply_reply(reply); echo(bridge, clock, 10.0); bridge.confirm_write(clock())
	bridge.apply_reply(reply)
	assert len(published(client)) == 1
	bridge.apply_reply(desired(8, setpoint_f=55))
	assert len(published(client)) == 2

def test_reoffered_version_is_reported_again_without_a_second_write():
	bridge, client, clock = make()
	online(bridge)
	reply = desired(7, setpoint_f=50)
	bridge.apply_reply(reply); echo(bridge, clock, 10.0); bridge.confirm_write(clock())
	bridge.pending_applied = {}  # the report went out and was lost
	bridge.apply_reply(reply)
	assert bridge.pending_applied == applied(7, "applied")
	assert len(published(client)) == 1

def test_lost_report_behind_not_ok_reply_is_resent_on_following_ticks():
	bridge, client, clock = make(post=mock.Mock(return_value=desired(7, setpoint_f=50)))
	online(bridge)
	bridge.tick()
	echo(bridge, clock, 10.0)
	offer = desired(7, setpoint_f=50)
	bridge.post = mock.Mock(side_effect=[{"ok": False}, offer, offer, {"ok": True}, {"ok": True}])
	for _ in range(5):
		bridge.tick()
	reports = ["applied" in c[0][2] for c in bridge.post.call_args_list]
	assert reports == [True, True, True, True, False]
	assert len(published(client)) == 1

def test_failed_publish_reports_failed_and_retries_same_version():
	bridge, client, clock = make()
	online(bridge)
	client.publish.return_value.rc = 4
	reply = desired(7, setpoint_f=50)
	bridge.apply_reply(reply)
	assert bridge.pending_applied == applied(7, "failed")
	client.publish.return_value.rc = 0
	bridge.apply_reply(reply)
	echo(bridge, clock, 10.0); bridge.confirm_write(clock())
	assert bridge.pending_applied == applied(7, "applied")

def test_publish_exception_is_failed_not_crash():
	bridge, client, _ = make()
	online(bridge)
	client.publish.side_effect = OSError("broker gone")
	bridge.apply_reply(desired(7, setpoint_f=50))
	assert bridge.pending_applied[NAME]["result"] == "failed"

# ---- v1.1: system_mode, per-thermostat state, allowed modes, quantization

OTHER = "attic_thermostat"
TWO = {NAME: SETTINGS, OTHER: {**SETTINGS, "floor_f": 55.0, "ceiling_f": 75.0, "allowed_modes": ["heat", "off", "cool", "auto"]}}

def make_two():
	return make(thermostats=TWO)

def sets(client, name):
	return [p for t, p in published(client) if t == "zigbee2mqtt/%s/set" % name]

def test_uplink_is_a_map_keyed_by_thermostat_and_omits_silent_ones():
	bridge, _, _ = make_two()
	state_msg(bridge, THERMO, topic="zigbee2mqtt/" + OTHER)
	state_msg(bridge, {"local_temperature": 20.0})
	online(bridge); online(bridge, OTHER)
	readings = bridge.build_body(NOW)["readings"]
	assert set(readings["thermostats"]) == {NAME, OTHER}
	assert readings["thermostats"][OTHER]["setpoint_f"] == 45.5 and readings["thermostats"][NAME]["local_temperature_f"] == 68.0
	assert "thermostat" not in readings
	bridge2, _, _ = make_two()
	state_msg(bridge2, THERMO, topic="zigbee2mqtt/" + OTHER)
	assert set(bridge2.build_body(NOW)["readings"]["thermostats"]) == {OTHER}

def test_on_connect_subscribes_every_thermostat():
	bridge, client, _ = make_two()
	bridge.on_connect(client, None, {}, 0)
	assert {t for t, _ in client.subscribe.call_args[0][0]} == {"zigbee2mqtt/%s%s" % (n, s) for n in (NAME, OTHER) for s in ("", "/availability")}

def test_desired_map_is_applied_per_thermostat_with_independent_versions_and_results():
	bridge, client, clock = make_two()
	online(bridge); online(bridge, OTHER)
	bridge.apply_reply({"ok": True, "desired": {NAME: {"setpoint_f": 50, "version": 7}, OTHER: {"setpoint_f": 70, "system_mode": "off", "version": 2},
		"unknown_key": 1}})
	assert sets(client, NAME) == [{"occupied_heating_setpoint": 10.0}]
	assert sets(client, OTHER) == [{"occupied_heating_setpoint": 21.0, "system_mode": "off"}]
	# only the other thermostat echoes: it alone is reported
	clock.t += 1
	state_msg(bridge, {"occupied_heating_setpoint": 21.0, "system_mode": "off"}, topic="zigbee2mqtt/" + OTHER)
	bridge.confirm_write(clock())
	assert bridge.pending_applied == {OTHER: {"version": 2, "result": "applied"}}
	bridge.post = mock.Mock(return_value={"ok": True})
	bridge.tick()
	assert bridge.post.call_args[0][2]["applied"] == {OTHER: {"version": 2, "result": "applied"}}
	# a repeat of NAME's unconfirmed v7 is not written again, while OTHER moves to v3
	bridge.apply_reply({"ok": True, "desired": {NAME: {"setpoint_f": 50, "version": 7}, OTHER: {"setpoint_f": 72, "version": 3}}})
	assert len(sets(client, NAME)) == 1 and len(sets(client, OTHER)) == 2

def test_rejection_of_one_thermostat_does_not_block_another():
	bridge, client, _ = make_two()
	online(bridge); online(bridge, OTHER)
	bridge.apply_reply({"ok": True, "desired": {NAME: {"setpoint_f": 70, "version": 1}, OTHER: {"setpoint_f": 70, "version": 1}}})
	assert sets(client, NAME) == [] and len(sets(client, OTHER)) == 1
	assert bridge.pending_applied == {NAME: {"version": 1, "result": "rejected_bounds"}}

def test_bounds_are_per_thermostat():
	bridge, client, _ = make_two()
	online(bridge); online(bridge, OTHER)
	bridge.apply_reply({"ok": True, "desired": {NAME: {"setpoint_f": 50, "version": 1}, OTHER: {"setpoint_f": 50, "version": 1}}})
	assert len(sets(client, NAME)) == 1 and sets(client, OTHER) == []
	assert bridge.pending_applied[OTHER]["result"] == "rejected_bounds"

def test_system_mode_heat_is_published_alone_and_confirmed_by_echo():
	bridge, client, clock = make()
	online(bridge)
	bridge.apply_reply(desired(4, system_mode="heat"))
	assert published(client) == [("zigbee2mqtt/crawlspace_thermostat/set", {"system_mode": "heat"})]
	assert bridge.pending_applied == {}
	state_msg(bridge, {"system_mode": "auto"}); bridge.confirm_write(clock())
	assert bridge.pending_applied == {}
	clock.t += 1
	state_msg(bridge, {"system_mode": "heat"}); bridge.confirm_write(clock())
	assert bridge.pending_applied == applied(4, "applied")

def test_system_mode_unconfirmed_times_out_as_failed():
	bridge, _, clock = make()
	online(bridge)
	bridge.apply_reply(desired(4, system_mode="heat"))
	clock.t += checkin.CONFIRM_TIMEOUT_SECONDS
	bridge.confirm_write(clock())
	assert bridge.pending_applied == applied(4, "failed")

def test_absent_system_mode_leaves_the_mode_alone():
	bridge, client, _ = make()
	online(bridge)
	bridge.apply_reply(desired(1, setpoint_f=50))
	assert "system_mode" not in sets(client, NAME)[0]

@pytest.mark.parametrize("mode", ["off", "cool", "auto", "turbo", "", None, 1, True])
def test_mode_outside_the_thermostats_allowed_modes_is_rejected_and_never_written(mode):
	bridge, client, _ = make()
	online(bridge)
	bridge.apply_reply(desired(5, system_mode=mode))
	client.publish.assert_not_called()
	assert bridge.pending_applied == applied(5, "rejected_bounds")

def test_a_disallowed_mode_blocks_the_whole_change_including_a_valid_setpoint():
	bridge, client, _ = make()
	online(bridge)
	bridge.apply_reply(desired(5, setpoint_f=50, system_mode="off"))
	client.publish.assert_not_called()
	assert bridge.pending_applied == applied(5, "rejected_bounds")

def test_both_fields_must_echo_before_applied():
	bridge, _, clock = make()
	online(bridge)
	bridge.apply_reply(desired(6, setpoint_f=50, system_mode="heat"))
	echo(bridge, clock, 10.0); bridge.confirm_write(clock())
	assert bridge.pending_applied == {}
	state_msg(bridge, {"system_mode": "heat"}); bridge.confirm_write(clock())
	assert bridge.pending_applied == applied(6, "applied")

def test_empty_change_is_acknowledged_as_rejected():
	bridge, client, _ = make()
	online(bridge)
	bridge.apply_reply(desired(9))
	client.publish.assert_not_called()
	assert bridge.pending_applied == applied(9, "rejected_bounds")

@pytest.mark.parametrize("echoed", [10.0, 10.2, 9.8, 10.24])
def test_celsius_echo_within_the_device_step_confirms(echoed):
	# 50 F = 10 C; the device may report a value that rounds to the same 0.5 C step
	bridge, _, clock = make()
	online(bridge)
	bridge.apply_reply(desired(7, setpoint_f=50))
	echo(bridge, clock, echoed); bridge.confirm_write(clock())
	assert bridge.pending_applied == applied(7, "applied")

def test_celsius_target_is_quantized_and_a_quantized_echo_confirms():
	# 51 F = 10.56 C is written as 10.5; the device reports 10.5, nowhere near within 0.01 of 10.56
	bridge, client, clock = make()
	online(bridge)
	bridge.apply_reply(desired(7, setpoint_f=51))
	assert sets(client, NAME) == [{"occupied_heating_setpoint": 10.5}]
	echo(bridge, clock, 10.6); bridge.confirm_write(clock())
	assert bridge.pending_applied == applied(7, "applied")

def test_echo_a_full_step_away_does_not_confirm():
	bridge, _, clock = make()
	online(bridge)
	bridge.apply_reply(desired(7, setpoint_f=50))
	echo(bridge, clock, 10.5); bridge.confirm_write(clock())
	assert bridge.pending_applied == {}

def test_fahrenheit_device_confirms_on_the_one_degree_step():
	bridge, client, clock = make(unit="f")
	online(bridge)
	bridge.apply_reply(desired(7, setpoint_f=50.4))
	assert sets(client, NAME) == [{"occupied_heating_setpoint": 50}]
	echo(bridge, clock, 50.3); bridge.confirm_write(clock())
	assert bridge.pending_applied == applied(7, "applied")

def test_reoffered_version_is_tracked_per_thermostat():
	bridge, client, clock = make_two()
	online(bridge); online(bridge, OTHER)
	bridge.apply_reply({"ok": True, "desired": {NAME: {"setpoint_f": 70, "version": 7}}})
	# the same version number on another thermostat is a different change
	bridge.apply_reply({"ok": True, "desired": {OTHER: {"setpoint_f": 70, "version": 7}}})
	assert len(sets(client, OTHER)) == 1

# ---- failure paths and secrets

def test_post_failure_keeps_pending_applied_and_does_not_queue():
	bridge, _, clock = make(post=mock.Mock(return_value=desired(7, setpoint_f=50)))
	online(bridge)
	bridge.tick()
	echo(bridge, clock, 10.0)
	bridge.post = mock.Mock(side_effect=OSError("timed out"))
	bridge.tick()
	assert bridge.post.call_count == 1
	bridge.post = mock.Mock(return_value={"ok": True})
	bridge.tick()
	assert bridge.post.call_args[0][2]["applied"][NAME]["result"] == "applied"
	assert bridge.seq == int(NOW) + 3  # one body per tick, no backlog

def test_token_is_never_logged(caplog):
	caplog.set_level(logging.DEBUG)
	bridge, _, _ = make(post=mock.Mock(side_effect=OSError("boom %s" % TOKEN)))
	state_msg(bridge, THERMO)
	bridge.tick()
	bridge.post = mock.Mock(return_value=desired(1, setpoint_f=99))
	bridge.tick()
	assert caplog.records and TOKEN not in caplog.text

def test_http_post_sends_bearer_and_refuses_redirects(monkeypatch):
	seen = {}
	class Resp:
		status = 200
		def __enter__(self): return self
		def __exit__(self, *a): pass
		def read(self, n): return b'{"ok": true}'
	def fake_open(request, timeout):
		seen.update(auth=request.get_header("Authorization"), timeout=timeout, body=json.loads(request.data))
		return Resp()
	monkeypatch.setattr(checkin._opener, "open", fake_open)
	assert checkin.http_post("https://h/x", TOKEN, {"a": 1}) == {"ok": True}
	assert seen == {"auth": "Bearer " + TOKEN, "timeout": 10, "body": {"a": 1}}
	assert checkin._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil") is None

def test_http_post_rejects_non_200_and_oversize(monkeypatch):
	class Resp:
		def __init__(self, status, raw): self.status, self.raw = status, raw
		def __enter__(self): return self
		def __exit__(self, *a): pass
		def read(self, n): return self.raw[:n]
	monkeypatch.setattr(checkin._opener, "open", lambda r, timeout: Resp(204, b""))
	with pytest.raises(checkin.CheckinError):
		checkin.http_post("https://h/x", TOKEN, {})
	monkeypatch.setattr(checkin._opener, "open", lambda r, timeout: Resp(200, b" " * (checkin.MAX_REPLY_BYTES + 5)))
	with pytest.raises(checkin.CheckinError):
		checkin.http_post("https://h/x", TOKEN, {})

def test_disconnect_invalidates_availability_until_fresh_evidence():
	bridge, client, _ = make()
	state_msg(bridge, THERMO)
	online(bridge)
	assert bridge.build_body(NOW)["readings"]["thermostats"][NAME]["available"] is True
	bridge.on_disconnect(client, None, 1)
	assert bridge.build_body(NOW)["readings"]["thermostats"][NAME]["available"] is False
	bridge.apply_reply(desired(7, setpoint_f=50))
	client.publish.assert_not_called()
	online(bridge)  # availability replayed by the broker after reconnecting
	assert bridge.build_body(NOW)["readings"]["thermostats"][NAME]["available"] is True

def test_failed_reconnect_attempt_also_clears_availability():
	bridge, client, _ = make()
	state_msg(bridge, THERMO); online(bridge)
	bridge.on_connect(client, None, {}, 5)
	assert bridge.build_body(NOW)["readings"]["thermostats"][NAME]["available"] is False

def test_bridge_registers_disconnect_callback():
	bridge, client, _ = make()
	assert client.on_disconnect == bridge.on_disconnect

def test_on_connect_subscribes_both_topics():
	bridge, client, _ = make()
	bridge.on_connect(client, None, {}, 0)
	topics = [t for t, _ in client.subscribe.call_args[0][0]]
	assert topics == ["zigbee2mqtt/crawlspace_thermostat", "zigbee2mqtt/crawlspace_thermostat/availability"]

# ---- main() wiring

def test_main_uses_bridge_not_mqtt_in_checkin_mode_and_closes_it(tmp_path, monkeypatch):
	import signal, threading, time
	import main as sensor_main
	cfg = tmp_path / "config.ini"
	cfg.write_text(CHECKIN.replace("/tmp/x/l.log", str(tmp_path / "t.log")))
	class P:
		pid, exitcode = 1, None
		def start(self): pass
		def is_alive(self): return True
		def terminate(self): pass
		def kill(self): pass
		def join(self, t=None): pass
	monkeypatch.setattr(sensor_main.multiprocessing, "Process", lambda *a, **k: P())
	queue_ = mock.Mock()
	queue_.get.side_effect = [{"cmd": "Time", "data": None}, {"cmd": "exit", "data": None}]
	monkeypatch.setattr(sensor_main.multiprocessing, "Queue", lambda: queue_)
	sensor = mock.Mock(); sensor.check_health.return_value = (True, "")
	monkeypatch.setattr(sensor_main, "SmartSensor", lambda *a, **k: sensor)
	mqtt_cls = mock.Mock(); bridge = mock.Mock()
	monkeypatch.setattr(sensor_main, "SmartSensorToMQTT", mqtt_cls)
	monkeypatch.setattr(sensor_main, "CheckinBridge", lambda c, s: bridge)
	try:
		assert sensor_main.main(str(cfg)) == 0
	finally:
		signal.signal(signal.SIGTERM, signal.SIG_DFL)
	mqtt_cls.assert_not_called()
	bridge.tick.assert_called_once()
	bridge.close.assert_called_once()

# ---- availability never heard

def test_write_refused_without_availability_warns_once(caplog):
	bridge, client, _ = make()
	with caplog.at_level("WARNING"):
		bridge.apply_reply(desired(1, setpoint_f=50))
		bridge.apply_reply(desired(2, setpoint_f=50))
	assert client.publish.call_count == 0
	assert bridge.pending_applied[NAME]["result"] == "failed"
	assert len([r for r in caplog.records if "enable availability" in r.getMessage()]) == 1

def test_write_refused_when_offline_does_not_warn_about_availability(caplog):
	bridge, client, _ = make()
	bridge.thermostats[NAME].state.on_availability(b"offline")
	with caplog.at_level("WARNING"):
		bridge.apply_reply(desired(1, setpoint_f=50))
	assert client.publish.call_count == 0
	assert not [r for r in caplog.records if "enable availability" in r.getMessage()]
