import socket
from unittest import mock
import pytest
from smartsensorToMQTT import connect_with_backoff, SmartSensorToMQTT

def test_retries_dns_failure_with_capped_backoff():
	client = mock.Mock()
	client.connect.side_effect = [socket.gaierror("Name or service not known")] * 4 + [None]
	sleeps = []
	connect_with_backoff(client, "h", 1883, sleep=sleeps.append, max_delay=4)
	assert client.connect.call_count == 5
	assert sleeps == [1, 2, 4, 4]

def test_non_network_errors_still_propagate():
	client = mock.Mock()
	client.connect.side_effect = ValueError("bad")
	with pytest.raises(ValueError):
		connect_with_backoff(client, "h", 1883, sleep=lambda s: None)

def _sensor():
	sensor = mock.Mock()
	sensor.get_sensor_type.return_value = "sensor"
	sensor.get_sensor_name.return_value = "sensorX"
	return sensor

def test_config_payload_defaults_keep_bedroom_identity():
	m = SmartSensorToMQTT("c", "h", 1883, "homeassistant", _sensor())
	t, h = m.get_config_payload("T"), m.get_config_payload("H")
	assert (t["unique_id"], h["unique_id"]) == ("temp01ae", "hum01ae")
	assert t["device"] == h["device"] == {"identifiers": ["Attic01ae"], "name": "Attic"}

def test_config_payload_uses_configured_identity():
	m = SmartSensorToMQTT("c", "h", 1883, "homeassistant", _sensor(), device_id="Crawlspace01",
		device_name="Crawlspace", temp_unique_id="tempCrawl01", hum_unique_id="humCrawl01")
	t, h = m.get_config_payload("T"), m.get_config_payload("H")
	assert (t["unique_id"], h["unique_id"]) == ("tempCrawl01", "humCrawl01")
	assert t["device"] == h["device"] == {"identifiers": ["Crawlspace01"], "name": "Crawlspace"}

def test_supervise_runs_during_backoff_and_can_abort():
	from smartsensorToMQTT import SupervisionFailure
	client = mock.Mock()
	client.connect.side_effect = OSError("down")
	calls = []
	def supervise():
		calls.append(1)
		if len(calls) >= 6:
			raise SupervisionFailure("sensor died")
	sleeps = []
	with pytest.raises(SupervisionFailure):
		connect_with_backoff(client, "h", 1883, sleep=sleeps.append, supervise=supervise, supervise_interval=1, max_delay=8)
	assert len(calls) == 6 and set(sleeps) == {1}
