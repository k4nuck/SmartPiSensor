import asyncio
import json

import pytest

import shield_service
from shield_service import Adb, AdbError, Shield, parse_route

NOW = 1_000_000.0

TUN = "1.1.1.1 dev tun1 table 1002 src 100.64.0.5 uid 0\n    cache"
LAN = "1.1.1.1 via 192.168.1.1 dev wlan0 src 192.168.1.62 uid 0\n    cache"

def test_parse_route():
	assert parse_route(TUN) is True
	assert parse_route(LAN) is False
	assert parse_route("") is None
	assert parse_route("Error: network unreachable") is None
	assert parse_route(None) is None

class FakeAdb:
	"""Route flips only after the broadcast; scripted failures stand in for a dead adb link."""
	def __init__(self, route=False, flip_after_reads=0):
		self.exit_node_name = "ha-main-remote"
		self.route = route
		self.broadcasts = []
		self.fail_reads = False
		self.fail_broadcast = None
		self.ignore_broadcast = False
		self.pending = None
		self.flip_after_reads = flip_after_reads

	async def route_uses_exit_node(self, deadline=None):
		if self.fail_reads:
			raise AdbError("offline")
		if self.pending is not None:
			if self.pending[1] <= 0:
				self.route, self.pending = self.pending[0], None
			else:
				self.pending = (self.pending[0], self.pending[1] - 1)
		return self.route

	async def broadcast(self, node, deadline=None):
		if self.fail_broadcast:
			raise AdbError(self.fail_broadcast)
		self.broadcasts.append(node)
		if not self.ignore_broadcast:
			self.pending = (node != "", self.flip_after_reads)

class FakePublisher:
	def __init__(self):
		self.states, self.results, self.availability = [], [], []
	def publish_state(self, state): self.states.append(state)
	def publish_result(self, result): self.results.append(result)
	def publish_availability(self, online): self.availability.append(online)

def make(adb=None, **kw):
	adb = adb or FakeAdb()
	pub = FakePublisher()
	return Shield(adb, pub, clock=lambda: NOW, verify_seconds=0.2, verify_poll=0.01, **kw), adb, pub

def cmd(command="turn_on", id="a1", ts=NOW, **extra):
	return json.dumps({"id": id, "command": command, "ts": ts, **extra})

def run(coro):
	return asyncio.run(coro)

def test_turn_on_broadcasts_exit_node_and_confirms_route():
	shield, adb, pub = make()
	run(shield.handle(cmd("turn_on")))
	assert adb.broadcasts == ["ha-main-remote"]
	assert pub.results == [{"id": "a1", "outcome": "ok", "error": None, "exit_node": True}]
	assert pub.states[-1] == {"exit_node": True, "online": True, "ts": int(NOW)}

def test_turn_off_sends_empty_exit_node():
	shield, adb, pub = make(FakeAdb(route=True))
	run(shield.handle(cmd("turn_off")))
	assert adb.broadcasts == [""]
	assert pub.results[0]["outcome"] == "ok" and pub.results[0]["exit_node"] is False

def test_waits_for_slow_route_flip():
	shield, adb, pub = make(FakeAdb(flip_after_reads=3))
	run(shield.handle(cmd("turn_on")))
	assert pub.results[0]["outcome"] == "ok"

def test_route_that_never_switches_is_failed_not_ok():
	adb = FakeAdb()
	adb.ignore_broadcast = True
	shield, _, pub = make(adb)
	run(shield.handle(cmd("turn_on")))
	assert pub.results[0]["outcome"] == "failed"
	assert pub.states[-1]["exit_node"] is False

def test_unreadable_route_after_broadcast_is_unknown():
	adb = FakeAdb()
	shield, _, pub = make(adb)
	adb.fail_reads = True
	run(shield.handle(cmd("turn_on")))
	assert pub.results[0]["outcome"] == "unknown"
	assert pub.states[-1] == {"exit_node": None, "online": False, "ts": int(NOW)}

def test_broadcast_error_is_unknown_because_it_may_have_landed():
	adb = FakeAdb()
	adb.fail_broadcast = "adb broadcast timed out"
	shield, _, pub = make(adb)
	run(shield.handle(cmd("turn_on")))
	assert pub.results[0]["outcome"] == "unknown"

