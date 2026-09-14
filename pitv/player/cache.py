"""Local prefetch cache on the Pi's attached drive.

Upcoming programmes are copied from the NAS ahead of air time so playback never depends on
the network at the moment it matters. The cache is a plain directory; files are named
``<media_id>_<original name>`` so the index is just a directory listing.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("pitv.cache")


class MediaCache:
    def __init__(self, cache_dir: Path | None, max_bytes: int, copy_mbps: float = 0.0) -> None:
        self.dir = cache_dir
        self.max_bytes = max_bytes
        self.copy_mbps = copy_mbps
        self.enabled = bool(cache_dir)
        self._lock = threading.Lock()
        self.current_copy: dict[str, Any] | None = None
        if self.dir:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("cache dir unusable (%s): %s", self.dir, exc)
                self.enabled = False

    def cached_path(self, media_id: int, source: str) -> Path | None:
        if not self.enabled or not self.dir:
            return None
        p = self.dir / f"{media_id}_{Path(source).name}"
        return p if p.is_file() else None

    def resolve(self, media: dict[str, Any] | None) -> str | None:
        """Best available path: cached copy > transcoded copy > original."""
        if not media:
            return None
        original = media.get("path")
        for cand in (self.cached_path(media["id"], original) if original else None,
                     media.get("transcoded_path"), original):
            if cand and Path(cand).is_file():
                return str(cand)
        return None

    def usage(self) -> dict[str, Any]:
        if not self.enabled or not self.dir:
            return {"enabled": False}
        files = [p for p in self.dir.iterdir() if p.is_file() and not p.name.endswith(".part")]
        used = sum(p.stat().st_size for p in files)
        try:
            du = shutil.disk_usage(self.dir)
            free = du.free
        except OSError:
            free = None
        return {"enabled": True, "dir": str(self.dir), "files": len(files), "used": used,
                "max": self.max_bytes, "free": free, "copying": self.current_copy}

    def fetch(self, media_id: int, source: str, should_abort: Callable[[], bool] | None = None) -> Path | None:
        if not self.enabled or not self.dir:
            return None
        src = Path(source)
        dest = self.dir / f"{media_id}_{src.name}"
        if dest.is_file():
            try:
                if dest.stat().st_size == src.stat().st_size:
                    return dest
            except OSError:
                return dest
        try:
            size = src.stat().st_size
        except OSError as exc:
            log.warning("cannot stat %s: %s", src, exc)
            return None
        if size > self.max_bytes:
            return None
        self._make_room(size)
        tmp = dest.with_suffix(dest.suffix + ".part")
        self.current_copy = {"media_id": media_id, "name": src.name, "size": size, "done": 0}
        log.info("caching %s (%.0f MB)", src.name, size / 1e6)
        chunk = 8 * 1024 * 1024
        limit = self.copy_mbps * 1024 * 1024 / 8 if self.copy_mbps > 0 else 0
        started = time.time()
        try:
            with open(src, "rb") as fin, open(tmp, "wb") as fout:
                done = 0
                while True:
                    if should_abort and should_abort():
                        raise InterruptedError
                    buf = fin.read(chunk)
                    if not buf:
                        break
                    fout.write(buf)
                    done += len(buf)
                    self.current_copy["done"] = done
                    if limit:
                        expected = done / limit
                        elapsed = time.time() - started
                        if expected > elapsed:
                            time.sleep(expected - elapsed)
            os.replace(tmp, dest)
            log.info("cached %s (%.0f MB in %.0fs)", src.name, size / 1e6, time.time() - started)
            return dest
        except (OSError, InterruptedError) as exc:
            log.warning("cache copy failed for %s: %s", src.name, exc)
            try:
                tmp.unlink()
            except OSError:
                pass
            return None
        finally:
            self.current_copy = None

    def _make_room(self, needed: int) -> None:
        if not self.dir:
            return
        files = [p for p in self.dir.iterdir() if p.is_file()]
        used = sum(p.stat().st_size for p in files)
        try:
            free = shutil.disk_usage(self.dir).free
        except OSError:
            free = needed
        # Evict least recently used until under the cap and the disk has room.
        files.sort(key=lambda p: p.stat().st_atime)
        for p in files:
            if used + needed <= self.max_bytes and free > needed + 512 * 1024 * 1024:
                break
            if self._protected(p):
                continue
            sz = p.stat().st_size
            try:
                p.unlink()
                used -= sz
                free += sz
                log.info("evicted %s from cache", p.name)
            except OSError:
                pass

    _protected_names: set[str] = set()

    def protect(self, names: set[str]) -> None:
        self._protected_names = names

    def _protected(self, p: Path) -> bool:
        return p.name in self._protected_names


class PrefetchWorker:
    """Copies programmes airing in the next few hours into the cache, current channel first."""

    def __init__(self, cache: MediaCache, db_path: Path, hours: float, current_channel: Callable[[], int | None],
                 clock: Callable[[], int], days: int = 1) -> None:
        self.cache = cache
        self.db_path = db_path
        self.hours = hours
        self.days = days
        self.current_channel = current_channel
        self.clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()

    def start(self) -> None:
        if not self.cache.enabled:
            return
        threading.Thread(target=self._loop, name="pitv-prefetch", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def poke(self) -> None:
        self._wake.set()

    def _horizon(self, conn: sqlite3.Connection, now: int) -> int:
        """At least `hours` ahead, and through the end of the next `days` broadcast days, so
        tomorrow's television is on the local drive before it starts at 08:00."""
        from datetime import timedelta
        from ..db import all_settings
        from ..scheduler.rules import broadcast_day_for, day_bounds, tz_of
        horizon = now + int(self.hours * 3600)
        if self.days > 0:
            settings = all_settings(conn)
            tz = tz_of(conn)
            day = broadcast_day_for(now, settings, tz) + timedelta(days=self.days)
            horizon = max(horizon, day_bounds(day, settings, tz)[2])
        return horizon

    def _upcoming(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        now = self.clock()
        horizon = self._horizon(conn, now)
        rows = conn.execute(
            "SELECT s.channel_id, s.start_ts, m.id, m.path, m.transcoded_path, m.size FROM schedule s"
            " JOIN media m ON m.id = s.media_id WHERE s.kind = 'programme' AND s.end_ts > ? AND s.start_ts < ?"
            " AND m.missing = 0 ORDER BY s.start_ts", (now, horizon)).fetchall()
        cur = self.current_channel()
        items = [dict(r) for r in rows]
        # Current channel first, then by start time; the same file only once.
        items.sort(key=lambda r: (0 if r["channel_id"] == cur else 1, r["start_ts"]))
        seen: set[int] = set()
        out = []
        for r in items:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            out.append(r)
        return out

    def _loop(self) -> None:
        from ..db import connect
        while not self._stop.is_set():
            try:
                conn = connect(self.db_path)
                try:
                    items = self._upcoming(conn)
                finally:
                    conn.close()
                self.cache.protect({f"{r['id']}_{Path(r['path']).name}" for r in items})
                for r in items:
                    if self._stop.is_set():
                        return
                    if r.get("transcoded_path") and Path(r["transcoded_path"]).is_file():
                        continue  # already local
                    if self.cache.cached_path(r["id"], r["path"]):
                        continue
                    if not Path(r["path"]).is_file():
                        continue
                    self.cache.fetch(r["id"], r["path"], should_abort=self._stop.is_set)
            except Exception:  # noqa: BLE001
                log.exception("prefetch loop error")
            self._wake.wait(120)
            self._wake.clear()
