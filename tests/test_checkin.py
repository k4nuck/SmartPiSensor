import json
import logging
from unittest import mock

import pytest
import checkin
from checkin import CheckinBridge, ThermostatState, c_to_f
from config import load_config, ConfigError

NOW = 1_800_000_000.0
TOKEN = "s3cret-device-token"

CFG = dict(device_id="crawlspace", checkin_url="https://gergy-edge.example/devices/checkin", checkin_token=TOKEN,
	setpoint_floor_f=45.0, setpoint_ceiling_f=65.0, thermostat_name="crawlspace_thermostat",
	thermostat_unit="c", local_mqtt_host="127.0.0.1", broker_port=1883, client_id=None)

class FakeSensor:
	def __init__(self, read_at):
		self.read_at = read_at
	def get_last_read_at(self): return self.read_at
	def get_sensor_data(self): return {"temperature_f": 41.2, "temperature_c": 5.1, "humidity": 63.0}

class Clock:
	def __init__(self, t=NOW): self.t = t
	def __call__(self): return self.t

def make(read_at=NOW - 5, post=None, **cfg):
	clock = Clock()
	client = mock.MagicMock()
	client.publish.return_value.rc = 0
	bridge = CheckinBridge({**CFG, **cfg}, FakeSensor(read_at), post=post or mock.Mock(return_value={"ok": True}),
		clock=clock, mqtt_client=client)
	return bridge, client, clock

def state_msg(bridge, payload, topic=None, retain=False):
	m = mock.Mock(topic=topic or bridge.state_topic, payload=json.dumps(payload).encode(), retain=retain)
	bridge.on_message(None, None, m)

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
	assert cfg["local_mqtt_host"] == "127.0.0.1" and cfg["thermostat_name"] == "crawlspace_thermostat"
	assert (cfg["setpoint_floor_f"], cfg["setpoint_ceiling_f"]) == (45.0, 65.0)
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
			("setpoint_floor_f=70\n", "below"), ("thermostat_unit=k\n", "thermostat_unit")):
		with pytest.raises(ConfigError, match=match):
			cfg_from(tmp_path, CHECKIN + bad)
	with pytest.raises(ConfigError, match="https"):
		cfg_from(tmp_path, CHECKIN.replace("https://h", "http://h"))
	assert cfg_from(tmp_path, CHECKIN.replace("https://h", "http://127.0.0.1:5050"))["transport"] == "checkin"

# ---- body shape and freshness