@pytest.mark.parametrize("payload,fragment", [
	("not json", "JSON object"),
	("[1]", "JSON object"),
	(json.dumps({"command": "turn_on", "ts": NOW}), "missing command id"),
	(cmd("reboot"), "unknown command"),
	(cmd(ts=NOW - 21), "expired"),
	(cmd(ts=NOW + 6), "future"),
	(cmd(ts="x"), "ts must be a number"),
	(cmd(ts=True), "ts must be a number"),
])
def test_bad_commands_are_rejected_without_touching_adb(payload, fragment):
	shield, adb, pub = make()
	run(shield.handle(payload))
	assert pub.results[0]["outcome"] == "rejected" and fragment in pub.results[0]["error"]
	assert adb.broadcasts == []

def test_redelivered_id_returns_cached_result_without_second_broadcast():
	shield, adb, pub = make()
	async def go():
		await asyncio.gather(shield.handle(cmd()), shield.handle(cmd()))
	run(go())
	assert adb.broadcasts == ["ha-main-remote"]
	assert pub.results[0] == pub.results[1]

def test_stopping_service_rejects_commands():
	shield, adb, pub = make()
	shield.closing = True
	run(shield.handle(cmd()))
	assert pub.results[0]["outcome"] == "rejected" and adb.broadcasts == []

def test_poll_publishes_on_change_and_forced_refresh_only():
	shield, adb, pub = make()
	async def go():
		await shield.poll_once(force=True)
		await shield.poll_once()
		adb.route = True
		await shield.poll_once()
		await shield.poll_once(force=True)
	run(go())
	assert [s["exit_node"] for s in pub.states] == [False, True, True]

def test_poll_with_adb_down_reports_offline_and_unknown_route():
	adb = FakeAdb()
	adb.fail_reads = True
	shield, _, pub = make(adb)
	run(shield.poll_once(force=True))
	assert pub.states == [{"exit_node": None, "online": False, "ts": int(NOW)}]
	assert pub.availability == [False]

def test_run_loop_stops_promptly():
	shield, _, pub = make()
	async def go():
		stop = asyncio.Event()
		task = asyncio.create_task(shield.run(stop, check_every=60))
		await asyncio.sleep(0.05)
		stop.set()
		await asyncio.wait_for(task, 1)
	run(go())
	assert pub.states

# -- the real Adb class, with the subprocess boundary faked --

class FakeProc:
	def __init__(self, out, rc=0, hang=False):
		self.out, self.returncode, self.hang = out, rc, hang
		self.killed = False
	async def communicate(self):
		if self.hang:
			await asyncio.sleep(10)
		return self.out.encode(), None
	def kill(self): self.killed = True
	async def wait(self): return self.returncode

def adb_with(script, monkeypatch):
	calls = []
	async def fake_exec(*args, **kw):
		calls.append(args)
		return script(args)
	monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
	return Adb("192.168.1.62:5555", "ha-main-remote"), calls

def test_adb_reconnects_once_when_a_shell_call_fails(monkeypatch):
	state = {"n": 0}
	def script(args):
		if args[1] == "connect":
			return FakeProc("connected to 192.168.1.62:5555")
		state["n"] += 1
		return FakeProc("error: device offline", rc=1) if state["n"] == 1 else FakeProc(TUN)
	adb, calls = adb_with(script, monkeypatch)
	assert run(adb.route_uses_exit_node()) is True
	assert [c[1] for c in calls] == ["-s", "connect", "-s"]

def test_adb_connect_failure_text_is_an_error_even_with_exit_zero(monkeypatch):
	adb, _ = adb_with(lambda a: FakeProc("failed to connect to '192.168.1.62:5555'") if a[1] == "connect" else FakeProc("x", rc=1), monkeypatch)
	with pytest.raises(AdbError):
		run(adb.route_uses_exit_node())

def test_adb_hang_times_out_and_kills_the_process(monkeypatch):
	monkeypatch.setattr(shield_service, "ADB_TIMEOUT_SECONDS", 0.05)
	procs = []
	def script(args):
		procs.append(FakeProc("", hang=True))
		return procs[-1]
	adb, _ = adb_with(script, monkeypatch)
	with pytest.raises(AdbError):
		run(adb._run("-s", "t", "shell", "x"))
	assert procs[0].killed

