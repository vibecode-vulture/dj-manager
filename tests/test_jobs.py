import sys
import threading
import time

from conftest import wait as wait_done
from djmanager.jobs import JobRunner
from djmanager.procs import release, spawn


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while not cond():
        if time.time() > end:
            raise TimeoutError
        time.sleep(0.02)


def test_stop_kills_running_process_tree():
    runner = JobRunner()
    started = threading.Event()
    procs = []

    def run(job):
        # a child that itself starts a grandchild - both must die on Stop
        code = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"
        proc = spawn([sys.executable, "-c", code])
        procs.append(proc)
        started.set()
        try:
            proc.wait()
        finally:
            release(proc)
        job.check_cancelled()
        return "finished"

    job = runner.submit("long", run)
    assert started.wait(10)
    t = time.time()
    runner.cancel(job.id)
    wait_for(lambda: job.status not in ("queued", "running"))
    assert job.status == "cancelled"
    assert procs[0].poll() is not None
    assert time.time() - t < 6


def test_cancel_queued_job_and_dedupe():
    runner = JobRunner()
    gate = threading.Event()
    first = runner.submit("Update all playlists", lambda job: gate.wait(10) and "ok", dedupe=True)
    wait_for(lambda: first.status == "running")
    again = runner.submit("Update all playlists", lambda job: "never", dedupe=True)
    assert again is first  # no duplicate queued behind the running one

    queued = runner.submit("other", lambda job: "never")
    runner.cancel(queued.id)
    assert queued.status == "cancelled"
    gate.set()
    wait_done(first)
    assert first.status == "done"
