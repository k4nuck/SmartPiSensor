import ast
import asyncio
import contextlib
import os
from unittest import mock

import pytest

# aiohomekit only exists in the ecobee service's environment, not the sensor Pi's.
pytest.importorskip("aiohomekit")

from aiohomekit.model import Accessory, Accessories
from aiohomekit.model.characteristics import CharacteristicsTypes as C
from aiohomekit.model.services import ServicesTypes as S

import ecobee_pair
import ecobee_service
import ecobee_store
from ecobee_state import discover_layout, read_ids, build_state

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _accessory(aid, name):
	return Accessory.create_with_info(aid, name, "ecobee", "model", "serial", "1.0")

def _ecobee(with_sensor=True):
	accessories = Accessories()
	t = _accessory(1, "Living Room")
	svc = t.add_service(S.THERMOSTAT)
	for char, value in [(C.TEMPERATURE_CURRENT, 21.5), (C.RELATIVE_HUMIDITY_CURRENT, 45), (C.HEATING_COOLING_TARGET, 3),
			(C.HEATING_COOLING_CURRENT, 1), (C.TEMPERATURE_TARGET, 22), (C.TEMPERATURE_HEATING_THRESHOLD, 20),
			(C.TEMPERATURE_COOLING_THRESHOLD, 25)]:
		svc.add_char(char, value=value)
	accessories.add_accessory(t)
	if with_sensor:
		s = _accessory(2, "Bedroom")
		s.add_service(S.TEMPERATURE_SENSOR).add_char(C.TEMPERATURE_CURRENT, value=19.0)
		s.add_service(S.OCCUPANCY_SENSOR).add_char(C.OCCUPANCY_DETECTED, value=1)
		accessories.add_accessory(s)
	return accessories

def _values(layout, overrides=None):
	values = {}
	for ident in read_ids(layout):
		values[ident] = {"value": 0}
	return {**values, **(overrides or {})}

def _chars_by_type(accessories):
	t = next(iter(accessories))
	return {c.type: (1, c.iid) for s in t.services for c in s.characteristics}

def test_state_matches_the_published_contract():
	accessories = _ecobee()
	layout = discover_layout(accessories)
	ids = _chars_by_type(accessories)
	sensor = layout["sensors"][0]
	values = {
		layout["thermostat"]["temperature_f"]: {"value": 21.5}, layout["thermostat"]["humidity"]: {"value": 45},
		layout["thermostat"]["mode"]: {"value": 3}, layout["thermostat"]["action"]: {"value": 1},
		layout["thermostat"]["target"]: {"value": 22},
		layout["thermostat"]["heat_threshold"]: {"value": 20}, layout["thermostat"]["cool_threshold"]: {"value": 25},
		sensor["temperature_f"]: {"value": 19.0}, sensor["occupancy"]: {"value": 1},
	}
	state = build_state("Living Room", layout, values, 1000)
	assert state == {
		"name": "Living Room", "temperature_f": 70.7, "humidity": 45, "hvac_mode": "heat_cool", "hvac_action": "heating",
		"target_heat_f": 68.0, "target_cool_f": 77.0,
		"sensors": [{"name": "Bedroom", "temperature_f": 66.2, "occupancy": True}], "ts": 1000,
	}

def test_missing_fields_are_null_not_errors():
	# A thermostat exposing only a temperature, one sensor read failing with a HAP status.
	accessories = Accessories()
	t = _accessory(1, "Backroom")
	t.add_service(S.THERMOSTAT).add_char(C.TEMPERATURE_CURRENT, value=20)
	accessories.add_accessory(t)
	s = _accessory(2, "Den")
	s.add_service(S.TEMPERATURE_SENSOR).add_char(C.TEMPERATURE_CURRENT, value=18)
	accessories.add_accessory(s)
	layout = discover_layout(accessories)
	values = {layout["thermostat"]["temperature_f"]: {"value": 20}, layout["sensors"][0]["temperature_f"]: {"status": -70402}}
	state = build_state("Backroom", layout, values, 5)
	assert state["temperature_f"] == 68.0
	assert state["sensors"] == [{"name": "Den", "temperature_f": None, "occupancy": None}]
	for key in ("humidity", "hvac_mode", "hvac_action", "target_heat_f", "target_cool_f"):
		assert state[key] is None

