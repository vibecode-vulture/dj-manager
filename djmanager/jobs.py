"""Background jobs: one at a time, with a live log the UI can poll."""

from __future__ import annotations

import threading
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .util import now_iso


@dataclass
class Job:
    id: str
    title: str
    status: str = "queued"  # queued | running | done | failed
    started_at: str = ""
    finished_at: str = ""
    result: str = ""
    error: str = ""
    log: list[str] = field(default_factory=list)
    progress: float | None = None

    def write(self, line: str) -> None:
        self.log.append(line)
        if len(self.log) > 5000:
            del self.log[:1000]

    def summary(self, since: int = 0) -> dict:
        return {
            "id": self.id, "title": self.title, "status": self.status, "started_at": self.started_at,
            "finished_at": self.finished_at, "result": self.result, "error": self.error,
            "progress": self.progress, "log_offset": since, "log": self.log[since:], "log_size": len(self.log),
        }


class JobRunner:
    def __init__(self) -> None:
        self._queue: deque[tuple[Job, Callable[[Job], str | None]]] = deque()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._cv = threading.Condition()
        self.current: Job | None = None
        threading.Thread(target=self._worker, daemon=True, name="djm-jobs").start()

    def submit(self, title: str, func: Callable[[Job], str | None]) -> Job:
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
            try:
                job.result = func(job) or ""
                job.status = "done"
            except Exception as exc:  # noqa: BLE001 - report everything to the UI
                job.error = str(exc)
                job.write("ERROR: " + str(exc))
                job.write(traceback.format_exc())
                job.status = "failed"
            finally:
                job.finished_at = now_iso()
                with self._cv:
                    self.current = None
