"""Background jobs (catalogue import, schedule build) run one at a time in a worker thread."""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .events import EventBus

MAX_JOBS_KEPT = 50    # finished jobs kept for the admin page; older ones are dropped
MAX_NOTES = 50        # notes kept per job; a schedule build can produce thousands


@dataclass
class Job:
    id: int
    kind: str
    label: str
    status: str = "queued"     # queued | running | done | failed
    message: str = ""
    done: int = 0
    total: int = 0
    result: Any = None
    error: str = ""
    notes: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
                "message": self.message, "done": self.done, "total": self.total,
                "error": self.error, "result": self.result, "notes": self.notes[-MAX_NOTES:]}


class JobRunner:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._jobs: dict[int, Job] = {}
        self._queue: list[tuple[Job, Callable[[Job], Any]]] = []
        self._lock = threading.Lock()
        self._next_id = 1
        self._worker: threading.Thread | None = None

    def submit(self, kind: str, label: str, fn: Callable[[Job], Any]) -> Job:
        with self._lock:
            job = Job(id=self._next_id, kind=kind, label=label)
            self._next_id += 1
            self._jobs[job.id] = job
            for old in [j for j in self._jobs.values() if j.status in ("done", "failed")][:-MAX_JOBS_KEPT]:
                del self._jobs[old.id]
            self._queue.append((job, fn))
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, name="pitv-jobs", daemon=True)
                self._worker.start()
        self.bus.publish_threadsafe("job", job.public())
        return job

    def recent(self, n: int = 20) -> list[dict[str, Any]]:
        with self._lock:   # submit() mutates the dict from request threads
            jobs = list(self._jobs.values())[-n:]
        return [j.public() for j in jobs]

    def progress(self, job: Job, message: str, done: int = 0, total: int = 0) -> None:
        job.message, job.done, job.total = message, done, total
        self.bus.publish_threadsafe("job", job.public())

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._worker = None
                    return
                job, fn = self._queue.pop(0)
            job.status = "running"
            self.bus.publish_threadsafe("job", job.public())
            try:
                job.result = fn(job)
                job.status = "done"
            except Exception as exc:  # noqa: BLE001 - reported on the job, never kills the worker
                job.status = "failed"
                job.error = f"{exc!r}"
                job.notes.append(traceback.format_exc()[-2000:])
            del job.notes[:-MAX_NOTES]
            self.bus.publish_threadsafe("job", job.public())