def test_missing_adb_binary_is_an_adb_error(monkeypatch):
	async def boom(*a, **k): raise FileNotFoundError("adb")
	monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
	with pytest.raises(AdbError):
		run(Adb("t", "n")._run("devices"))

def test_broadcast_command_quotes_the_empty_exit_node(monkeypatch):
	adb, calls = adb_with(lambda a: FakeProc("Broadcast completed: result=0"), monkeypatch)
	run(adb.broadcast(""))
	run(adb.broadcast("ha-main-remote"))
	off, on = calls[0][-1], calls[1][-1]
	assert "--es exitNode ''" in off and "--ez allowLanAccess true" in off
	assert "--es exitNode ha-main-remote " in on
	assert "com.tailscale.ipn/.IPNReceiver" in on

def test_undelivered_broadcast_is_an_error(monkeypatch):
	adb, _ = adb_with(lambda a: FakeProc("Broadcast completed: result=0") if False else FakeProc("Error: no receiver"), monkeypatch)
	with pytest.raises(AdbError):
		run(adb.broadcast("x"))

def test_config_defaults_and_overrides():
	from config import load_config
	env = {"SMARTSENSOR_SENSOR_NAME": "s", "SMARTSENSOR_CLIENT_ID": "c", "SMARTSENSOR_BROKER_HOST": "h", "SMARTSENSOR_LOG_PATH": "/tmp/x/l.log"}
	cfg = load_config("/nonexistent.ini", environ=env)
	assert (cfg["shield_slug"], cfg["shield_adb"], cfg["shield_exit_node"]) == ("upstate_tv", "192.168.1.62:5555", "ha-main-remote")
	cfg = load_config("/nonexistent.ini", environ={**env, "SMARTSENSOR_SHIELD_SLUG": "den"})
	assert cfg["shield_slug"] == "den"

def test_publisher_sets_retained_lwt():
	from unittest import mock
	with mock.patch.object(shield_service.mqtt, "Client") as client_cls:
		shield_service.MqttPublisher("cid", "broker", 1883, "upstate_tv")
	client_cls.return_value.will_set.assert_called_once_with("upstate/shield/upstate_tv/availability", "offline", qos=1, retain=True)
	client_cls.return_value.connect_async.assert_called_once_with("broker", 1883)

# -- rework of review findings --

class BoundedPublisher(FakePublisher):
	"""Client queue of fixed size that only drains when the test says so, like unacknowledged qos1 publishes."""
	def __init__(self, size=4, queued=0):
		super().__init__()
		self.size, self.queued, self.order = size, queued, []
	def _take(self, kind):
		if self.queued >= self.size:
			return False
		self.queued += 1
		self.order.append(kind)
		return True
	def publish_state(self, state): return self._take("state")
	def publish_availability(self, online): return self._take("availability")
	def publish_result(self, result):
		ok = self._take("result")
		if ok:
			self.results.append(result)
		return ok

def test_result_is_published_before_state_when_queue_is_nearly_full():
	pub = BoundedPublisher(size=4, queued=2)
	shield = Shield(FakeAdb(), pub, clock=lambda: NOW, verify_seconds=0.2, verify_poll=0.01)
	run(shield.handle(cmd("turn_on")))
	assert pub.order[0] == "result"
	assert [r["outcome"] for r in pub.results] == ["ok"]

def test_result_publish_is_retried_when_queue_is_full(monkeypatch):
	monkeypatch.setattr(shield_service, "RESULT_RETRY_SECONDS", 0.01)
	pub = BoundedPublisher(size=1, queued=1)
	shield = Shield(FakeAdb(), pub, clock=lambda: NOW, verify_seconds=0.2, verify_poll=0.01)
	async def go():
		task = asyncio.create_task(shield.handle(cmd("turn_on")))
		await asyncio.sleep(0.05)
		pub.queued = 0
		await task
	run(go())
	assert [r["outcome"] for r in pub.results] == ["ok"]