def test_single_setpoint_modes_use_the_target_temperature_even_with_stale_thresholds():
	layout = discover_layout(_ecobee(with_sensor=False))
	t = layout["thermostat"]
	stale = {t["heat_threshold"]: {"value": 20}, t["cool_threshold"]: {"value": 25}}
	heat = build_state("x", layout, {t["mode"]: {"value": 1}, t["target"]: {"value": 22}, **stale}, 1)
	assert (heat["hvac_mode"], heat["target_heat_f"], heat["target_cool_f"]) == ("heat", 71.6, None)
	cool = build_state("x", layout, {t["mode"]: {"value": 2}, t["target"]: {"value": 24}, **stale}, 1)
	assert (cool["hvac_mode"], cool["target_heat_f"], cool["target_cool_f"]) == ("cool", None, 75.2)
	auto = build_state("x", layout, {t["mode"]: {"value": 3}, t["target"]: {"value": 22}, **stale}, 1)
	assert (auto["target_heat_f"], auto["target_cool_f"]) == (68.0, 77.0)

def test_thermostat_own_temperature_service_is_not_a_room_sensor():
	accessories = _ecobee(with_sensor=False)
	next(iter(accessories)).add_service(S.TEMPERATURE_SENSOR).add_char(C.TEMPERATURE_CURRENT, value=21)
	assert discover_layout(accessories)["sensors"] == []

class FakePairing:
	"""Enforces the read-only contract: any write-ish call fails the test."""
	def __init__(self, accessories, outcomes):
		self.accessories = accessories
		self.outcomes = list(outcomes)
		self.reads = 0
		self.listed = 0

	async def list_accessories_and_characteristics(self):
		self.listed += 1
		return []

	async def get_characteristics(self, ids):
		self.reads += 1
		outcome = self.outcomes.pop(0)
		if isinstance(outcome, Exception):
			raise outcome
		if outcome == "hap-error":
			return {i: {"status": -70402, "description": "Unable to communicate"} for i in ids}
		return {i: {"value": 1} for i in ids}

	def __getattr__(self, name):
		raise AssertionError("poller touched pairing.%s" % name)

class FakePublisher:
	def __init__(self):
		self.events = []

	def publish_state(self, state):
		self.events.append(("state", state))

	def publish_availability(self, online):
		self.events.append(("availability", online))

def _poller(outcomes):
	pairing = FakePairing(_ecobee(), outcomes)
	publisher = FakePublisher()
	return ecobee_service.Poller("living-room", "Living Room", ecobee_service.ReadOnlyPairing(pairing), publisher, clock=lambda: 42), pairing, publisher

def test_poll_publishes_state_then_online():
	poller, _, publisher = _poller(["ok"])
	assert asyncio.run(poller.poll_once()) is True
	assert [e[0] for e in publisher.events] == ["state", "availability"]
	assert publisher.events[0][1]["ts"] == 42 and publisher.events[1] == ("availability", True)

def test_offline_after_two_consecutive_failures_once_and_recovers():
	boom = ConnectionError("down")
	poller, _, publisher = _poller([boom, boom, boom, "ok"])
	asyncio.run(poller.poll_once())
	assert publisher.events == []  # one miss is tolerated
	asyncio.run(poller.poll_once())
	assert publisher.events == [("availability", False)]
	asyncio.run(poller.poll_once())
	assert publisher.events == [("availability", False)]  # no repeat spam while down
	asyncio.run(poller.poll_once())
	assert publisher.events[-1] == ("availability", True) and publisher.events[-2][0] == "state"

def test_hung_read_times_out_instead_of_stalling(monkeypatch):
	monkeypatch.setattr(ecobee_service, "READ_TIMEOUT_SECONDS", 0.05)
	class Hung(FakePairing):
		async def get_characteristics(self, ids):
			await asyncio.sleep(5)
	poller = ecobee_service.Poller("d", "D", ecobee_service.ReadOnlyPairing(Hung(_ecobee(), [])), FakePublisher())
	assert asyncio.run(poller.poll_once()) is False

def test_all_hap_errors_count_as_a_failed_poll():
	poller, _, publisher = _poller(["hap-error", "hap-error", "ok"])
	assert asyncio.run(poller.poll_once()) is False
	assert publisher.events == []
	assert asyncio.run(poller.poll_once()) is False
	assert publisher.events == [("availability", False)]
	assert asyncio.run(poller.poll_once()) is True

