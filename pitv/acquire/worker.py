"""Background worker (runs inside the player daemon) that fulfils the wanted list and the
transcode queue, one item at a time, inside the configured hours."""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..db import all_settings, connect, now_ts, row_to_dict, tx
from ..library.probe import ffprobe
from ..library.scanner import scan_source
from ..scheduler.rules import hhmm_to_minutes
from . import providers
from .transcode import transcode

log = logging.getLogger("pitv.acquire")
_SAFE = re.compile(r"[^\w\s\-\.,'()!&]")


def safe_name(text: str) -> str:
    return _SAFE.sub("", text).strip() or "untitled"


def in_hours(window: str, now: datetime) -> bool:
    try:
        a, b = window.split("-")
        start, end = hhmm_to_minutes(a), hhmm_to_minutes(b)
    except ValueError:
        return True
    cur = now.hour * 60 + now.minute
    return start <= cur <= end if start <= end else (cur >= start or cur <= end)


class AcquisitionWorker:
    def __init__(self, db_path: Path, clock: Callable[[], int], on_pi: bool, ffprobe_binary: str,
                 on_library_changed: Callable[[], None]) -> None:
        self.db_path = db_path
        self.clock = clock
        self.on_pi = on_pi
        self.ffprobe_binary = ffprobe_binary
        self.on_library_changed = on_library_changed
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.current: dict[str, Any] | None = None

    def start(self) -> None:
        threading.Thread(target=self._loop, name="pitv-acquire", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def poke(self) -> None:
        self._wake.set()

    def status(self) -> dict[str, Any]:
        return {"current": self.current}

    # --- loop -----------------------------------------------------------------------------

    def _loop(self) -> None:
        time.sleep(45)
        while not self._stop.is_set():
            try:
                self._once()
            except Exception:  # noqa: BLE001
                log.exception("acquisition loop error")
            self._wake.wait(60)
            self._wake.clear()

    def _once(self) -> None:
        conn = connect(self.db_path)
        try:
            settings = all_settings(conn)
            now = datetime.fromtimestamp(self.clock())
            if settings.get("acquire_enabled") and in_hours(settings.get("acquire_hours", "00:00-23:59"), now):
                if settings.get("acquire_fill_gaps"):
                    self.queue_gaps(conn)
                row = conn.execute("SELECT * FROM wanted WHERE status IN ('queued') AND attempts < 3 ORDER BY id LIMIT 1").fetchone()
                if row:
                    self._fulfil(conn, row_to_dict(row), settings)
                    return
            if settings.get("transcode_enabled") and in_hours(settings.get("transcode_hours", "01:00-07:00"), now):
                row = conn.execute("SELECT * FROM transcode_queue WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
                if row:
                    self._transcode(conn, dict(row), settings)
        finally:
            conn.close()

    # --- gaps ---------------------------------------------------------------------------------

    def queue_gaps(self, conn: sqlite3.Connection) -> int:
        """Queue episodes missing between the first and last episode of each season on disk."""
        added = 0
        shows = conn.execute("SELECT id, title, year FROM shows WHERE missing = 0 AND excluded = 0").fetchall()
        for show in shows:
            eps = conn.execute("SELECT season, episode FROM media WHERE show_id = ? AND missing = 0 AND season IS NOT NULL"
                               " AND episode IS NOT NULL ORDER BY season, episode", (show["id"],)).fetchall()
            by_season: dict[int, set[int]] = {}
            for e in eps:
                by_season.setdefault(e["season"], set()).add(e["episode"])
            for season, have in by_season.items():
                if season == 0:
                    continue
                for ep in range(min(have), max(have)):
                    if ep in have:
                        continue
                    exists = conn.execute("SELECT 1 FROM wanted WHERE show_id = ? AND season = ? AND episode = ?",
                                          (show["id"], season, ep)).fetchone()
                    if exists:
                        continue
                    with tx(conn):
                        conn.execute("INSERT INTO wanted(kind, title, year, season, episode, show_id, provider, auto, created_at)"
                                     " VALUES ('episode', ?, ?, ?, ?, ?, 'auto', 1, ?)",
                                     (show["title"], show["year"], season, ep, show["id"], now_ts()))
                    added += 1
        return added

    # --- fulfilment -------------------------------------------------------------------------------

    def _update(self, conn: sqlite3.Connection, wid: int, **fields: Any) -> None:
        fields["updated_at"] = now_ts()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with tx(conn):
            conn.execute(f"UPDATE wanted SET {sets} WHERE id = ?", (*fields.values(), wid))
        cur = self.current or {}
        self.current = {"kind": "wanted", "id": wid, "title": cur.get("title") or self._title_of(conn, wid),
                        **{k: v for k, v in fields.items() if k in ("status", "progress", "message")}}

    def _title_of(self, conn: sqlite3.Connection, wid: int) -> str:
        row = conn.execute("SELECT title, season, episode FROM wanted WHERE id = ?", (wid,)).fetchone()
        if not row:
            return ""
        se = f" S{row['season']:02d}E{row['episode']:02d}" if row["season"] is not None and row["episode"] is not None else ""
        return f"{row['title']}{se}"

    def _dest_for(self, w: dict[str, Any], settings: dict[str, Any], ext: str = ".mp4") -> tuple[Path, str]:
        base = Path(settings.get("acquire_dir") or "") if settings.get("acquire_dir") else Path(settings.get("cache_dir") or "/var/lib/pitv") / "acquired"
        title = safe_name(w["title"])
        year = f" ({w['year']})" if w.get("year") else ""
        if w["kind"] == "episode":
            s, e = int(w.get("season") or 1), int(w.get("episode") or 1)
            return base / "tvshows" / f"{title}{year}" / f"Season {s:02d}" / f"{title} - S{s:02d}E{e:02d}{ext}", "tv"
        if w["kind"] == "movie":
            return base / "movies" / f"{title}{year}" / f"{title}{year}{ext}", "movie"
        return base / "ads" / str(w.get("year") or "unknown") / f"{title}{ext}", "advert"

    def _fulfil(self, conn: sqlite3.Connection, w: dict[str, Any], settings: dict[str, Any]) -> None:
        wid = w["id"]
        self._update(conn, wid, status="searching", attempts=w["attempts"] + 1, message="")
        try:
            url, size = self._resolve(w, settings)
            if not url:
                self._update(conn, wid, status="failed", message="No suitable file found")
                return
            dest, stype = self._dest_for(w, settings)
            self._update(conn, wid, status="downloading", message=url[:120])
            prog = lambda msg, frac: self._update(conn, wid, progress=frac, message=msg)  # noqa: E731
            if providers.is_direct_media(url):
                dest = dest.with_suffix(Path(url.split("?")[0]).suffix or ".mp4")
                got = providers.download_url(url, dest, prog, size)
            else:
                got = providers.download_with_ytdlp(url, dest.with_suffix(""), prog)
            info = ffprobe(got, self.ffprobe_binary)
            if info and info.vcodec not in ("h264", "hevc"):
                self._update(conn, wid, status="transcoding", progress=0, message=f"re-encoding from {info.vcodec}")
                out = got.with_name(got.stem + ".h264.mp4")
                transcode(got, out, info.duration, self.on_pi, int(settings.get("transcode_max_height", 720)),
                          int(settings.get("transcode_bitrate_kbps", 4000)), prog, self._stop.is_set)
                got.unlink(missing_ok=True)
                got = out.rename(got.with_suffix(".mp4"))
            media_id = self._register(conn, got, stype, w)
            self._update(conn, wid, status="done", progress=1.0, dest_path=str(got), media_id=media_id, message="ready")
            self.on_library_changed()
        except Exception as exc:  # noqa: BLE001
            log.exception("acquisition failed for wanted %s", wid)
            self._update(conn, wid, status="failed" if w["attempts"] + 1 >= 3 else "queued", message=f"{exc}"[:300])
        finally:
            self.current = None

    def _resolve(self, w: dict[str, Any], settings: dict[str, Any]) -> tuple[str | None, int | None]:
        provider = w.get("provider") or "auto"
        ref = w.get("ref") or ""
        if provider == "url":
            return ref, None
        if provider == "archive" and ref:
            ident, _, fname = ref.partition("/")
            if fname:
                import urllib.parse
                return f"https://archive.org/download/{ident}/{urllib.parse.quote(fname)}", None
            f = providers.archive_pick_file(ident, w.get("season"), w.get("episode"))
            return (f["url"], f["size"]) if f else (None, None)
        if "archive" in (settings.get("acquire_providers") or []):
            query = f'title:("{w["title"]}")'
            for cand in providers.archive_search(query, w.get("year"), rows=8):
                f = providers.archive_pick_file(cand.ref, w.get("season"), w.get("episode"))
                if f:
                    return f["url"], f["size"]
        return None, None

    def _register(self, conn: sqlite3.Connection, path: Path, stype: str, w: dict[str, Any]) -> int | None:
        """Make sure the acquired folder is a source, scan it, and return the new media id."""
        root = path.parents[2] if stype in ("tv",) else path.parents[1]
        if stype == "advert":
            root = path.parents[1]
        src = conn.execute("SELECT * FROM sources WHERE path = ?", (str(root),)).fetchone()
        if not src:
            with tx(conn):
                conn.execute("INSERT INTO sources(type, name, path) VALUES (?,?,?)",
                             (stype, f"Acquired {stype}", str(root)))
            src = conn.execute("SELECT * FROM sources WHERE path = ?", (str(root),)).fetchone()
        scan_source(conn, row_to_dict(src), ffprobe_binary=self.ffprobe_binary)
        row = conn.execute("SELECT id FROM media WHERE path = ?", (str(path),)).fetchone()
        return row["id"] if row else None

    # --- transcode queue ----------------------------------------------------------------------------

    def _transcode(self, conn: sqlite3.Connection, item: dict[str, Any], settings: dict[str, Any]) -> None:
        media = conn.execute("SELECT * FROM media WHERE id = ?", (item["media_id"],)).fetchone()
        if not media:
            with tx(conn):
                conn.execute("DELETE FROM transcode_queue WHERE id = ?", (item["id"],))
            return

        def upd(status: str, progress: float, message: str) -> None:
            with tx(conn):
                conn.execute("UPDATE transcode_queue SET status = ?, progress = ?, message = ?, updated_at = ? WHERE id = ?",
                             (status, progress, message, now_ts(), item["id"]))
            self.current = {"kind": "transcode", "id": item["id"], "media_id": media["id"], "title": media["title"],
                            "status": status, "progress": progress, "message": message}

        base = Path(settings.get("acquire_dir") or "") if settings.get("acquire_dir") else Path(settings.get("cache_dir") or "/var/lib/pitv") / "acquired"
        dest = base / "transcoded" / f"{media['id']}_{Path(media['path']).stem}.mp4"
        upd("running", 0.0, "starting")
        try:
            transcode(Path(media["path"]), dest, media["duration"], self.on_pi, int(settings.get("transcode_max_height", 720)),
                      int(settings.get("transcode_bitrate_kbps", 4000)), lambda m, f: upd("running", f, m), self._stop.is_set)
            with tx(conn):
                conn.execute("UPDATE media SET transcoded_path = ?, hwdec = 1, attention = NULL WHERE id = ?", (str(dest), media["id"]))
            upd("done", 1.0, "ready")
        except Exception as exc:  # noqa: BLE001
            log.exception("transcode failed")
            upd("failed", 0.0, f"{exc}"[:300])
        finally:
            self.current = None