def test_refused_state_publish_is_retried_by_next_poll():
	pub = BoundedPublisher(size=0)
	shield = Shield(FakeAdb(), pub, clock=lambda: NOW)
	run(shield.poll_once())
	pub.size = 10
	run(shield.poll_once())
	assert pub.order.count("state") == 1

def test_broadcast_error_publishes_unknown_state_and_reachable_availability():
	adb = FakeAdb(route=False)
	adb.fail_broadcast = "adb broadcast timed out"
	shield, _, pub = make(adb)
	run(shield.poll_once(force=True))
	run(shield.handle(cmd("turn_on")))
	assert pub.results[0]["outcome"] == "unknown"
	assert pub.states[-1] == {"exit_node": None, "online": True, "ts": int(NOW)}
	assert pub.availability[-1] is True

def test_broadcast_error_with_unreachable_shield_publishes_offline():
	adb = FakeAdb()
	adb.fail_broadcast = "adb exited 1: device offline"
	shield, _, pub = make(adb)
	run(shield.poll_once(force=True))
	adb.fail_reads = True
	run(shield.handle(cmd("turn_on")))
	assert pub.states[-1] == {"exit_node": None, "online": False, "ts": int(NOW)}
	assert pub.availability[-1] is False

def slow_adb(monkeypatch, per_call, script):
	"""Real Adb whose every subprocess takes per_call seconds (and times out at the ADB timeout, scaled down)."""
	monkeypatch.setattr(shield_service, "ADB_TIMEOUT_SECONDS", per_call)
	calls = []
	async def fake_exec(*args, **kw):
		calls.append(args)
		return script(args)
	monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
	return Adb("t:5555", "ha-main-remote"), calls

def test_verification_read_sequence_is_capped_by_the_deadline(monkeypatch):
	import time
	# Every call hangs to its timeout: shell, connect, shell would be 3 x 0.3 s without a shared deadline.
	adb, calls = slow_adb(monkeypatch, 0.3, lambda a: FakeProc("", hang=True))
	started = time.monotonic()
	with pytest.raises(AdbError):
		run(adb.route_uses_exit_node(deadline=time.monotonic() + 0.4))
	assert time.monotonic() - started < 0.6

def test_whole_command_stays_inside_verify_budget_with_real_adb(monkeypatch):
	import time
	def script(args):
		if "am broadcast" in args[-1]:
			return FakeProc("Broadcast completed: result=0")
		return FakeProc("", hang=True)
	adb, _ = slow_adb(monkeypatch, 0.3, script)
	pub = FakePublisher()
	shield = Shield(adb, pub, clock=time.time, verify_seconds=0.4, verify_poll=0.01)
	started = time.monotonic()
	run(shield.handle(json.dumps({"id": "x", "command": "turn_on", "ts": time.time()})))
	# verify budget 0.4 s plus the reachability-free path; unscoped it would be several multiples of 0.3 s per read.
	assert time.monotonic() - started < 0.9
	assert pub.results[0]["outcome"] == "unknown"

def test_broadcast_is_not_resent_after_the_command_expired(monkeypatch):
	import time
	state = {"shell": 0}
	def script(args):
		if args[1] == "connect":
			return FakeProc("connected to t:5555")
		state["shell"] += 1
		return FakeProc("error: device offline", rc=1)
	adb, calls = slow_adb(monkeypatch, 1, script)
	async def slow_connect(deadline=None):
		await asyncio.sleep(0.15)
	monkeypatch.setattr(adb, "connect", slow_connect)
	with pytest.raises(AdbError):
		run(adb.broadcast("ha-main-remote", deadline=time.monotonic() + 0.1))
	assert state["shell"] == 1

def test_command_deadline_reaches_the_broadcast():
	seen = {}
	class Spy(FakeAdb):
		async def broadcast(self, node, deadline=None):
			seen["left"] = deadline - __import__("time").monotonic()
			await super().broadcast(node)
	shield, _, _ = make(Spy())
	run(shield.handle(cmd("turn_on", ts=NOW - 15)))
	# Accepted at age 15 s of a 20 s window: roughly 5 s of life left, never the full 20.
	assert 4 < seen["left"] <= 5.1
