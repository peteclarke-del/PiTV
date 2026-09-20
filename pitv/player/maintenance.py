"""Housekeeping thread inside the player: import pitv_content's library index, keep the
schedule horizon topped up, apply delivery reports dropped as files, evict from the cache, run
the readiness checks and trim old history. Keeping this in the always-running player means no
cron or systemd timers are needed for PiTV on the Pi.

The thread opens its own database connection for each pass and never touches the player's:
SQLite connections stay on the thread that made them. Hours such as `catalogue_hour` are
wall-clock hours in the configured timezone, the one the schedule is built in."""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import catalogue, display, tool_client
from ..content import apply_report_files, protect_manifest, push_screen
from ..db import all_settings, connect, now_ts, tx
from ..lineup import evict_fetched, remove_aired_transients
from ..readiness import check as readiness_check
from ..scheduler.horizon import build_horizon, needs_rebuild, refill_empty_days
from ..scheduler.rules import tz_of
from ..wanted import band_needs, queue_gaps, request_all_band_material, withdraw_gaps
from .cache import MediaCache

log = logging.getLogger("pitv.maintenance")

STARTUP_DELAY = 20        # seconds; let playback start before the first pass
PASS_INTERVAL = 600       # seconds between passes
EMPTY_BUILD_RETRY = 3600  # a build that produced nothing (empty library) is not retried every pass
GAP_BUILD_RETRY = 3600    # retry holding-card gaps as remote entries become eligible
SCHEDULE_KEEP_DAYS = 14   # aired slots kept for the history and "what was on" views
RUN_LOG_KEEP_DAYS = 30