def test_partial_sensor_failure_still_publishes():
	class Partial(FakePairing):
		async def get_characteristics(self, ids):
			layout_sensor = discover_layout(self.accessories)["sensors"][0]["temperature_f"]
			return {i: ({"status": -70402} if i == layout_sensor else {"value": 1}) for i in ids}
	pairing = Partial(_ecobee(), [])
	publisher = FakePublisher()
	poller = ecobee_service.Poller("d", "D", ecobee_service.ReadOnlyPairing(pairing), publisher)
	assert asyncio.run(poller.poll_once()) is True
	assert publisher.events[0][1]["sensors"][0]["temperature_f"] is None

def test_layout_is_rebuilt_when_the_accessory_tree_is_replaced_or_ages():
	now = [0]
	pairing = FakePairing(_ecobee(with_sensor=False), ["ok"] * 5)
	poller = ecobee_service.Poller("d", "D", ecobee_service.ReadOnlyPairing(pairing), FakePublisher(), monotonic=lambda: now[0])
	asyncio.run(poller.poll_once())
	asyncio.run(poller.poll_once())
	assert pairing.listed == 1 and poller.layout["sensors"] == []
	pairing.accessories = _ecobee(with_sensor=True)  # aiohomekit swaps in a new tree on a config change
	asyncio.run(poller.poll_once())
	assert pairing.listed == 2 and len(poller.layout["sensors"]) == 1
	now[0] += ecobee_service.LAYOUT_REFRESH_SECONDS
	asyncio.run(poller.poll_once())
	assert pairing.listed == 3

def test_stop_cancels_a_hung_poll_promptly():
	class Hung(FakePairing):
		async def get_characteristics(self, ids):
			await asyncio.sleep(60)
	poller = ecobee_service.Poller("d", "D", ecobee_service.ReadOnlyPairing(Hung(_ecobee(), [])), FakePublisher())
	async def go():
		stop = asyncio.Event()
		task = asyncio.create_task(poller.run(stop))
		await asyncio.sleep(0.05)
		stop.set()
		await asyncio.wait_for(task, 1)
	asyncio.run(go())

def test_read_only_facade_exposes_no_write_methods():
	facade = ecobee_service.ReadOnlyPairing(mock.Mock())
	for name in ("put_characteristics", "add_pairing", "remove_pairing", "identify", "thermostat_write", "list_pairings"):
		assert not hasattr(facade, name)

def test_no_homekit_write_calls_anywhere_in_ecobee_code():
	forbidden = {"put_characteristics", "add_pairing", "remove_pairing", "identify", "thread_provision", "image", "set_value"}
	for fname in ("ecobee_service.py", "ecobee_state.py", "ecobee_hk.py", "ecobee_store.py"):
		with open(os.path.join(REPO, fname)) as fp:
			tree = ast.parse(fp.read())
		used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
		assert not (used & forbidden), (fname, used & forbidden)

def test_publisher_sets_retained_lwt_per_device():
	with mock.patch.object(ecobee_service.mqtt, "Client") as client_cls:
		ecobee_service.MqttPublisher("cid", "broker", 1883, "living-room")
	client_cls.assert_called_once_with("cid")
	client_cls.return_value.will_set.assert_called_once_with("upstate/ecobee/living-room/availability", "offline", qos=1, retain=True)
	client_cls.return_value.connect_async.assert_called_once_with("broker", 1883)

def _publisher():
	with mock.patch.object(ecobee_service.mqtt, "Client") as client_cls:
		pub = ecobee_service.MqttPublisher("cid", "b", 1883, "living-room")
	client = client_cls.return_value
	client.publish.return_value.rc = 0
	return pub, client

def test_publish_is_retained_on_contract_topics():
	pub, client = _publisher()
	pub._on_connect(None, None, None, 0)
	pub.publish_state({"a": 1})
	pub.publish_availability(True)
	assert client.publish.call_args_list == [
		mock.call("upstate/ecobee/living-room/state", '{"a": 1}', qos=1, retain=True),
		mock.call("upstate/ecobee/living-room/availability", "online", qos=1, retain=True),
	]

