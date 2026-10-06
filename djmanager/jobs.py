"""Background jobs: one at a time, with a live log the UI can poll."""

from __future__ import annotations

import contextvars
import threading
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .util import now_iso


class JobCancelled(Exception):
    """Raised inside a job after the user pressed Stop."""


# The job running in the current thread; procs.spawn() registers child processes with it.
current_job: contextvars.ContextVar["Job | None"] = contextvars.ContextVar("current_job", default=None)


@dataclass
class Job:
    id: str
    title: str
    status: str = "queued"  # queued | running | done | failed | cancelled
    started_at: str = ""
    finished_at: str = ""
    result: str = ""
    error: str = ""
    log: list[str] = field(default_factory=list)
    progress: float | None = None
    cancel_requested: bool = False
    processes: set[Any] = field(default_factory=set, repr=False)

    def check_cancelled(self) -> None:
        if self.cancel_requested:
            raise JobCancelled()

    def write(self, line: str) -> None:
        self.log.append(line)
        if len(self.log) > 5000:
            del self.log[:1000]

    def summary(self, since: int = 0) -> dict:
        return {
            "id": self.id, "title": self.title, "status": self.status, "started_at": self.started_at,
            "finished_at": self.finished_at, "result": self.result, "error": self.error,
            "progress": self.progress, "cancel_requested": self.cancel_requested, "log_offset": since, "log": self.log[since:], "log_size": len(self.log),
        }


class JobRunner:
    def __init__(self) -> None:
        self._queue: deque[tuple[Job, Callable[[Job], str | None]]] = deque()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._cv = threading.Condition()
        self.current: Job | None = None
        threading.Thread(target=self._worker, daemon=True, name="djm-jobs").start()

    def submit(self, title: str, func: Callable[[Job], str | None], dedupe: bool = False) -> Job:
        """Queue a job. With dedupe, an identical job that is queued or running is reused."""
        with self._cv:
            if dedupe:
                for active in [self.current, *(j for j, _ in self._queue)]:
                    if active is not None and active.title == title and not active.cancel_requested:
                        return active
        job = Job(id=uuid.uuid4().hex[:10], title=title)
        with self._cv:
            self._jobs[job.id] = job
            self._order.append(job.id)
            if len(self._order) > 50:
                self._jobs.pop(self._order.pop(0), None)
            self._queue.append((job, func))
            self._cv.notify()
        return job

    @property
    def busy(self) -> bool:
        with self._cv:
            return self.current is not None or bool(self._queue)

    def cancel(self, job_id: str) -> Job | None:
        """Stop a job: drop it from the queue, or kill its processes if it is running."""
        from .procs import kill_tree  # procs imports this module

        with self._cv:
            job = self._jobs.get(job_id)
            if job is None or job.status not in ("queued", "running"):
                return job
            job.cancel_requested = True
            for queued in list(self._queue):
                if queued[0] is job:
                    self._queue.remove(queued)
                    job.status = "cancelled"
                    job.finished_at = now_iso()
                    job.write("Cancelled before it started")
                    return job
            processes = list(job.processes)
        job.write("Stopping ...")
        for proc in processes:
            kill_tree(proc)
        return job

    def cancel_all(self) -> None:
        for job in [self.current, *(j for j, _ in list(self._queue))]:
            if job is not None:
                self.cancel(job.id)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def recent(self) -> list[Job]:
        return [self._jobs[i] for i in reversed(self._order) if i in self._jobs]

    def _worker(self) -> None:
        while True:
            with self._cv:
                while not self._queue:
                    self._cv.wait()
                job, func = self._queue.popleft()
                self.current = job
            job.status = "running"
            job.started_at = now_iso()
            token = current_job.set(job)
            try:
                job.result = func(job) or ""
                job.status = "cancelled" if job.cancel_requested else "done"
            except JobCancelled:
                job.status = "cancelled"
                job.write("Stopped")
            except Exception as exc:  # noqa: BLE001 - report everything to the UI
                job.error = str(exc)
                job.write("ERROR: " + str(exc))
                job.write(traceback.format_exc())
                job.status = "cancelled" if job.cancel_requested else "failed"
            finally:
                current_job.reset(token)
                job.finished_at = now_iso()
                with self._cv:
                    self.current = None