class Maintenance:
    def __init__(self, db_path: Path, clock: Callable[[], int],
                 on_schedule_changed: Callable[[], None], cache: MediaCache) -> None:
        self.db_path = db_path
        self.cache = cache
        self.clock = clock
        self.on_schedule_changed = on_schedule_changed
        self._stop = threading.Event()
        self._readiness_done: set[str] = set()   # "YYYY-MM-DD:hour" stamps already checked today
        self._first_pass = True
        self._empty_build_at = 0
        self._gap_build_at = 0
        self._wanted_requested_at = 0
        self._screen_pushed: dict[str, Any] | None = None   # the profile pitv_content last took
        self._index_mtime = 0.0                 # the index file version last imported
        self._pruned_on: str | None = None      # local date of the last history/schedule trim
        self.status: dict[str, Any] = {"last_build": None, "last_import": None, "last_readiness": None,
                                      "last_wanted_run": None, "last_band_fetch": None, "error": None}

    def start(self) -> None:
        threading.Thread(target=self._loop, name="pitv-maintenance", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        if self._stop.wait(STARTUP_DELAY):
            return
        while not self._stop.is_set():
            try:
                self._once()
                self.status["error"] = None
            except Exception as exc:  # one bad pass must not end housekeeping
                log.exception("maintenance failed")
                self.status["error"] = repr(exc)
            self._stop.wait(PASS_INTERVAL)

    def _build(self, conn: sqlite3.Connection, now: int) -> None:
        if now - self._empty_build_at < EMPTY_BUILD_RETRY:
            return
        log.info("schedule horizon short; building")
        result = build_horizon(conn, now=now)
        self.status["last_build"] = {"at": now_ts(), "status": result["status"], "summary": result["summary"]}
        log.info("schedule: %s", result["summary"])
        if result["built"]:
            self.on_schedule_changed()
        else:
            self._empty_build_at = now   # nothing to schedule yet; do not thrash the run log

    def _last_import(self, conn: sqlite3.Connection, ok_only: bool) -> int | None:
        where = "status IN ('ok', 'warning')" if ok_only else "status != 'running'"
        row = conn.execute(f"SELECT MAX(started_at) AS t FROM run_log WHERE kind = 'catalogue' AND {where}").fetchone()
        return row["t"] if row else None

    def _keep_content_screen(self, settings: dict[str, Any]) -> None:
        """The screen goes to pitv_content after every start and whenever its values change,
        whether the admin chose another or a release changed the table. Refused or unreachable,
        it is tried again on the next pass."""
        screen = display.content_profile(settings)
        if screen == self._screen_pushed:
            return
        if failed := push_screen(settings):
            log.debug("screen not pushed to pitv_content: %s", failed)
        else:
            self._screen_pushed = screen

    def _once(self) -> None:
        conn = connect(self.db_path)
        try:
            self._pass(conn)
        finally:
            conn.close()

    def _pass(self, conn: sqlite3.Connection) -> None:
        now = self.clock()
        settings = all_settings(conn)
        tz = tz_of(conn)
        if needs_rebuild(conn, now):
            self._build(conn, now)
        # A complete horizon may still contain a holding-card slot. Remote entries become
        # eligible as their lead window passes even when no catalogue file changes, so retry from
        # the first gap periodically. The refill preserves everything billed before that point.
        if now - self._gap_build_at >= GAP_BUILD_RETRY:
            refill = refill_empty_days(conn, now=now)
            self._gap_build_at = now
            if refill["days"]:
                self.status["last_build"] = {"at": now_ts(), "status": refill["status"],
                                             "summary": refill["summary"]}
                self.on_schedule_changed()
        local = datetime.fromtimestamp(now, tz)
        today = local.date().isoformat()

        # Catalogue: import pitv_content's index daily at catalogue_hour, and whenever it
        # rewrites the index file (after its own re-index), so new material is schedulable.
        # After a restart an index already imported is not imported again: its mtime is
        # compared with the last successful import rather than with nothing.
        if self._first_pass:
            self._index_mtime = float(self._last_import(conn, ok_only=True) or 0)
        last = self._last_import(conn, ok_only=False)
        last_day = datetime.fromtimestamp(last, tz).date().isoformat() if last else None
        changed = catalogue.index_changed_since(settings, self._index_mtime)
        if changed or (local.hour == int(settings["catalogue_hour"]) and last_day != today):
            result = catalogue.refresh(conn)
            if result.get("status") != "error":
                enriched = catalogue.enrich_missing_metadata(conn, limit=25)
                if enriched["found"]:
                    log.info("catalogue metadata: %s", enriched["summary"])
            if changed:
                self._index_mtime = changed
            self.status["last_import"] = {"at": now_ts(), "status": result["status"], "summary": result["summary"]}
            self._empty_build_at = 0
            if needs_rebuild(conn, now):
                self._build(conn, now)
            if result.get("refilled_days"):   # the import rebuilt days that had held only filler
                self.on_schedule_changed()

        remove_aired_transients(conn)
        if apply_report_files(conn, self.cache):
            self.cache.invalidate()
            self.on_schedule_changed()
        if settings["acquire_fill_gaps"]:
            queue_gaps(conn)
        elif withdrawn := withdraw_gaps(conn):
            log.info("missing-episode requests are off: withdrew %d unanswered request(s)", withdrawn)
        self._keep_content_screen(settings)
        # A band with no local pool depends on collection before its individual scheduled files
        # can even enter the cache manifest. Declare those top-ups first; pitv_content gives these
        # urgent catalogue jobs queue priority, while still running only one downloader at a time.
        starving = settings.get("band_fetch") and any(n["have"] == 0 for n in band_needs(conn, settings))
        if settings.get("band_fetch") and (local.hour in (settings.get("band_fetch_hours") or []) or starving):
            result = request_all_band_material(conn, settings)
            if result["asked"] or self.status["last_band_fetch"] is None:
                self.status["last_band_fetch"] = {"at": now_ts(), **result}
        # Wanted items are schedule commitments. A busy coordinator retains the request; older
        # versions return 409 and are tried again on the next maintenance pass.
        queued = conn.execute("SELECT COUNT(*) FROM wanted WHERE status = 'queued'").fetchone()[0]
        if queued and now - self._wanted_requested_at >= PASS_INTERVAL:
            status, payload = tool_client.request(tool_client.base_url(settings), "POST", "run",
                                                  body={"mode": "cache"}, timeout=15)
            if status < 400 and isinstance(payload, dict) and payload.get("ok"):
                self._wanted_requested_at = now
                self.status["last_wanted_run"] = {"at": now_ts(), "status": "queued", "queued": queued,
                                                   "job_id": payload.get("job_id")}
                log.info("wanted: queued pitv_content cache run for %d item(s)", queued)
            elif status not in (409, 503):
                reason = payload.get("error") if isinstance(payload, dict) else f"HTTP {status}"
                self.status["last_wanted_run"] = {"at": now_ts(), "status": "error", "queued": queued,
                                                   "error": reason}
                log.warning("wanted: pitv_content did not accept a cache run: %s", reason)
        if self.cache.enabled and not self.cache.content_tool_running():
            protect_manifest(conn, self.cache, now=now)
            self.cache.make_room()
            evict_fetched(conn, now=now)

        # Readiness: is tomorrow (and the rest of today) actually playable? Once at each of
        # the configured hours (default 06:00 and 07:00), plus the first pass after boot.
        stamp = f"{today}:{local.hour}"
        self._readiness_done = {s for s in self._readiness_done if s.startswith(today)}
        if self._first_pass or (local.hour in settings["readiness_hours"] and stamp not in self._readiness_done):
            self._readiness_done.add(stamp)
            result = readiness_check(conn, now=now, days=1)
            self.status["last_readiness"] = {"at": now_ts(), "status": result["status"], "summary": result["summary"]}
            if result["substituted"]:
                self.on_schedule_changed()
        self._first_pass = False

        if self._pruned_on != today:   # daily: nothing here ages by the ten-minute pass
            keep = int(settings["history_keep_days"]) * 86400
            with tx(conn):
                conn.execute("DELETE FROM history WHERE started_at < ?", (now - keep,))
                conn.execute("DELETE FROM schedule WHERE end_ts < ?", (now - SCHEDULE_KEEP_DAYS * 86400,))
                conn.execute("DELETE FROM run_log WHERE started_at < ?", (now - RUN_LOG_KEEP_DAYS * 86400,))
            self._pruned_on = today
