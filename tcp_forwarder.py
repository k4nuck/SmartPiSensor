#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  tcp_forwarder.py
#
# Exposes LAN-only ports of the upstate SHIELD on this Pi's tailnet address so ha-main can reach them (gergy-incidents#1896):
#   tcp_forwarder.py --bind 100.123.50.43 --target 192.168.1.62 --ports 6466,6467,5555
# Stdlib only. Every setting is a flag or a TCP_FORWARDER_* env var; nothing about the SHIELD is hard-coded here.

import argparse
import asyncio
import ipaddress
import logging
import os
import signal
import sys

CONNECT_TIMEOUT_SECONDS = 5
BUFFER_BYTES = 65536
# Tailscale hands out IPv4 from the CGNAT range (and IPv6 from this ULA prefix).
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")

class ForwarderError(Exception):
	pass

# The bind address is the only thing keeping the SHIELD off the in-laws' LAN, so anything that is not a tailnet
# address (0.0.0.0, a LAN IP, a hostname that could resolve to either) is refused instead of trusted.
def validate_bind(value):
	try:
		addr = ipaddress.ip_address((value or "").strip())
	except ValueError:
		raise ForwarderError("bind must be this Pi's tailnet IP address, got %r" % value)
	if addr not in TAILNET_V4 and addr not in TAILNET_V6:
		raise ForwarderError("bind %s is not a tailnet address (100.64.0.0/10); refusing to expose the SHIELD on another network" % addr)
	return str(addr)

def parse_ports(value):
	try:
		ports = [int(p) for p in str(value).split(",") if p.strip()]
	except ValueError:
		raise ForwarderError("ports must be a comma-separated list of integers, got %r" % value)
	if not ports or any(not 0 < p < 65536 for p in ports) or len(set(ports)) != len(ports):
		raise ForwarderError("ports must be unique values in 1-65535, got %r" % value)
	return ports

async def _pipe(reader, writer):
	try:
		while True:
			data = await reader.read(BUFFER_BYTES)
			if not data:
				break
			writer.write(data)
			await writer.drain()
	except (ConnectionError, OSError):
		pass
	finally:
		try:
			# Half-close so the peer sees EOF but can still finish sending its own direction.
			if writer.can_write_eof():
				writer.write_eof()
		except (ConnectionError, OSError, RuntimeError):
			pass

async def _handle(client_reader, client_writer, target, port, connect, tasks):
	peer = client_writer.get_extra_info("peername")
	upstream_writer = None
	task = asyncio.current_task()
	tasks.add(task)
	try:
		upstream_reader, upstream_writer = await asyncio.wait_for(
			connect(target, port), CONNECT_TIMEOUT_SECONDS)
		await asyncio.gather(_pipe(client_reader, upstream_writer), _pipe(upstream_reader, client_writer))
	except (OSError, asyncio.TimeoutError) as e:
		# A powered-off SHIELD is routine; the client just sees its connection close.
		logging.info("forward %s -> %s:%d failed: %s", peer, target, port, e or type(e).__name__)
	finally:
		tasks.discard(task)
		for w in (client_writer, upstream_writer):
			if w is not None:
				w.close()

# `connect` is injectable so tests can point the upstream side at a local fake.
# `tasks` collects the live per-connection handlers so run() can cancel them on shutdown.
async def start_servers(bind, target, ports, connect=asyncio.open_connection, tasks=None):
	tasks = set() if tasks is None else tasks
	servers = []
	try:
		for port in ports:
			servers.append(await asyncio.start_server(
				lambda r, w, p=port: _handle(r, w, target, p, connect, tasks), host=bind, port=port))
	except OSError:
		# All or nothing: a half-bound forwarder would look healthy to systemd while a port is dead.
		for s in servers:
			s.close()
		raise
	return servers

async def run(bind, target, ports, stop, connect=asyncio.open_connection):
	tasks = set()
	servers = await start_servers(bind, target, ports, connect=connect, tasks=tasks)
	logging.info("forwarding %s:%s -> %s", bind, ports, target)
	await stop.wait()
	for s in servers:
		s.close()
	# Python 3.12+ Server.wait_closed() blocks until every accepted connection ends, and a long-lived remote/adb
	# session never would, so systemd would SIGKILL us. Cancel the handlers (their finally closes both sockets) first.
	# Listeners are already closed above, so no new handler can join `tasks` while we drain it.
	pending = list(tasks)
	for t in pending:
		t.cancel()
	await asyncio.gather(*pending, return_exceptions=True)
	await asyncio.gather(*(s.wait_closed() for s in servers), return_exceptions=True)

def parse_args(argv, environ=None):
	environ = os.environ if environ is None else environ
	p = argparse.ArgumentParser(description=__doc__)
	p.add_argument("--bind", default=environ.get("TCP_FORWARDER_BIND"), help="this Pi's tailnet IP (required)")
	p.add_argument("--target", default=environ.get("TCP_FORWARDER_TARGET"), help="LAN IP to forward to (required)")
	p.add_argument("--ports", default=environ.get("TCP_FORWARDER_PORTS", "6466,6467,5555"))
	args = p.parse_args(argv)
	if not args.target:
		raise ForwarderError("target is required (--target or TCP_FORWARDER_TARGET)")
	return validate_bind(args.bind), args.target, parse_ports(args.ports)

async def _main(bind, target, ports):
	stop = asyncio.Event()
	loop = asyncio.get_running_loop()
	for sig in (signal.SIGTERM, signal.SIGINT):
		loop.add_signal_handler(sig, stop.set)
	await run(bind, target, ports, stop)

def main(argv=None):
	logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
	try:
		bind, target, ports = parse_args(sys.argv[1:] if argv is None else argv)
		asyncio.run(_main(bind, target, ports))
	except ForwarderError as e:
		logging.error("%s", e)
		return 2
	except OSError as e:
		# Usually tailscaled has not assigned the address yet; exit non-zero so Restart=always retries.
		logging.error("cannot listen: %s", e)
		return 1
	return 0

if __name__ == "__main__":
	sys.exit(main())