def test_nothing_is_queued_while_disconnected_and_only_latest_is_sent_on_connect():
	pub, client = _publisher()
	client.max_queued_messages_set.assert_called_once()
	for n in range(2880):
		pub.publish_state({"n": n})
		pub.publish_availability(True)
	assert client.publish.call_count == 0
	pub._on_connect(None, None, None, 0)
	assert client.publish.call_args_list == [
		mock.call("upstate/ecobee/living-room/state", '{"n": 2879}', qos=1, retain=True),
		mock.call("upstate/ecobee/living-room/availability", "online", qos=1, retain=True),
	]
	pub._on_disconnect(None, None, 1)
	pub.publish_state({"n": 1})
	assert client.publish.call_count == 2

def test_close_publishes_offline_when_connected_and_is_bounded_when_not():
	pub, client = _publisher()
	pub._on_connect(None, None, None, 0)
	ecobee_service.close_publishers([pub])
	client.publish.assert_called_with("upstate/ecobee/living-room/availability", "offline", qos=1, retain=True)
	client.disconnect.assert_called_once()
	down, down_client = _publisher()
	ecobee_service.close_publishers([down])
	assert down_client.publish.call_count == 0 and down_client.loop_stop.called

def test_store_writes_keys_private_and_atomically(tmp_path):
	path = str(tmp_path / "ecobee" / "pairings.json")
	ecobee_store.save_pairings(path, {"a": {"k": "v"}})
	assert oct(os.stat(path).st_mode & 0o777) == "0o600"
	assert oct(os.stat(os.path.dirname(path)).st_mode & 0o777) == "0o700"
	assert not os.path.exists(path + ".tmp")
	assert ecobee_store.load_pairings(path) == {"a": {"k": "v"}}
	assert ecobee_store.load_pairings(str(tmp_path / "missing.json")) == {}

def test_slug_is_topic_safe():
	assert ecobee_store.slug(" Living Room ") == "living-room"
	with pytest.raises(ValueError):
		ecobee_store.slug("///")

def test_setup_code_must_be_eight_digits():
	assert ecobee_pair._normalize_code("12345678") == "123-45-678"
	with pytest.raises(ecobee_pair.PairError):
		ecobee_pair._normalize_code("123-45-67")

def _fake_open_controller(finish):
	discovery = mock.Mock()
	discovery.description.id = "AA:BB"
	discovery.description.category = 9
	discovery.paired = False
	discovery.async_start_pairing = mock.AsyncMock(return_value=finish)
	controller = mock.Mock(discoveries={"aa:bb": discovery})
	@contextlib.asynccontextmanager
	async def opener():
		yield controller
	return opener

def test_wrong_setup_code_fails_loudly_and_stores_nothing(tmp_path, monkeypatch):
	monkeypatch.setattr(ecobee_pair, "DISCOVER_SECONDS", 0)
	monkeypatch.setattr(ecobee_pair, "open_controller", _fake_open_controller(mock.AsyncMock(side_effect=ValueError("auth"))))
	cfg = {"log_path": str(tmp_path / "x.log")}
	with pytest.raises(ecobee_pair.PairError, match="wrong setup code"):
		asyncio.run(ecobee_pair.pair(cfg, "living-room", "123-45-678"))
	assert not os.path.exists(ecobee_store.pairings_path(cfg))

def test_successful_pair_persists_keys(tmp_path, monkeypatch):
	monkeypatch.setattr(ecobee_pair, "DISCOVER_SECONDS", 0)
	paired = mock.Mock(pairing_data={"AccessoryPairingID": "AA:BB"})
	monkeypatch.setattr(ecobee_pair, "open_controller", _fake_open_controller(mock.AsyncMock(return_value=paired)))
	cfg = {"log_path": str(tmp_path / "x.log")}
	assert asyncio.run(ecobee_pair.pair(cfg, "Living Room", "12345678")) == ("living-room", "AA:BB")
	assert ecobee_store.load_pairings(ecobee_store.pairings_path(cfg)) == {"living-room": {"AccessoryPairingID": "AA:BB"}}