def test_body_shape_matches_contract():
	bridge, _, _ = make()
	state_msg(bridge, THERMO)
	bridge.thermostat.on_availability(b"online")
	body = bridge.build_body(NOW)
	assert body["device_id"] == "crawlspace" and isinstance(body["seq"], int)
	assert body["ts"].endswith("Z") and "applied" not in body
	dht = body["readings"]["dht22"]
	assert dht == {"temperature_f": 41.2, "temperature_c": 5.1, "humidity": 63, "valid": True,
		"reading_at": checkin.iso_utc(NOW - 5)}
	assert body["readings"]["thermostat"] == {"available": True, "local_temperature_f": 44.6, "setpoint_f": 45.5,
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
	assert bridge.build_body(NOW)["readings"]["thermostat"]["available"] is False
	bridge.on_message(None, None, mock.Mock(topic=bridge.availability_topic, payload=b'{"state":"online"}', retain=True))
	assert bridge.build_body(NOW)["readings"]["thermostat"]["available"] is True
	bridge.on_message(None, None, mock.Mock(topic=bridge.availability_topic, payload=b"offline", retain=True))
	assert bridge.build_body(NOW)["readings"]["thermostat"]["available"] is False

def test_retained_state_without_last_seen_is_not_used_as_a_reading():
	bridge, _, _ = make()
	state_msg(bridge, THERMO, retain=True)
	assert "thermostat" not in bridge.build_body(NOW)["readings"]
	state_msg(bridge, {**THERMO, "last_seen": "2026-10-24T01:00:00Z"}, retain=True)
	assert bridge.build_body(NOW)["readings"]["thermostat"]["reading_at"] == "2026-10-24T01:00:00Z"

def test_partial_state_messages_merge_and_refresh_reading_at():
	bridge, _, clock = make()
	state_msg(bridge, THERMO)
	clock.t = NOW + 30
	state_msg(bridge, {"local_temperature": 8.0})
	t = bridge.build_body(NOW + 30)["readings"]["thermostat"]
	assert t["local_temperature_f"] == 46.4 and t["setpoint_f"] == 45.5 and t["reading_at"] == checkin.iso_utc(NOW + 30)

# ---- units

def test_celsius_to_fahrenheit_and_back():
	assert c_to_f(0) == 32 and round(c_to_f(5.1), 1) == 41.2
	bridge, _, _ = make()
	state_msg(bridge, {"local_temperature": 20.0})
	assert bridge.build_body(NOW)["readings"]["thermostat"]["local_temperature_f"] == 68.0

def test_fahrenheit_thermostat_passes_through():
	bridge, client, _ = make(thermostat_unit="f")
	state_msg(bridge, {"local_temperature": 44.6, "occupied_heating_setpoint": 50})
	t = bridge.build_body(NOW)["readings"]["thermostat"]
	assert (t["local_temperature_f"], t["setpoint_f"]) == (44.6, 50)
	bridge.apply_reply({"ok": True, "desired": {"setpoint_f": 50, "version": 1}})
	assert json.loads(client.publish.call_args[0][1]) == {"occupied_heating_setpoint": 50}

# ---- downlink

def published(client):
	return [(c[0][0], json.loads(c[0][1])) for c in client.publish.call_args_list]

def test_in_bounds_desired_publishes_celsius_and_reports_applied():
	bridge, client, _ = make(post=mock.Mock(return_value={"ok": True, "desired": {"setpoint_f": 50, "version": 7}}))
	bridge.tick()
	assert published(client) == [("zigbee2mqtt/crawlspace_thermostat/set", {"occupied_heating_setpoint": 10.0})]
	bridge.post = mock.Mock(return_value={"ok": True})
	bridge.tick()
	assert bridge.post.call_args[0][2]["applied"] == {"setpoint_version": 7, "result": "applied"}
	bridge.tick()
	assert "applied" not in bridge.post.call_args[0][2]

def test_bounds_edges_are_accepted_and_rounding_never_leaves_them():
	for f in (45, 65):
		bridge, client, _ = make()
		bridge.apply_reply({"ok": True, "desired": {"setpoint_f": f, "version": 1}})
		value = published(client)[0][1]["occupied_heating_setpoint"]
		assert 45 <= c_to_f(value) <= 65, value

@pytest.mark.parametrize("setpoint", [44.9, 65.1, 0, 200, None, "50", float("nan"), True])
def test_out_of_bounds_or_malformed_setpoint_never_writes(setpoint):
	bridge, client, _ = make()
	bridge.apply_reply({"ok": True, "desired": {"setpoint_f": setpoint, "version": 3}})
	client.publish.assert_not_called()
	assert bridge.pending_applied == {"setpoint_version": 3, "result": "rejected_bounds"}

def test_no_desired_never_touches_thermostat():
	for reply in ({"ok": True}, {"ok": True, "desired": None}, {"ok": False, "desired": {"setpoint_f": 50, "version": 1}},
			"junk", None, {"ok": True, "desired": {"setpoint_f": 50}}, {"ok": True, "desired": {"setpoint_f": 50, "version": "1"}},
			{"ok": True, "desired": "x"}):
		bridge, client, _ = make()
		bridge.apply_reply(reply)
		client.publish.assert_not_called()
		assert bridge.pending_applied is None

def test_same_version_not_reapplied_but_new_version_is():
	bridge, client, _ = make()
	reply = {"ok": True, "desired": {"setpoint_f": 50, "version": 7}}
	bridge.apply_reply(reply); bridge.apply_reply(reply)
	assert len(published(client)) == 1
	bridge.apply_reply({"ok": True, "desired": {"setpoint_f": 55, "version": 8}})
	assert len(published(client)) == 2

def test_reoffered_version_is_reported_again_without_a_second_write():
	bridge, client, _ = make()
	reply = {"ok": True, "desired": {"setpoint_f": 50, "version": 7}}
	bridge.apply_reply(reply)
	bridge.pending_applied = None  # the report went out and was lost
	bridge.apply_reply(reply)
	assert bridge.pending_applied == {"setpoint_version": 7, "result": "applied"}
	assert len(published(client)) == 1

def test_lost_report_behind_not_ok_reply_is_resent_on_following_ticks():
	bridge, client, _ = make(post=mock.Mock(return_value={"ok": True, "desired": {"setpoint_f": 50, "version": 7}}))
	bridge.tick()
	offer = {"ok": True, "desired": {"setpoint_f": 50, "version": 7}}
	bridge.post = mock.Mock(side_effect=[{"ok": False}, offer, offer, {"ok": True}, {"ok": True}])
	for _ in range(5):
		bridge.tick()
	reports = ["applied" in c[0][2] for c in bridge.post.call_args_list]
	assert reports == [True, True, True, True, False]
	assert len(published(client)) == 1

def test_failed_publish_reports_failed_and_retries_same_version():
	bridge, client, _ = make()
	client.publish.return_value.rc = 4
	reply = {"ok": True, "desired": {"setpoint_f": 50, "version": 7}}
	bridge.apply_reply(reply)
	assert bridge.pending_applied == {"setpoint_version": 7, "result": "failed"}
	client.publish.return_value.rc = 0
	bridge.apply_reply(reply)
	assert bridge.pending_applied == {"setpoint_version": 7, "result": "applied"}

def test_publish_exception_is_failed_not_crash():
	bridge, client, _ = make()
	client.publish.side_effect = OSError("broker gone")
	bridge.apply_reply({"ok": True, "desired": {"setpoint_f": 50, "version": 7}})
	assert bridge.pending_applied["result"] == "failed"

# ---- failure paths and secrets

def test_post_failure_keeps_pending_applied_and_does_not_queue():
	bridge, _, _ = make(post=mock.Mock(return_value={"ok": True, "desired": {"setpoint_f": 50, "version": 7}}))
	bridge.tick()
	bridge.post = mock.Mock(side_effect=OSError("timed out"))
	bridge.tick()
	assert bridge.post.call_count == 1
	bridge.post = mock.Mock(return_value={"ok": True})
	bridge.tick()
	assert bridge.post.call_args[0][2]["applied"]["result"] == "applied"
	assert bridge.seq == int(NOW) + 3  # one body per tick, no backlog

def test_token_is_never_logged(caplog):
	caplog.set_level(logging.DEBUG)
	bridge, _, _ = make(post=mock.Mock(side_effect=OSError("boom %s" % TOKEN)))
	state_msg(bridge, THERMO)
	bridge.tick()
	bridge.post = mock.Mock(return_value={"ok": True, "desired": {"setpoint_f": 99, "version": 1}})
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
