import os
import signal
import subprocess
import time
import multiprocessing
from procutil import stop_processes

class Fake:
	def __init__(self, pid=1, alive=True):
		self.pid, self.alive, self.terminated, self.killed = pid, alive, False, False
	def is_alive(self): return self.alive
	def terminate(self): self.terminated = True; self.alive = False
	def kill(self): self.killed = True; self.alive = False
	def join(self, t=None): pass

def test_unstarted_process_is_skipped_and_rest_still_stopped():
	class Unstarted(Fake):
		def join(self, t=None): raise AssertionError("can only join a started process")
	live = Fake()
	stop_processes([Unstarted(pid=None), None, live])
	assert live.terminated

def test_failing_step_does_not_abort_other_cleanup():
	class Broken(Fake):
		def terminate(self): raise OSError("nope")
	live = Fake()
	stop_processes([Broken(), live])
	assert live.terminated

def test_sigterm_ignoring_process_is_killed():
	stubborn = Fake()
	stubborn.terminate = lambda: None
	stop_processes([stubborn])
	assert stubborn.killed

def _sleeper():
	time.sleep(60)

def test_real_stopped_process_is_killed_within_bound():
	proc = multiprocessing.Process(target=_sleeper)
	proc.start()
	os.kill(proc.pid, signal.SIGSTOP)  # SIGTERM stays pending on a stopped process
	start = time.monotonic()
	stop_processes([proc], timeout=1)
	assert time.monotonic() - start < 5
	assert not proc.is_alive()
