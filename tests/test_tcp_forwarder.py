import asyncio

import pytest

import tcp_forwarder
from tcp_forwarder import ForwarderError, validate_bind, parse_ports, parse_args, start_servers

@pytest.mark.parametrize("value", ["100.123.50.43", "100.64.0.1", "100.127.255.254", "fd7a:115c:a1e0::1"])
def test_validate_bind_accepts_tailnet(value):
	assert validate_bind(value) == value

@pytest.mark.parametrize("value", ["0.0.0.0", "::", "", None, "192.168.1.5", "127.0.0.1", "100.128.0.1", "100.63.255.255", "ha-main", "localhost"])
def test_validate_bind_rejects_everything_else(value):
	with pytest.raises(ForwarderError):
		validate_bind(value)

def test_parse_ports():
	assert parse_ports("6466, 6467,5555") == [6466, 6467, 5555]
	for bad in ["", "abc", "0", "70000", "1,1"]:
		with pytest.raises(ForwarderError):
			parse_ports(bad)

def test_parse_args_requires_bind_and_target():
	with pytest.raises(ForwarderError):
		parse_args([], environ={})
	with pytest.raises(ForwarderError):
		parse_args(["--bind", "100.64.1.1"], environ={})
	with pytest.raises(ForwarderError):
		parse_args(["--target", "10.0.0.1"], environ={})
	assert parse_args([], environ={"TCP_FORWARDER_BIND": "100.64.1.1", "TCP_FORWARDER_TARGET": "10.0.0.1"}) == ("100.64.1.1", "10.0.0.1", [6466, 6467, 5555])
	assert parse_args(["--bind", "100.64.1.1", "--target", "t", "--ports", "1,2"], environ={}) == ("100.64.1.1", "t", [1, 2])

def _free_port():
	import socket
	s = socket.socket()
	s.bind(("127.0.0.1", 0))
	port = s.getsockname()[1]
	s.close()
	return port

def test_forwards_both_directions_and_closes_servers():
	async def go():
		got = []
		async def echo(r, w):
			data = await r.read(100)
			got.append(data)
			w.write(b"re:" + data)
			await w.drain()
			w.close()
		upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
		uport = upstream.sockets[0].getsockname()[1]
		fport = _free_port()
		async def open_conn(host, port):
			return await asyncio.open_connection(host, uport)
		try:
			servers = await start_servers("127.0.0.1", "127.0.0.1", [fport], connect=open_conn)
			r, w = await asyncio.open_connection("127.0.0.1", fport)
			w.write(b"hello")
			await w.drain()
			assert await asyncio.wait_for(r.read(100), 3) == b"re:hello"
			w.close()
		finally:
			for s in servers:
				s.close()
			upstream.close()
		assert got == [b"hello"]
	asyncio.run(go())

def test_unreachable_target_closes_client_without_crashing():
	async def go():
		dead = _free_port()
		fport = _free_port()
		async def open_conn(host, port):
			return await asyncio.open_connection("127.0.0.1", dead)
		try:
			servers = await start_servers("127.0.0.1", "x", [fport], connect=open_conn)
			r, w = await asyncio.open_connection("127.0.0.1", fport)
			assert await asyncio.wait_for(r.read(100), 3) == b""
			w.close()
			# Still serving after a failed upstream.
			r, w = await asyncio.open_connection("127.0.0.1", fport)
			w.close()
		finally:
			for s in servers:
				s.close()
	asyncio.run(go())

def test_port_in_use_binds_nothing():
	async def go():
		busy = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
		busy_port = busy.sockets[0].getsockname()[1]
		free = _free_port()
		with pytest.raises(OSError):
			await start_servers("127.0.0.1", "x", [free, busy_port])
		# The first port must have been released again.
		s = await asyncio.start_server(lambda r, w: None, "127.0.0.1", free)
		s.close()
		busy.close()
	asyncio.run(go())

# An idle, long-lived connection (like a Remote v2 or adb session) must not stop run() from returning on stop.
def test_run_shuts_down_with_active_connection():
	async def go():
		async def hold_open(r, w):
			await r.read()
		upstream = await asyncio.start_server(hold_open, "127.0.0.1", 0)
		uport = upstream.sockets[0].getsockname()[1]
		fport = _free_port()
		async def open_conn(host, port):
			return await asyncio.open_connection(host, uport)
		stop = asyncio.Event()
		task = asyncio.ensure_future(tcp_forwarder.run("127.0.0.1", "x", [fport], stop, connect=open_conn))
		try:
			for _ in range(50):
				try:
					r, w = await asyncio.open_connection("127.0.0.1", fport)
					break
				except OSError:
					await asyncio.sleep(0.05)
			w.write(b"x")
			await w.drain()
			await asyncio.sleep(0.2)  # let the forward get established
			stop.set()
			await asyncio.wait_for(task, 3)
			# The client side was closed by the forwarder, not left hanging.
			assert await asyncio.wait_for(r.read(100), 3) == b""
			w.close()
		finally:
			task.cancel()
			upstream.close()
	asyncio.run(go())
