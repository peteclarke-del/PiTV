"""The contract between PiTV (schedule + display) and pitv_content (fetch + transcode + cache).

PiTV publishes a manifest of everything the next broadcast day(s) need; pitv_content copies or
transcodes those files into the cache (and fetches wanted items from wherever it can), then
posts a report. See docs/PLAN.md §7.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from .db import all_settings, now_ts, row_to_dict, tx
from .player.cache import MediaCache
from .player.hwdec import PI_HW_CODECS, is_raspberry_pi
from .scheduler.rules import broadcast_day_for, day_bounds, tz_of

MANIFEST_SCHEMA = 1
MAX_WANTED_ATTEMPTS = 3
# Typical running times per kind, so pitv_content can reject obviously wrong search hits.
WANTED_MINUTES = {"music": [2, 8], "advert": [0.1, 2], "episode": [20, 60], "movie": [70, 180]}
# Kodi-style folder layouts pitv_content must file fetched items into (relative to acquire_dir).
ACQUIRED_LAYOUTS = {
    "episode": "<acquire_dir>/tvshows/<Show> (<premiere year>)/Season <YYYY>/<Show> - S<YYYY>E<nn> - <Title>.mp4 (+ .nfo, tvshow.nfo)",
    "sport": "<acquire_dir>/tvsports/<Sport> (<year>)/Season <YYYY>/<Sport> - S<YYYY>E<nn> - <Title>.mp4",
    "movie": "<acquire_dir>/movies/<Title> (<year>)/<Title> (<year>).mp4",
    "advert": "<acquire_dir>/ads/<year>/<Title>.mp4 (+ .nfo with <tag>alcohol|tobacco|adult|gambling</tag> where known)",
    "music": "<acquire_dir>/music videos/<Genre>/<Artist> - <Title> (<year>).mp4 ; concerts under Concerts/<Genre>/",
}
log = logging.getLogger("pitv.content")


def acquire_dir(settings: dict[str, Any]) -> str:
    """Where pitv_content files what it fetches: `acquire_dir`, else `<cache_dir>/acquired`."""
    cache = settings.get("cache_dir") or ""
    return settings.get("acquire_dir") or (str(Path(cache) / "acquired") if cache else "")


def manifest_window(settings: dict[str, Any], tz, now: int, days: int) -> int:
    """End of the manifest window: `days`=1 means through the end of the next broadcast day,
    so a run at 01:00 covers today's remainder and the whole of tomorrow (08:00 to 08:00)."""
    day = broadcast_day_for(now, settings, tz)
    return day_bounds(day + timedelta(days=max(1, days)), settings, tz)[2]


