"""Background jobs (scan, schedule build, transcode) run one at a time in a worker thread."""

from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from .events import EventBus


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
                "error": self.error, "result": self.result, "notes": self.notes[-50:]}


class JobRunner:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._jobs: dict[int, Job] = {}
        self._queue: list[tuple[Job, Callable[[Job], Any]]] = []
        self._lock = threading.Lock()
        self._next_id = 1
        self._worker: threading.Thread | None = None
        self.current: Job | None = None

    def submit(self, kind: str, label: str, fn: Callable[[Job], Any]) -> Job:
        with self._lock:
            job = Job(id=self._next_id, kind=kind, label=label)
            self._next_id += 1
            self._jobs[job.id] = job
            self._queue.append((job, fn))
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, name="pitv-jobs", daemon=True)
                self._worker.start()
        self.bus.publish_threadsafe("job", job.public())
        return job

    def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def recent(self, n: int = 20) -> list[dict[str, Any]]:
        return [j.public() for j in list(self._jobs.values())[-n:]]

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
                self.current = job
            job.status = "running"
            self.bus.publish_threadsafe("job", job.public())
            try:
                job.result = fn(job)
                job.status = "done"
            except Exception as exc:  # noqa: BLE001
                job.status = "failed"
                job.error = f"{exc!r}"
                job.notes.append(traceback.format_exc()[-2000:])
            finally:
                self.current = None
            self.bus.publish_threadsafe("job", job.public())
