"""Housekeeping thread inside the player: keep the schedule horizon topped up, run the
nightly library scan, and trim old history. Keeping this in the always-running player
means no cron or systemd timers are needed on the Pi."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..db import all_settings, connect, now_ts, tx
from ..scheduler.build import build_horizon, needs_rebuild

log = logging.getLogger("pitv.maintenance")


class Maintenance:
    def __init__(self, db_path: Path, ffprobe: str, clock: Callable[[], int],
                 on_schedule_changed: Callable[[], None], cache=None) -> None:
        self.db_path = db_path
        self.cache = cache
        self.ffprobe = ffprobe
        self.clock = clock
        self.on_schedule_changed = on_schedule_changed
        self._stop = threading.Event()
        self.last_scan_day: str | None = None
        self._readiness_done: set[str] = set()
        self.status = {"last_build": None, "last_scan": None, "last_readiness": None, "error": None}

    def start(self) -> None:
        threading.Thread(target=self._loop, name="pitv-maintenance", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        time.sleep(20)  # let playback start first
        while not self._stop.is_set():
            try:
                self._once()
            except Exception as exc:  # noqa: BLE001
                log.exception("maintenance failed")
                self.status["error"] = repr(exc)
            self._stop.wait(600)

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
                from ..library.scanner import scan_all
                log.info("nightly scan")
                scan_all(conn, ffprobe_binary=self.ffprobe)
                self.status["last_scan"] = now_ts()
                if needs_rebuild(conn, now):
                    build_horizon(conn, now=now)
                    self.on_schedule_changed()
            from ..content import apply_report_files, manifest
            if apply_report_files(conn, settings.get("cache_dir") or ""):
                self.on_schedule_changed()
            if settings.get("acquire_fill_gaps"):
                from ..wanted import queue_gaps
                queue_gaps(conn)
            if self.cache is not None and self.cache.enabled and not self.cache.content_tool_running():
                # LRU eviction under the cap; everything in tomorrow's manifest stays.
                self.cache.protect({Path(i["target"]).name for i in manifest(conn, days=1, now=now)["items"]})
                self.cache.evict()
            # Readiness: is tomorrow (and the rest of today) actually playable? Runs at the
            # configured hours (default 06:00 and 07:30) once each, plus the first pass after boot.
            hours = [int(h) for h in (settings.get("readiness_hours") or [6, 7])]
            stamp = f"{today}:{local.hour}"
            if (local.hour in hours and stamp not in self._readiness_done) or not self._readiness_done:
                from ..readiness import check
                self._readiness_done.add(stamp)
                result = check(conn, now=now, days=1)
                self.status["last_readiness"] = {"at": now_ts(), "status": result["status"], "summary": result["summary"]}
                if result["substituted"]:
                    self.on_schedule_changed()
            keep = int(settings.get("history_keep_days", 180)) * 86400
            with tx(conn):
                conn.execute("DELETE FROM history WHERE started_at < ?", (now - keep,))
                conn.execute("DELETE FROM schedule WHERE end_ts < ?", (now - 14 * 86400,))
        finally:
            conn.close()
