"""Is everything scheduled actually playable? Run at `readiness_hours` (06:00 and 07:00) and on
demand from the admin.

A programme is ready when pitv_content has put it in the cache. With `nas_fallback` on, an
uncached programme whose NAS original is there still counts, logged as a warning because
pitv_content missed it. Anything else is an error: the slot and the rest of that channel-day
are rebuilt using only programmes that are playable now, so air time never meets a gap.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .content import manifest_window
from .db import all_settings, now_ts, rows_to_dicts, run_log_finish, run_log_start
from .player.cache import MediaCache
from .scheduler.build import rebuild_from
from .scheduler.rules import tz_of

log = logging.getLogger("pitv.readiness")


def playable_media(conn: sqlite3.Connection, cache: MediaCache, nas_fallback: bool) -> set[int]:
    """Every catalogue file that could play right now: a cache copy exists, or NAS fallback is on
    and the file's share is mounted (checked once per share, not per file, so this stays cheap
    over CIFS)."""
    mounted: dict[int, bool] = {}
    if nas_fallback:
        mounted = {r["id"]: Path(r["path"]).is_dir() for r in conn.execute("SELECT id, path FROM sources WHERE location = 'nas'")}
    ids: set[int] = set()
    for m in rows_to_dicts(conn.execute("SELECT id, path, cache_path, origin, source_id FROM media WHERE missing = 0 AND excluded = 0")):
        if cache.cache_copy(m) is not None or (nas_fallback and m["origin"] == "nas" and mounted.get(m["source_id"])):
            ids.add(m["id"])
    return ids


def check(conn: sqlite3.Connection, *, now: int | None = None, days: int = 1, substitute: bool = True) -> dict[str, Any]:
    """Check every slot from `now` to the end of the broadcast day `days` after the current one
    (1: tomorrow), as the module describes. With `substitute`, each channel with a failure is
    rebuilt from its first one."""
    settings = all_settings(conn)
    tz = tz_of(conn)
    now = now or now_ts()
    cache = MediaCache.from_settings(settings)
    nas_fallback = bool(settings.get("nas_fallback", True))
    horizon = manifest_window(settings, tz, now, days)
    rows = rows_to_dicts(conn.execute(
        "SELECT s.id AS slot_id, s.channel_id, s.start_ts, s.title, s.wanted_id, c.name AS channel_name,"
        " m.id, m.path, m.cache_path, m.origin"
        " FROM schedule s LEFT JOIN media m ON m.id = s.media_id JOIN channels c ON c.id = s.channel_id"
        " WHERE s.kind != 'filler' AND s.replay = 0 AND s.start_ts >= ? AND s.start_ts < ? ORDER BY s.start_ts",
        (now, horizon)))
    run_id = run_log_start(conn, "readiness")
    missing: dict[int, list[dict[str, Any]]] = {}   # channel_id -> slots
    notes: list[str] = []
    nas_only = 0
    seen: dict[int, str] = {}    # where each file plays from; a file can air on several channels, so locate it once
    for r in rows:
        if r["id"] is None:
            if r["wanted_id"] is not None:
                when = _slot_label(r, tz)
                missing.setdefault(r["channel_id"], []).append(r)
                notes.append(f"NOT FETCHED {when} (wanted #{r['wanted_id']})")
                log.error("not fetched in time: %s (wanted %s)", when, r["wanted_id"])
            continue
        if r["id"] not in seen:
            seen[r["id"]] = cache.locate(r, nas_fallback)[1]
        where = seen[r["id"]]
        if where == "cache":
            continue
        when = _slot_label(r, tz)
        if where == "nas":
            nas_only += 1
            notes.append(f"NOT CACHED {when}: will play from the NAS")   # detail in the run notes, one log line below
            continue
        missing.setdefault(r["channel_id"], []).append(r)
        notes.append(f"NOT PLAYABLE {when}: {where}")
        log.error("not playable: %s: %s", when, where)
    substituted = 0
    if substitute and missing:
        playable = playable_media(conn, cache, nas_fallback)
        for channel_id, slots in missing.items():
            first = min(s["start_ts"] for s in slots)
            exclude = {s["id"] for s in slots if s["id"] is not None}
            result = rebuild_from(conn, channel_id, first, now=now, exclude_media_ids=exclude,
                                  allow_external=False, only_media_ids=playable)
            substituted += len(slots)
            log.warning("rebuilt %s from %s replacing %d programme(s): %s", slots[0]["channel_name"],
                        _hhmm(first, tz), len(slots), result.get("summary"))
            notes.append(f"REBUILT {slots[0]['channel_name']} from {_hhmm(first, tz)}: {result.get('summary')}")
    outstanding = conn.execute("SELECT COUNT(*) FROM wanted WHERE status = 'queued'").fetchone()[0]
    if outstanding:
        notes.append(f"{outstanding} wanted item(s) still outstanding for pitv_content")
    if nas_only:
        log.warning("%d scheduled items are not in the cache and will play from the NAS; pitv_content has not"
                    " delivered them (see the readiness run notes)", nas_only)
    n_missing = sum(len(v) for v in missing.values())
    status = "error" if n_missing else ("warning" if nas_only else "ok")
    summary = f"{len(rows)} items checked, {n_missing} not playable, {substituted} substituted, {nas_only} relying on NAS fallback"
    run_log_finish(conn, run_id, status, summary, notes)
    log.info("readiness: %s", summary)
    return {"status": status, "summary": summary, "notes": notes, "missing": n_missing,
            "substituted": substituted, "checked": len(rows), "nas_fallback": nas_only}


def _hhmm(ts: int, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%a %H:%M")


def _slot_label(slot: dict[str, Any], tz: ZoneInfo) -> str:
    return f"{slot['channel_name']} {_hhmm(slot['start_ts'], tz)} '{slot['title']}'"