def manifest(conn: sqlite3.Connection, days: int = 1, now: int | None = None) -> dict[str, Any]:
    settings = all_settings(conn)
    tz = tz_of(conn)
    now = now or now_ts()
    cache = MediaCache.from_settings(settings)
    acquire = acquire_dir(settings)
    horizon = manifest_window(settings, tz, now, days)
    profile = settings.get("content_profile") or {}
    max_h = int(profile.get("height", 576))
    sources = {r["id"]: row_to_dict(r) for r in conn.execute("SELECT * FROM sources")}
    rows = conn.execute(
        "SELECT s.channel_id, s.start_ts, c.number AS channel, m.* FROM schedule s"
        " JOIN media m ON m.id = s.media_id JOIN channels c ON c.id = s.channel_id"
        " WHERE s.end_ts > ? AND s.start_ts < ? AND m.missing = 0 AND s.kind != 'filler'"
        " ORDER BY s.start_ts", (now, horizon)).fetchall()
    items: dict[int, dict[str, Any]] = {}
    for r in rows:
        it = items.get(r["id"])
        if it is None:
            src = sources.get(r["source_id"]) or {}
            stem, ext = Path(r["path"]).stem, Path(r["path"]).suffix
            hw = (r["vcodec"] or "") in PI_HW_CODECS
            # Anything the Pi cannot decode in hardware, or well above the CRT's 576 lines,
            # is worth re-encoding to the CRT profile; the rest is copied as-is.
            needs_transcode = (not hw) or ((r["height"] or 0) > max_h * 1.5)
            target = f"{r['id']}_{stem}.mp4" if needs_transcode else f"{r['id']}_{stem}{ext}"
            try:
                relpath = str(Path(r["path"]).relative_to(src.get("path") or "/"))
            except ValueError:
                relpath = None
            hours_ahead = max(0.0, (r["start_ts"] - now) / 3600)
            it = {
                "media_id": r["id"], "kind": r["kind"], "title": r["title"], "path": r["path"],
                "relpath": relpath, "share": src.get("path"), "remote": src.get("remote"), "source": src.get("name"),
                "source_type": src.get("type"), "size": r["size"], "duration": r["duration"],
                "priority": int(hours_ahead // 4),           # 0 = needed within 4 hours
                "deadline_ts": r["start_ts"] - 15 * 60,
                "vcodec": r["vcodec"], "acodec": r["acodec"], "width": r["width"], "height": r["height"],
                "interlaced": bool(r["interlaced"]), "hwdec_ok": hw, "first_air_ts": r["start_ts"],
                "channels": [], "target": str(cache.dir / target) if cache.dir else target,
                "action": "transcode" if needs_transcode else "copy",
                "already_cached": cache.cached_path(r["id"], r["path"]) is not None,
                "transcoded_path": r["transcoded_path"],
            }
            items[r["id"]] = it
        if r["channel"] not in it["channels"]:
            it["channels"].append(r["channel"])
    wanted = []
    for w in conn.execute("SELECT * FROM wanted WHERE status IN ('queued', 'failed') AND attempts < ? ORDER BY id",
                          (MAX_WANTED_ATTEMPTS,)):
        d = row_to_dict(w)
        show = conn.execute("SELECT title FROM shows WHERE id = ?", (d["show_id"],)).fetchone() if d.get("show_id") else None
        if show is None and d.get("lineup_id"):
            show = conn.execute("SELECT title FROM lineup WHERE id = ? AND kind = 'show'", (d["lineup_id"],)).fetchone()
        wanted.append({"wanted_id": d["id"], "kind": d["kind"], "title": d["title"], "artist": d.get("artist"),
                       "year": d.get("year"), "season": d.get("season"), "episode": d.get("episode"),
                       "genre": d.get("genre"), "provider": d.get("provider"), "ref": d.get("ref"),
                       "show_id": d.get("show_id"), "show_title": show["title"] if show else None,
                       "dest_dir": _wanted_dest(d, acquire), "attempts": d.get("attempts", 0),
                       "duration_minutes": WANTED_MINUTES.get(d["kind"], [1, 240]),
                       "hints": _search_hints(d, show["title"] if show else None),
                       # A music video must be the exact release; a film or episode may be listed under a nearby year.
                       "year_tolerance": 0 if d["kind"] == "music" else 2,
                       "transient": bool(d.get("transient")), "lineup_id": d.get("lineup_id")})
    free_bytes = None
    try:
        free_bytes = shutil.disk_usage(cache.dir).free if cache.dir and cache.dir.exists() else None
    except OSError:
        free_bytes = None
    return {"schema": MANIFEST_SCHEMA, "generated_ts": now, "horizon_ts": horizon, "days": days,
            "cache_dir": str(cache.dir) if cache.dir else "", "acquire_dir": acquire, "pi": is_raspberry_pi(),
            "free_bytes": free_bytes, "cache_max_bytes": cache.max_bytes,
            "running_marker": str(cache.running_marker) if cache.dir else None,
            "reports_dir": str(cache.reports_dir) if cache.dir else None,
            "note": "Downloads must be written under acquire_dir on the Pi's cache drive, never onto the NAS shares.",
            "profile": profile, "layouts": ACQUIRED_LAYOUTS,
            "items": sorted(items.values(), key=lambda i: i["first_air_ts"]),
            "wanted": wanted, "shortfalls": [{"channel_id": None, "note": n} for n in _recent_notes(conn)]}


def protect_manifest(conn: sqlite3.Connection, cache: MediaCache, now: int | None = None) -> None:
    """Keep eviction away from everything in the current manifest (docs/PLAN.md §7 shared-drive rules)."""
    cache.protect({Path(i["target"]).name for i in manifest(conn, days=1, now=now)["items"]})


def _search_hints(w: dict[str, Any], show_title: str | None) -> list[str]:
    """hints[0] is the exact search phrase pitv_content tries first; the rest are extra words."""
    yr = f" {w['year']}" if w.get("year") else ""
    if w["kind"] == "music":
        artist = f"{w['artist']} " if w.get("artist") else ""
        return [f"{artist}{w['title']} official video".strip(), "Top of the Pops"]
    if w["kind"] == "advert":
        return [f"{w['title']}{yr} UK advert", "ITV", "TV ad"]
    if w["kind"] == "episode":
        se = ""
        if w.get("season") is not None and w.get("episode") is not None:
            se = f" S{int(w['season']):02d}E{int(w['episode']):02d}"
        return [f"{show_title or w['title']}{se}{yr} full episode", "BBC", "ITV"]
    return [f"{w['title']}{yr} full film"]


def _wanted_dest(w: dict[str, Any], acquire: str) -> str:
    base = Path(acquire) if acquire else Path("acquired")
    title = w["title"]
    year = f" ({w['year']})" if w.get("year") else ""
    if w["kind"] == "episode":
        return str(base / "tvshows" / f"{title}{year}" / f"Season {int(w.get('season') or 1):02d}")
    if w["kind"] == "movie":
        return str(base / "movies" / f"{title}{year}")
    if w["kind"] == "music":
        return str(base / "music videos" / (w.get("genre") or "Unsorted").title())
    return str(base / "ads" / str(w.get("year") or "unknown"))


def _recent_notes(conn: sqlite3.Connection) -> list[str]:
    """Notes from the last schedule build that mean the library came up short (gaps pitv_content
    could fill): missing music and filler slots."""
    row = conn.execute("SELECT details FROM run_log WHERE kind = 'schedule' ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return []
    try:
        return [n for n in json.loads(row["details"]) if "no music" in n or "filler" in n]
    except ValueError:
        return []


def apply_report(conn: sqlite3.Connection, report: dict[str, Any]) -> dict[str, Any]:
    """Record what pitv_content produced; returns counts. Cached copies are found by the player
    by name, so only wanted rows and run logging need updating here."""
    done_items = failed_items = 0
    for it in report.get("items") or []:
        if not isinstance(it, dict):
            continue
        if it.get("status") == "done":
            done_items += 1
            p = it.get("path")
            try:
                media_id = int(it.get("media_id") or 0)
            except (TypeError, ValueError):
                media_id = 0
            if p and isinstance(p, str) and media_id and Path(p).is_file():
                # A transcode is also the best local copy for later days, after the cache has moved on.
                with tx(conn):
                    conn.execute("UPDATE media SET transcoded_path = ?, hwdec = 1 WHERE id = ? AND transcoded_path IS NULL"
                                 " AND ? LIKE '%.mp4'", (p, media_id, p))
        elif it.get("status") == "failed":
            failed_items += 1
    done_w = failed_w = 0
    with tx(conn):
        for w in report.get("wanted") or []:
            try:
                wid = int(w.get("wanted_id") or 0)
            except (TypeError, ValueError, AttributeError):
                wid = 0
            if not wid:
                continue
            if w.get("status") == "done":
                done_w += 1
                conn.execute("UPDATE wanted SET status = 'done', progress = 1, dest_path = ?, message = ?, updated_at = ? WHERE id = ?",
                             (w.get("path"), (w.get("message") or "fetched by pitv_content")[:300], now_ts(), wid))
            else:
                failed_w += 1
                msg = (w.get("message") or "pitv_content failed")[:300]
                if "bot check" in msg.lower() or "rate limit" in msg.lower():
                    # The provider, not the request, was the problem: retry without using up an attempt.
                    conn.execute("UPDATE wanted SET status = 'queued', message = ?, updated_at = ? WHERE id = ?", (msg, now_ts(), wid))
                else:
                    conn.execute("UPDATE wanted SET status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'queued' END,"
                                 " attempts = attempts + 1, message = ?, updated_at = ? WHERE id = ?",
                                 (MAX_WANTED_ATTEMPTS, msg, now_ts(), wid))
        run = report.get("run") if isinstance(report.get("run"), dict) else {}

        def ts(key: str) -> int:
            try:
                return int(run.get(key) or now_ts())
            except (TypeError, ValueError):
                return now_ts()
        conn.execute("INSERT INTO run_log(kind, started_at, finished_at, status, summary, details) VALUES (?,?,?,?,?,?)",
                     ("content", ts("started_ts"), ts("finished_ts"),
                      "ok" if not (failed_items or failed_w) else "warning",
                      f"{run.get('tool', 'pitv_content')}: {done_items} cached, {failed_items} failed; wanted {done_w} done, {failed_w} failed",
                      json.dumps([str(run.get("log_tail") or "")[-4000:]])))
    return {"items_done": done_items, "items_failed": failed_items, "wanted_done": done_w, "wanted_failed": failed_w}


def apply_report_files(conn: sqlite3.Connection, cache: MediaCache) -> int:
    """Apply report JSON files pitv_content dropped in `<cache>/reports` that we have not seen;
    each applied file gets a `.applied` marker next to it."""
    d = cache.reports_dir
    if d is None or not d.is_dir():
        return 0
    applied = 0
    for f in sorted(d.glob("*.json")):
        done = f.with_suffix(".json.applied")
        if done.exists():
            continue
        try:
            apply_report(conn, json.loads(f.read_text()))
            done.write_text(str(now_ts()))
            applied += 1
        except (OSError, ValueError) as exc:
            log.warning("report file %s not applied (will retry): %s", f.name, exc)
    return applied
