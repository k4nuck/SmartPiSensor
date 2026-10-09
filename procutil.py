#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  procutil.py

import logging
import time

CHILD_STOP_SECONDS = 2

def _attempt(action, what):
	try:
		return action()
	except Exception:
		logging.warning("procutil: %s failed" % what, exc_info=True)
		return None

# Non-daemon children block interpreter exit, so a child that ignores SIGTERM (e.g. SIGSTOPped)
# must be escalated to SIGKILL. Every step is isolated so one bad child can't abort cleanup of the rest.
def stop_processes(procs, timeout=CHILD_STOP_SECONDS):
	# join()/is_alive() assert on a Process whose start() never ran (failed or interrupted startup)
	started = [p for p in procs if p is not None and p.pid is not None]

	for proc in started:
		if _attempt(proc.is_alive, "is_alive"):
			_attempt(proc.terminate, "terminate")

	# One shared deadline keeps total stop time bounded no matter how many children there are
	deadline = time.monotonic() + timeout
	for proc in started:
		_attempt(lambda: proc.join(max(0, deadline - time.monotonic())), "join")

	stragglers = [p for p in started if _attempt(p.is_alive, "is_alive")]
	for proc in stragglers:
		logging.warning("procutil: pid %s ignored SIGTERM, killing" % proc.pid)
		_attempt(proc.kill, "kill")
	deadline = time.monotonic() + timeout
	for proc in stragglers:
		_attempt(lambda: proc.join(max(0, deadline - time.monotonic())), "join after kill")
