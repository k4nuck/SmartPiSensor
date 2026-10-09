import pytest
from config import load_config, ConfigError

INI = """[sensor]
sensor_name = sensorCrawlspace
client_id = PiSensorClient
broker_host = ha-main-remote.tail9144d.ts.net
log_path = /tmp/x/crawl.log
"""

def write(tmp_path, text=INI):
	p = tmp_path / "config.ini"
	p.write_text(text)
	return str(p)

def test_loads_file_with_defaults(tmp_path):
	cfg = load_config(write(tmp_path), environ={})
	assert cfg["sensor_name"] == "sensorCrawlspace"
	assert cfg["broker_port"] == 1883
	assert cfg["fifo_path"] == "/tmp/x/temp.fifo"

def test_env_overrides_file(tmp_path):
	cfg = load_config(write(tmp_path), environ={"SMARTSENSOR_BROKER_HOST": "192.168.1.252", "SMARTSENSOR_BROKER_PORT": "1884"})
	assert cfg["broker_host"] == "192.168.1.252"
	assert cfg["broker_port"] == 1884

def test_env_only_without_file(tmp_path):
	env = {"SMARTSENSOR_" + k.upper(): v for k, v in
		dict(sensor_name="s", client_id="c", broker_host="h", log_path="/tmp/l.log").items()}
	assert load_config(str(tmp_path / "missing.ini"), environ=env)["sensor_name"] == "s"

def test_missing_required_raises(tmp_path):
	with pytest.raises(ConfigError, match="broker_host"):
		load_config(write(tmp_path, "[sensor]\nsensor_name=a\nclient_id=b\nlog_path=/tmp/l\n"), environ={})

def test_bad_port_raises(tmp_path):
	with pytest.raises(ConfigError, match="broker_port"):
		load_config(write(tmp_path), environ={"SMARTSENSOR_BROKER_PORT": "abc"})

def test_example_file_is_loadable():
	import os
	example = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.ini.example")
	assert load_config(example, environ={})["broker_host"] == "192.168.1.252"
