"""PiTV's half of the contract with pitv_content (docs/CONTENT_CONTRACT.md, schema 2).

The request manifest lists every file the schedule needs through the end of the next broadcast
day: copy or transcode it from the NAS, or fetch it online. Delivery reports record where each
file landed; material fetched online becomes a catalogue entry here and takes the placeholder
slots that asked for it.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from .db import all_settings, now_ts, row_to_dict, rows_to_dicts, tx
from .player.cache import MediaCache
from .player.hwdec import PI_HW_CODECS, is_raspberry_pi
from .scheduler.rules import broadcast_day_for, day_bounds, tz_of

MANIFEST_SCHEMA = 2
RESIZE_THRESHOLD = 30   # seconds; smaller differences between scheduled and delivered length are absorbed
MAX_WANTED_ATTEMPTS = 3
# Typical running times per kind, so pitv_content can reject obviously wrong search hits.
WANTED_MINUTES = {"music": [2, 8], "advert": [0.1, 2], "episode": [20, 60], "movie": [70, 180]}
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


def _identity(m: dict[str, Any]) -> dict[str, Any]:
    return {"kind": m["kind"], "show_title": m.get("show_title"), "season": m.get("season"),
            "episode": m.get("episode"), "title": m["title"], "year": m.get("year"), "artist": m.get("artist"),
            "duration": m.get("duration")}


def _fetch_fields(w: dict[str, Any], show_title: str | None, acquire: str) -> dict[str, Any]:
    hints = _search_hints(w, show_title)
    return {"search": {"phrase": hints[0], "hints": hints[1:],
                       "duration_minutes": WANTED_MINUTES.get(w["kind"], [1, 240]),
                       # A music video must be the exact release; a film or episode may carry a nearby year.
                       "year_tolerance": 0 if w["kind"] == "music" else 2},
            "dest_dir": _wanted_dest({**w, "title": show_title or w["title"]}, acquire)}


def manifest(conn: sqlite3.Connection, days: int = 1, now: int | None = None) -> dict[str, Any]:
    settings = all_settings(conn)
    tz = tz_of(conn)
    now = now or now_ts()
    cache = MediaCache.from_settings(settings)
    acquire = acquire_dir(settings)
    horizon = manifest_window(settings, tz, now, days)
    profile = settings.get("content_profile") or {}
    max_h = int(profile.get("height", 576))
    items: dict[str, dict[str, Any]] = {}

    def add(request_id: str, first_air: int, channel: int, build) -> None:
        it = items.get(request_id)
        if it is None:
            it = build()
            hours_ahead = max(0.0, (first_air - now) / 3600)
            it.update({"request_id": request_id, "channels": [], "first_air_ts": first_air,
                       "deadline_ts": first_air - 15 * 60, "priority": int(hours_ahead // 4)})
            items[request_id] = it
        if channel not in it["channels"]:
            it["channels"].append(channel)

    for r in rows_to_dicts(conn.execute(
            "SELECT s.start_ts, c.number AS channel, m.*, sh.title AS show_title FROM schedule s"
            " JOIN media m ON m.id = s.media_id JOIN channels c ON c.id = s.channel_id"
            " LEFT JOIN shows sh ON sh.id = m.show_id"
            " WHERE s.end_ts > ? AND s.start_ts < ? AND m.missing = 0 AND s.kind != 'filler'"
            " ORDER BY s.start_ts", (now, horizon))):
        def build(m=r):
            copy = cache.cache_copy(m)
            base = {"media_id": m["id"], "wanted_id": None, "uid": m.get("uid"), **_identity(m),
                    "already_cached": copy is not None, "transient": bool(m.get("transient"))}
            if m.get("origin", "nas") == "nas":
                stem, ext = Path(m["path"]).stem, Path(m["path"]).suffix
                hw = (m.get("vcodec") or "") in PI_HW_CODECS
                # Anything the Pi cannot decode in hardware, or well above the CRT's 576 lines,
                # is re-encoded to the profile; the rest is copied as it is.
                transcode = (not hw) or ((m.get("height") or 0) > max_h * 1.5)
                name = f"{m['id']}_{stem}.mp4" if transcode else f"{m['id']}_{stem}{ext}"
                return {**base, "action": "transcode" if transcode else "copy",
                        "source": {"path": m["path"], "vcodec": m.get("vcodec"), "height": m.get("height"),
                                   "interlaced": bool(m.get("interlaced")), "size": m.get("size")},
                        "target": str(copy or (cache.dir / name if cache.dir else name))}
            # Material that only ever lived in the cache: nothing to do while it is there; if it
            # has been evicted it has to be fetched again.
            if copy is not None:
                return {**base, "action": "copy", "source": None, "target": str(copy)}
            return {**base, "action": "fetch", "source": None,
                    **_fetch_fields({"kind": m["kind"], "title": m["title"], "year": m.get("year"),
                                     "season": m.get("season"), "episode": m.get("episode"),
                                     "artist": m.get("artist")}, m.get("show_title"), acquire)}
        add(f"m:{r['id']}", r["start_ts"], r["channel"], build)

    # Placeholders: line-up material not on disk, requested by wanted row.
    for r in rows_to_dicts(conn.execute(
            "SELECT s.start_ts, s.end_ts, c.number AS channel, w.*, l.title AS lineup_title FROM schedule s"
            " JOIN wanted w ON w.id = s.wanted_id JOIN channels c ON c.id = s.channel_id"
            " LEFT JOIN lineup l ON l.id = w.lineup_id"
            " WHERE s.media_id IS NULL AND s.end_ts > ? AND s.start_ts < ? AND w.status != 'done'"
            " ORDER BY s.start_ts", (now, horizon))):
        def build_w(w=r):
            show_title = w["lineup_title"] if w["kind"] == "episode" else None
            return {"media_id": None, "wanted_id": w["id"], "uid": None, "kind": w["kind"], "show_title": show_title,
                    "season": w.get("season"), "episode": w.get("episode"), "title": w["title"], "year": w.get("year"),
                    "artist": w.get("artist"), "duration": w["end_ts"] - w["start_ts"], "action": "fetch",
                    "source": None, "already_cached": False, "transient": bool(w.get("transient")),
                    "attempts": w.get("attempts", 0), **_fetch_fields(w, show_title, acquire)}
        add(f"w:{r['id']}", r["start_ts"], r["channel"], build_w)

    # Requests not tied to a slot: adverts and music videos added by hand, series gaps.
    scheduled = {it["wanted_id"] for it in items.values() if it.get("wanted_id")}
    wanted = []
    for w in rows_to_dicts(conn.execute(
            "SELECT w.*, sh.title AS show_title FROM wanted w LEFT JOIN shows sh ON sh.id = w.show_id"
            " WHERE w.status IN ('queued', 'failed') AND w.attempts < ? ORDER BY w.id", (MAX_WANTED_ATTEMPTS,))):
        if w["id"] in scheduled:
            continue
        wanted.append({"request_id": f"w:{w['id']}", "media_id": None, "wanted_id": w["id"], "uid": None,
                       "kind": w["kind"], "show_title": w.get("show_title"), "season": w.get("season"),
                       "episode": w.get("episode"), "title": w["title"], "year": w.get("year"), "artist": w.get("artist"),
                       "genre": w.get("genre"), "ref": w.get("ref"), "action": "fetch", "source": None,
                       "transient": bool(w.get("transient")), "attempts": w.get("attempts", 0),
                       **_fetch_fields(w, w.get("show_title"), acquire)})
    try:
        free_bytes = shutil.disk_usage(cache.dir).free if cache.dir and cache.dir.exists() else None
    except OSError:
        free_bytes = None
    return {"schema": MANIFEST_SCHEMA, "generated_ts": now, "horizon_ts": horizon, "days": days,
            "cache_dir": str(cache.dir) if cache.dir else "", "acquire_dir": acquire, "pi": is_raspberry_pi(),
            "free_bytes": free_bytes, "cache_max_bytes": cache.max_bytes,
            "running_marker": str(cache.running_marker) if cache.dir else None,
            "reports_dir": str(cache.reports_dir) if cache.dir else None,
            "profile": profile, "items": sorted(items.values(), key=lambda i: (i["priority"], i["deadline_ts"])),
            "wanted": wanted}


def protect_manifest(conn: sqlite3.Connection, cache: MediaCache, now: int | None = None) -> None:
    """Keep eviction away from everything in the current manifest (contract section 5)."""
    cache.protect({Path(i["target"]).name for i in manifest(conn, days=1, now=now)["items"] if i.get("target")})


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


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _resize_slots(conn: sqlite3.Connection, media_id: int, real: float) -> dict[int, int]:
    """Give future slots of `media_id` its delivered length. Returns {channel_id: earliest change}
    for the caller to rebuild; differences under RESIZE_THRESHOLD are absorbed."""
    changed: dict[int, int] = {}
    now = now_ts()
    for sl in conn.execute("SELECT id, channel_id, start_ts, end_ts FROM schedule WHERE media_id = ? AND replay = 0"
                           " AND start_ts > ?", (media_id, now)).fetchall():
        end = sl["start_ts"] + int(round(real))
        if abs(end - sl["end_ts"]) < RESIZE_THRESHOLD:
            continue
        conn.execute("UPDATE schedule SET end_ts = ? WHERE id = ?", (end, sl["id"]))
        at = min(end, sl["end_ts"])
        changed[sl["channel_id"]] = min(changed.get(sl["channel_id"], at), at)
    return changed


def _deliver_fetched(conn: sqlite3.Connection, wid: int, file: dict[str, Any], meta: dict[str, Any]) -> dict[int, int]:
    """Material fetched online: create its catalogue entry from the report, then hand it to the
    line-up and the placeholder slots that asked for it."""
    from .lineup import attach_delivery
    w = row_to_dict(conn.execute("SELECT * FROM wanted WHERE id = ?", (wid,)).fetchone())
    if w is None:
        return {}
    kind = meta.get("kind") or w["kind"]
    show_id = None
    if kind == "episode":
        entry = conn.execute("SELECT * FROM lineup WHERE id = ?", (w.get("lineup_id"),)).fetchone() if w.get("lineup_id") else None
        show_title = meta.get("show_title") or (entry["title"] if entry else None) or w["title"]
        year = meta.get("year") or w.get("year")
        show_id = entry["show_id"] if entry and entry["show_id"] else None
        if show_id is None:
            key = f"fetched:show:{show_title.lower()}:{year or ''}"
            row = conn.execute("SELECT id FROM shows WHERE path = ?", (key,)).fetchone()
            if row is None:
                cur = conn.execute("INSERT INTO shows(source_id, path, title, year, certificate, genres, plot, category, updated_at)"
                                   " VALUES (NULL, ?, ?, ?, ?, ?, ?, 'general', ?)",
                                   (key, show_title, year, meta.get("certificate"), json.dumps(meta.get("genres") or []),
                                    meta.get("plot"), now_ts()))
                show_id = int(cur.lastrowid)
            else:
                show_id = int(row["id"])
    season = w.get("season") if w.get("season") is not None else meta.get("season")
    episode = w.get("episode") if w.get("episode") is not None else meta.get("episode")
    title = w["title"] if kind == "episode" else (meta.get("title") or w["title"])
    fields = {
        "uid": str(meta.get("uid") or f"fetched:{wid}"), "source_id": None, "kind": kind, "show_id": show_id,
        "season": season, "episode": episode, "title": title, "year": meta.get("year") or w.get("year"),
        "origin": "online", "path": file["path"], "cache_path": file["path"], "size": file.get("size"),
        "duration": _number(file.get("duration")), "vcodec": file.get("vcodec"), "acodec": file.get("acodec"),
        "width": file.get("width"), "height": file.get("height"), "interlaced": int(bool(file.get("interlaced"))),
        "hwdec": int((file.get("vcodec") or "") in PI_HW_CODECS), "certificate": meta.get("certificate"),
        "genres": json.dumps(meta.get("genres") or []), "plot": meta.get("plot"), "artist": meta.get("artist") or w.get("artist"),
        "concert": int(bool(meta.get("concert"))), "family_safe": int(meta.get("family_safe", True) is not False),
        "transient": int(bool(w.get("transient"))), "missing": 0, "updated_at": now_ts(),
    }
    existing = conn.execute("SELECT id FROM media WHERE uid = ? OR path = ?", (fields["uid"], fields["path"])).fetchone()
    if existing:
        conn.execute("UPDATE media SET %s WHERE id = ?" % ", ".join(f"{k} = ?" for k in fields), (*fields.values(), existing["id"]))
        media_id = int(existing["id"])
    else:
        cur = conn.execute("INSERT INTO media(%s) VALUES (%s)" % (", ".join(fields), ", ".join("?" * len(fields))),
                           tuple(fields.values()))
        media_id = int(cur.lastrowid)
    conn.execute("UPDATE wanted SET status = 'done', progress = 1, dest_path = ?, message = ?, updated_at = ? WHERE id = ?",
                 (file["path"], "delivered by pitv_content", now_ts(), wid))
    return attach_delivery(conn, wid, media_id)


def _fail_wanted(conn: sqlite3.Connection, wid: int, message: str) -> None:
    msg = (message or "pitv_content failed")[:300]
    if "bot check" in msg.lower() or "rate limit" in msg.lower():
        # The provider, not the request, was the problem: retry without using up an attempt.
        conn.execute("UPDATE wanted SET status = 'queued', message = ?, updated_at = ? WHERE id = ?", (msg, now_ts(), wid))
    else:
        conn.execute("UPDATE wanted SET status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'queued' END,"
                     " attempts = attempts + 1, message = ?, updated_at = ? WHERE id = ?",
                     (MAX_WANTED_ATTEMPTS, msg, now_ts(), wid))


def apply_report(conn: sqlite3.Connection, report: dict[str, Any]) -> dict[str, Any]:
    """Record a delivery report (schema 2; schema 1 is still read during the transition)."""
    from .scheduler.build import rebuild_from
    counts = {"items_done": 0, "items_failed": 0, "wanted_done": 0, "wanted_failed": 0, "created": 0}
    entries: list[dict[str, Any]] = [e for e in (report.get("items") or []) if isinstance(e, dict)]
    # Schema 1 carried fetched material in a separate list with only a path.
    for w in report.get("wanted") or []:
        if isinstance(w, dict):
            entries.append({**w, "file": {"path": w.get("path")} if w.get("path") else None})
    refill: dict[int, int] = {}
    with tx(conn):
        for e in entries:
            status = e.get("status")
            wid = int(_number(e.get("wanted_id")) or 0)
            mid = int(_number(e.get("media_id")) or 0)
            file = e.get("file") if isinstance(e.get("file"), dict) else ({"path": e["path"]} if isinstance(e.get("path"), str) else None)
            usable = bool(file and isinstance(file.get("path"), str) and Path(file["path"]).is_file())
            if status == "skipped" and not usable:
                continue   # still being written by another process: it arrives measured in a later report
            if status in ("done", "skipped") and usable:
                if wid:
                    changes = _deliver_fetched(conn, wid, file, e.get("meta") if isinstance(e.get("meta"), dict) else {})
                    counts["wanted_done"] += 1
                    counts["created"] += 1
                elif mid:
                    conn.execute("UPDATE media SET cache_path = ?, cache_vcodec = ?, cache_interlaced = ?, updated_at = ? WHERE id = ?",
                                 (file["path"], file.get("vcodec"),
                                  None if file.get("interlaced") is None else int(bool(file["interlaced"])), now_ts(), mid))
                    real = _number(file.get("duration"))
                    changes = _resize_slots(conn, mid, real) if real else {}
                    counts["items_done"] += 1
                else:
                    continue
                for ch, at in changes.items():
                    refill[ch] = min(refill.get(ch, at), at)
            elif status == "failed" or (status == "done" and not usable):
                message = e.get("message") or ("reported file does not exist" if status == "done" else "")
                if wid:
                    _fail_wanted(conn, wid, message)
                    counts["wanted_failed"] += 1
                else:
                    counts["items_failed"] += 1
                    log.error("pitv_content could not deliver media %s: %s", mid or "?", message)
        run = report.get("run") if isinstance(report.get("run"), dict) else {}

        def ts(key: str) -> int:
            return int(_number(run.get(key)) or now_ts())
        conn.execute("INSERT INTO run_log(kind, started_at, finished_at, status, summary, details) VALUES (?,?,?,?,?,?)",
                     ("content", ts("started_ts"), ts("finished_ts"),
                      "ok" if not (counts["items_failed"] or counts["wanted_failed"]) else "warning",
                      f"{run.get('tool', 'pitv_content')}: {counts['items_done']} cached, {counts['items_failed']} failed;"
                      f" fetched {counts['wanted_done']}, {counts['wanted_failed']} failed",
                      json.dumps([str(run.get("log_tail") or "")[-4000:]])))
    for channel_id, from_ts in refill.items():
        if from_ts > now_ts():
            rebuild_from(conn, channel_id, from_ts)
    if counts["created"]:
        from .catalogue import write_mirror
        write_mirror(conn)
    return counts


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
