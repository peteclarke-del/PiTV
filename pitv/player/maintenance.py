"""Housekeeping thread inside the player: keep the schedule horizon topped up, run the
nightly library scan, apply pitv_content reports, evict from the cache, run the readiness
checks and trim old history. Keeping this in the always-running player means no cron or
systemd timers are needed on the Pi. The thread has its own database connection."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..content import apply_report_files, protect_manifest
from ..db import all_settings, connect, now_ts, tx
from ..library.scanner import scan_all
from ..readiness import check as readiness_check
from ..scheduler.build import build_horizon, needs_rebuild
from ..wanted import queue_gaps
from .cache import MediaCache

log = logging.getLogger("pitv.maintenance")

STARTUP_DELAY = 20      # seconds; let playback start before the first pass
PASS_INTERVAL = 600     # seconds between passes


class Maintenance:
    def __init__(self, db_path: Path, ffprobe: str, clock: Callable[[], int],
                 on_schedule_changed: Callable[[], None], cache: MediaCache) -> None:
        self.db_path = db_path
        self.cache = cache
        self.ffprobe = ffprobe
        self.clock = clock
        self.on_schedule_changed = on_schedule_changed
        self._stop = threading.Event()
        self._readiness_done: set[str] = set()   # "YYYY-MM-DD:hour" stamps already checked
        self.status: dict[str, Any] = {"last_build": None, "last_scan": None, "last_readiness": None, "error": None}

    def start(self) -> None:
        threading.Thread(target=self._loop, name="pitv-maintenance", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        time.sleep(STARTUP_DELAY)
        while not self._stop.is_set():
            try:
                self._once()
            except Exception as exc:  # noqa: BLE001 - one bad pass must not end housekeeping
                log.exception("maintenance failed")
                self.status["error"] = repr(exc)
            self._stop.wait(PASS_INTERVAL)

    def _once(self) -> None:
        conn = connect(self.db_path)
        try:
            now = self.clock()
            settings = all_settings(conn)
            if needs_rebuild(conn, now):
                log.info("schedule horizon short; building")
                result = build_horizon(conn, now=now)
                self.status["last_build"] = {"at": now_ts(), **{k: result[k] for k in ("status", "summary")}}
                log.info("schedule: %s", result["summary"])
                self.on_schedule_changed()
            local = datetime.fromtimestamp(now)
            today = local.date().isoformat()
            row = conn.execute("SELECT MAX(started_at) AS t FROM run_log WHERE kind = 'scan' AND status != 'running'").fetchone()
            last_scan = datetime.fromtimestamp(row["t"]).date().isoformat() if row and row["t"] else None
            if local.hour == int(settings.get("scan_hour", 4)) and last_scan != today:
                log.info("nightly scan")
                scan_all(conn, ffprobe_binary=self.ffprobe)
                self.status["last_scan"] = now_ts()
                if needs_rebuild(conn, now):
                    build_horizon(conn, now=now)
                    self.on_schedule_changed()
            if apply_report_files(conn, self.cache):
                self.on_schedule_changed()
            if settings.get("acquire_fill_gaps"):
                queue_gaps(conn)
            if self.cache.enabled and not self.cache.content_tool_running():
                protect_manifest(conn, self.cache, now=now)
                self.cache.make_room()
            # Readiness: is tomorrow (and the rest of today) actually playable? Once at each of
            # the configured hours (default 06:00 and 07:00), plus the first pass after boot.
            hours = [int(h) for h in (settings.get("readiness_hours") or [6, 7])]
            stamp = f"{today}:{local.hour}"
            if (local.hour in hours and stamp not in self._readiness_done) or not self._readiness_done:
                self._readiness_done.add(stamp)
                result = readiness_check(conn, now=now, days=1)
                self.status["last_readiness"] = {"at": now_ts(), "status": result["status"], "summary": result["summary"]}
                if result["substituted"]:
                    self.on_schedule_changed()
            keep = int(settings.get("history_keep_days", 180)) * 86400
            with tx(conn):
                conn.execute("DELETE FROM history WHERE started_at < ?", (now - keep,))
                conn.execute("DELETE FROM schedule WHERE end_ts < ?", (now - 14 * 86400,))
                conn.execute("DELETE FROM run_log WHERE started_at < ?", (now - 30 * 86400,))
        finally:
            conn.close()
