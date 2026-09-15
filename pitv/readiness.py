"""Is everything scheduled actually playable? Run ahead of time (and live by the player).

For every programme in the window, the file must be readable from the cache, a transcoded
copy, or the NAS original. Files that are missing while their share is mounted are treated as
unavailable: the slot is replaced and the rest of that channel-day rebalanced, and an ERROR is
logged. If a whole share is down, nothing is substituted (the outage, not the schedule, is the
problem) and the player falls back to the test card at air time.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from .db import all_settings, now_ts, run_log_finish, run_log_start
from .player.cache import MediaCache
from .scheduler.build import rebuild_from
from .scheduler.rules import broadcast_day_for, day_bounds, tz_of

log = logging.getLogger("pitv.readiness")


def resolvable(cache: MediaCache, media: dict[str, Any]) -> str | None:
    return cache.resolve({"id": media["id"], "path": media["path"], "transcoded_path": media.get("transcoded_path")})


def check(conn: sqlite3.Connection, *, now: int | None = None, days: int = 1, substitute: bool = True) -> dict[str, Any]:
    settings = all_settings(conn)
    tz = tz_of(conn)
    now = now or now_ts()
    cache_dir = settings.get("cache_dir") or ""
    cache = MediaCache(Path(cache_dir) if cache_dir else None, int(float(settings.get("cache_max_gb", 0)) * 1024 ** 3))
    day = broadcast_day_for(now, settings, tz)
    _, _, horizon = day_bounds(day + timedelta(days=max(1, days)), settings, tz)
    sources = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM sources")}
    share_up = {sid: Path(src["path"]).is_dir() for sid, src in sources.items()}
    rows = conn.execute(
        "SELECT s.id AS slot_id, s.channel_id, s.start_ts, s.title, c.number AS channel, c.name AS channel_name, m.*"
        " FROM schedule s JOIN media m ON m.id = s.media_id JOIN channels c ON c.id = s.channel_id"
        " WHERE s.kind = 'programme' AND s.replay = 0 AND s.start_ts >= ? AND s.start_ts < ? ORDER BY s.start_ts",
        (now, horizon)).fetchall()
    run_id = run_log_start(conn, "readiness")
    checked = 0
    missing: dict[int, list[dict[str, Any]]] = {}   # channel_id -> slots
    down_shares: set[int] = set()
    notes: list[str] = []
    seen: dict[int, bool] = {}
    for r in rows:
        checked += 1
        ok = seen.get(r["id"])
        if ok is None:
            ok = resolvable(cache, dict(r)) is not None
            seen[r["id"]] = ok
        if ok:
            continue
        if not share_up.get(r["source_id"], True):
            down_shares.add(r["source_id"])
            continue
        missing.setdefault(r["channel_id"], []).append(dict(r))
        notes.append(f"MISSING {r['channel_name']} {_hhmm(r['start_ts'], tz)} '{r['title']}' ({r['path']})")
        log.error("missing programme: %s at %s on %s: %s", r["title"], _hhmm(r["start_ts"], tz), r["channel_name"], r["path"])
    for sid in down_shares:
        src = sources[sid]
        notes.append(f"SHARE DOWN {src['name']} ({src['path']}): not substituting, will retry")
        log.error("share not mounted: %s (%s); programmes from it cannot be verified", src["name"], src["path"])
    substituted = 0
    if substitute:
        for channel_id, slots in missing.items():
            exclude = {s["id"] for s in slots}
            first = min(s["start_ts"] for s in slots)
            result = rebuild_from(conn, channel_id, first, now=now, exclude_media_ids=exclude)
            substituted += len(slots)
            log.warning("rebalanced %s from %s replacing %d programme(s): %s", slots[0]["channel_name"],
                        _hhmm(first, tz), len(slots), result.get("summary"))
            notes.append(f"REBALANCED {slots[0]['channel_name']} from {_hhmm(first, tz)}: {result.get('summary')}")
    outstanding = conn.execute("SELECT COUNT(*) FROM wanted WHERE status IN ('queued', 'searching', 'downloading', 'transcoding')").fetchone()[0]
    if outstanding:
        notes.append(f"{outstanding} wanted item(s) still outstanding for pitv_content")
    status = "ok" if not missing and not down_shares else ("error" if missing else "warning")
    summary = f"{checked} programmes checked, {sum(len(v) for v in missing.values())} missing, {substituted} substituted, {len(down_shares)} share(s) down"
    run_log_finish(conn, run_id, status, summary, notes)
    log.info("readiness: %s", summary)
    return {"status": status, "summary": summary, "notes": notes, "missing": sum(len(v) for v in missing.values()),
            "substituted": substituted, "checked": checked}


def _hhmm(ts: int, tz) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts, tz).strftime("%a %H:%M")