def test_unwritable_storage_is_caught_before_pairing(tmp_path, monkeypatch):
	monkeypatch.setattr(ecobee_pair, "DISCOVER_SECONDS", 0)
	finish = mock.AsyncMock()
	monkeypatch.setattr(ecobee_pair, "open_controller", _fake_open_controller(finish))
	blocker = tmp_path / "blocked"
	blocker.write_text("a file where the ecobee data dir should be")
	cfg = {"log_path": str(blocker / "x.log")}
	with pytest.raises(ecobee_pair.PairError, match="nothing was paired"):
		asyncio.run(ecobee_pair.pair(cfg, "living-room", "12345678"))
	finish.assert_not_called()

def test_save_failure_after_pairing_leaves_a_private_recovery_file(tmp_path, monkeypatch):
	monkeypatch.setattr(ecobee_pair, "DISCOVER_SECONDS", 0)
	paired = mock.Mock(pairing_data={"AccessoryPairingID": "AA:BB", "iOSDeviceLTSK": "secret"})
	monkeypatch.setattr(ecobee_pair, "open_controller", _fake_open_controller(mock.AsyncMock(return_value=paired)))
	monkeypatch.setattr(ecobee_pair, "save_pairings", mock.Mock(side_effect=OSError("disk full")))
	monkeypatch.setenv("HOME", str(tmp_path))
	cfg = {"log_path": str(tmp_path / "data" / "x.log")}
	with pytest.raises(ecobee_pair.PairError, match="PAIRED, but saving"):
		asyncio.run(ecobee_pair.pair(cfg, "living-room", "12345678"))
	recovery = tmp_path / "data" / "ecobee" / "ecobee-pairing-living-room.recovery.json"
	assert oct(os.stat(recovery).st_mode & 0o777) == "0o600"
	assert "secret" in recovery.read_text()

def test_save_failure_with_no_recovery_location_prints_the_keys(tmp_path, monkeypatch):
	monkeypatch.setattr(ecobee_pair, "DISCOVER_SECONDS", 0)
	paired = mock.Mock(pairing_data={"AccessoryPairingID": "AA:BB", "iOSDeviceLTSK": "secret"})
	monkeypatch.setattr(ecobee_pair, "open_controller", _fake_open_controller(mock.AsyncMock(return_value=paired)))
	monkeypatch.setattr(ecobee_pair, "save_pairings", mock.Mock(side_effect=OSError("ro fs")))
	monkeypatch.setattr(ecobee_pair, "write_recovery", lambda *a: None)
	cfg = {"log_path": str(tmp_path / "x.log")}
	with pytest.raises(ecobee_pair.PairError, match="ONLY COPY") as exc:
		asyncio.run(ecobee_pair.pair(cfg, "living-room", "12345678"))
	assert "secret" in str(exc.value)

def test_two_unpaired_thermostats_need_an_explicit_device_id(tmp_path, monkeypatch):
	monkeypatch.setattr(ecobee_pair, "DISCOVER_SECONDS", 0)
	opener = _fake_open_controller(mock.AsyncMock())
	async def two():
		async with opener() as controller:
			controller.discoveries["cc:dd"] = mock.Mock(paired=False, description=mock.Mock(id="CC:DD", category=9))
			with pytest.raises(ecobee_pair.PairError, match="--device-id"):
				await ecobee_pair._find_unpaired(controller, None)
	asyncio.run(two())

def test_service_stops_within_deadline_with_a_hung_read_and_closes_publishers(tmp_path, monkeypatch):
	class Hung(FakePairing):
		async def get_characteristics(self, ids):
			await asyncio.sleep(60)
	controller = mock.Mock()
	controller.load_pairing.return_value = Hung(_ecobee(), [])
	@contextlib.asynccontextmanager
	async def opener():
		yield controller
	monkeypatch.setattr(ecobee_service, "open_controller", opener)
	cfg = {"log_path": str(tmp_path / "x.log"), "client_id": "c", "broker_host": "b", "broker_port": 1883}
	ecobee_store.save_pairings(ecobee_store.pairings_path(cfg), {"living-room": {}})
	closed = []
	monkeypatch.setattr(ecobee_service, "close_publishers", lambda pubs: closed.extend(pubs))
	async def go():
		stop = asyncio.Event()
		with mock.patch.object(ecobee_service.mqtt, "Client"):
			task = asyncio.create_task(ecobee_service.run_service(cfg, stop))
			await asyncio.sleep(0.1)
			stop.set()
			await asyncio.wait_for(task, 2)
	asyncio.run(go())
	assert len(closed) == 1
