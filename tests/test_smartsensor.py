import os
import signal
import time
from unittest import mock
import pytest
import smartsensor

@pytest.fixture
def fast(monkeypatch):
	monkeypatch.setattr(smartsensor, "MANAGER_PROBE_SECONDS", 1)
	monkeypatch.setattr(smartsensor.adafruit_dht.DHT22.return_value, "temperature", 20.0, raising=False)
	monkeypatch.setattr(smartsensor.adafruit_dht.DHT22.return_value, "humidity", 50.0, raising=False)

def test_healthy_sensor_reports_ok_then_shuts_down(fast):
	s = smartsensor.SmartSensor("D4", False, "sensor", "s")
	try:
		assert s.check_health() == (True, "")
	finally:
		s.shutdown()
	assert not s.worker_thread.is_alive() and not s.timer_thread.is_alive()
	assert not s.manager._process.is_alive()

def test_stopped_worker_is_killed_by_shutdown(fast):
	s = smartsensor.SmartSensor("D4", False, "sensor", "s")
	os.kill(s.worker_thread.pid, signal.SIGSTOP)
	start = time.monotonic()
	s.shutdown()
	assert time.monotonic() - start < 10
	assert not s.worker_thread.is_alive()

def test_stopped_manager_is_detected_and_shutdown_is_bounded(fast):
	s = smartsensor.SmartSensor("D4", False, "sensor", "s")
	os.kill(s.manager._process.pid, signal.SIGSTOP)
	start = time.monotonic()
	ok, reason = s.check_health()
	assert not ok and "Manager" in reason
	assert time.monotonic() - start < 5
	start = time.monotonic()
	s.shutdown()
	assert time.monotonic() - start < 10
	assert not s.manager._process.is_alive()

def test_dead_manager_is_detected(fast):
	s = smartsensor.SmartSensor("D4", False, "sensor", "s")
	try:
		s.manager._process.kill()
		s.manager._process.join()
		ok, reason = s.check_health()
		assert not ok and "manager" in reason
	finally:
		s.shutdown()

def test_failed_construction_cleans_up_started_children(fast, monkeypatch):
	started = []
	real_start = smartsensor.multiprocessing.Process.start
	def flaky_start(self):
		if started:
			raise OSError("fork failed")
		real_start(self); started.append(self)
	monkeypatch.setattr(smartsensor.multiprocessing.Process, "start", flaky_start)
	with pytest.raises(OSError):
		smartsensor.SmartSensor("D4", False, "sensor", "s")
	assert started and not started[0].is_alive()
