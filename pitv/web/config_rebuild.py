"""The schedule follows the configuration without anyone pressing Rebuild.

A band's hours, a channel's pattern, a line-up entry or a watershed changed in the admin used to
reach only the days built after it: everything already built kept the old shape until the owner
thought to press "Rebuild week", and the guide said one thing while the settings said another.
The configuration is the authority on what the schedule looks like, so a change to it rebuilds
what it governs from now on.

Edits come in runs (a band's start, then its length, then its genres), so a change waits a few
seconds for the next before anything is built, and the channels named in between are rebuilt
together in one job. What a rebuild always keeps is unchanged: what is on air, locked slots, and
remote programmes already promised inside the lead window."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from typing import Any

from .. import db as dbm
from ..scheduler.horizon import build_horizon
from .tasks import JobRunner

log = logging.getLogger("pitv.web")

SETTLE_SECONDS = 10.0   # how long a change waits for the next one before the rebuild starts


class ConfigRebuild:
    def __init__(self, jobs: JobRunner, db_path: Any, on_done: Callable[[], None],
                 settle: float = SETTLE_SECONDS) -> None:
        self.jobs = jobs
        self.db_path = db_path
        self.on_done = on_done
        self.settle = settle
        self._lock = threading.Lock()
        self._channels: set[int] | None = set()   # None: every channel
        self._reasons: list[str] = []
        self._timer: threading.Timer | None = None

    def request(self, channel_numbers: Iterable[int] | None, reason: str) -> None:
        """Rebuild these channels (None: all of them) from now, once the edits stop."""
        with self._lock:
            if channel_numbers is None or self._channels is None:
                self._channels = None
            else:
                self._channels |= {int(n) for n in channel_numbers}
            self._reasons.append(reason)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.settle, self._submit)
            self._timer.daemon = True
            self._timer.start()

    def _submit(self) -> None:
        with self._lock:
            channels, reasons = self._channels, self._reasons
            self._channels, self._reasons, self._timer = set(), [], None
        if channels is not None and not channels:
            return
        numbers = sorted(channels) if channels is not None else None
        which = "every channel" if numbers is None else "channel " + ", ".join(map(str, numbers))
        label = f"Rebuild {which} after a settings change"

        def run(job: Any) -> dict[str, Any]:
            conn = dbm.connect(self.db_path)
            try:
                log.info("%s: %s", label, "; ".join(dict.fromkeys(reasons)))
                result = build_horizon(conn, force=True, channel_numbers=numbers,
                                       progress=lambda m: self.jobs.progress(job, m))
                job.notes.extend(result.get("notes", []))
                return result
            finally:
                conn.close()
                self.on_done()
        self.jobs.submit("schedule", label, run)
