"""Child processes that can be stopped.

Every long-running tool (spotdl, pip, ffmpeg via spotdl) is started through spawn():
  * it gets its own process group, so stopping it also stops its children,
  * it is registered with the job that started it, so the job's Stop button kills it,
  * on Linux it dies with DJ Manager even if DJ Manager crashes (PR_SET_PDEATHSIG),
  * kill_all() at shutdown stops whatever is still running.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import signal
import subprocess
import sys
import threading
import time

from . import paths
from .jobs import JobCancelled, current_job

_lock = threading.Lock()
_running: set[subprocess.Popen] = set()

_LIBC = None
if sys.platform.startswith("linux"):
    try:
        _LIBC = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    except OSError:
        _LIBC = None


def _linux_die_with_parent() -> None:  # runs in the child between fork and exec
    if _LIBC is not None:
        _LIBC.prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG


def spawn(args: list[str], **kwargs) -> subprocess.Popen:
    job = current_job.get()
    if job is not None and job.cancel_requested:
        raise JobCancelled()
    if paths.IS_WINDOWS:
        flags = kwargs.pop("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        kwargs["creationflags"] = flags | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
        if _LIBC is not None:
            kwargs["preexec_fn"] = _linux_die_with_parent
    proc = subprocess.Popen(args, **kwargs)
    with _lock:
        _running.add(proc)
    if job is not None:
        job.processes.add(proc)
        if job.cancel_requested:  # cancelled while starting
            kill_tree(proc)
    return proc


def release(proc: subprocess.Popen) -> None:
    with _lock:
        _running.discard(proc)
    job = current_job.get()
    if job is not None:
        job.processes.discard(proc)


def kill_tree(proc: subprocess.Popen, grace: float = 3.0) -> None:
    """Stop a process and everything it started: politely first, then forcefully."""
    if proc.poll() is not None:
        return
    try:
        if paths.IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
            return
        os.killpg(proc.pid, signal.SIGTERM)
        end = time.time() + grace
        while time.time() < end and proc.poll() is None:
            time.sleep(0.1)
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def kill_all() -> None:
    with _lock:
        procs = list(_running)
    for proc in procs:
        kill_tree(proc, grace=1.0)
