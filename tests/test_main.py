import os
import signal
import subprocess
import sys
import threading
import time
from unittest import mock

import pytest
import main as sensor_main

class FakeProc:
	def __init__(self, alive=True, exitcode=None):
		self.alive, self.exitcode, self.terminated = alive, exitcode, False
	def is_alive(self): return self.alive
	def terminate(self): self.terminated = True; self.alive = False
	def kill(self): self.alive = False
	def join(self, t=None): pass

def healthy_sensor():
	s = mock.Mock()
	s.check_health.return_value = (True, "")
	return s

def test_find_failure_none_when_healthy():
	assert sensor_main.find_failure({"fifo": FakeProc(), "timer": FakeProc()}, healthy_sensor()) is None

def test_find_failure_dead_child():
	reason = sensor_main.find_failure({"fifo": FakeProc(), "timer": FakeProc(False, -9)}, healthy_sensor())
	assert "timer" in reason and "-9" in reason

def test_find_failure_dead_manager():
	s = mock.Mock()
	s.check_health.return_value = (False, "sensor data Manager unusable")
	assert "Manager" in sensor_main.find_failure({"fifo": FakeProc()}, s)

def test_stop_children_terminates_live_and_kills_stubborn():
	live, stubborn = FakeProc(), FakeProc()
	stubborn.terminate = lambda: None  # ignores SIGTERM
	sensor_main.stop_children([live, stubborn])
	assert live.terminated and not stubborn.alive

@pytest.fixture
def patched_main(tmp_path, monkeypatch):
	"""main() with real config + logging but fake sensor/MQTT/processes."""
	cfg = tmp_path / "config.ini"
	cfg.write_text("[sensor]\nsensor_name=s\nclient_id=c\nbroker_host=h\nlog_path=%s/t.log\n" % tmp_path)
	procs = []
	def make_proc(*a, **k):
		p = FakeProc(); p.start = lambda: None; procs.append(p); return p
	monkeypatch.setattr(sensor_main.multiprocessing, "Process", make_proc)
	monkeypatch.setattr(sensor_main, "HEALTH_CHECK_SECONDS", 0.05)
	sensor = healthy_sensor()
	monkeypatch.setattr(sensor_main, "SmartSensor", lambda *a, **k: sensor)
	monkeypatch.setattr(sensor_main, "SmartSensorToMQTT", lambda *a, **k: mock.Mock())
	yield str(cfg), procs, sensor
	# main() installs a SIGTERM handler; restore so it doesn't leak into other tests
	signal.signal(signal.SIGTERM, signal.SIG_DFL)

def test_main_exits_nonzero_when_child_dies(patched_main):
	cfg, procs, sensor = patched_main
	def die_soon():
		time.sleep(0.2); procs[0].alive = False; procs[0].exitcode = 1
	threading.Thread(target=die_soon).start()
	assert sensor_main.main(cfg) == 1
	assert procs[1].terminated  # surviving child stopped, not left to block exit
	sensor.shutdown.assert_called()

def test_main_exits_nonzero_when_manager_dies(patched_main):
	cfg, procs, sensor = patched_main
	sensor.check_health.return_value = (False, "Manager gone")
	assert sensor_main.main(cfg) == 1

def test_main_exits_zero_on_sigterm_and_cleans_up(patched_main):
	cfg, procs, sensor = patched_main
	threading.Timer(0.2, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
	assert sensor_main.main(cfg) == 0
	assert all(p.terminated for p in procs)
	sensor.shutdown.assert_called()

def test_sigterm_real_process_exits_quickly(tmp_path):
	"""End to end with real multiprocessing children: SIGTERM must stop everything in a few seconds."""
	root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
	cfg = tmp_path / "config.ini"
	cfg.write_text("[sensor]\nsensor_name=s\nclient_id=c\nbroker_host=h\nlog_path=%s/t.log\n" % tmp_path)
	script = (
		"import sys, time\nsys.path.insert(0, %r)\nsys.path.insert(0, %r)\n"
		"import conftest\nfrom unittest import mock\nimport smartsensor, smartsensorToMQTT, main\n"
		"smartsensor.adafruit_dht.DHT22.return_value.temperature = 20.0\n"
		"smartsensor.adafruit_dht.DHT22.return_value.humidity = 50.0\n"
		"main.SmartSensorToMQTT = lambda *a, **k: mock.Mock()\n"
		"sys.exit(main.main(%r))\n" % (root, os.path.join(root, "tests"), str(cfg)))
	proc = subprocess.Popen([sys.executable, "-c", script])
	time.sleep(3)
	assert proc.poll() is None
	proc.send_signal(signal.SIGTERM)
	assert proc.wait(timeout=10) == 0
