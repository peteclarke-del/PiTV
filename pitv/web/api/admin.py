"""Admin API: sources, library, channels, weighting/settings, schedule editing, system."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ... import __version__
from ...db import (DEFAULT_SETTINGS, all_settings, effective, now_ts, row_to_dict, rows_to_dicts,
                   set_setting, tx)
from ...scheduler.rules import parse_pattern
from .deps import SLOT_QUERY, admin_conn, media_public, show_public, slot_public

router = APIRouter(prefix="/api", dependencies=[Depends(admin_conn)])

SECRET_SETTINGS = {"admin_password_hash", "session_secret"}
SHOW_OVERRIDE_FIELDS = {"title", "year", "certificate", "genres", "plot", "kids"}
SHOW_DIRECT_FIELDS = {"home_channel_id", "mode", "anchor_time", "anchor_days", "rest_weeks", "excluded"}
MEDIA_OVERRIDE_FIELDS = {"title", "year", "certificate", "genres", "plot", "season", "episode"}
MEDIA_DIRECT_FIELDS = {"excluded", "channel_hint"}
CHANNEL_FIELDS = {"number", "name", "short_name", "colour", "enabled", "ads_enabled", "ads_per_break",
                  "pattern", "era_weights", "genre_weights", "kind_weights", "daypart_profile",
                  "overnight_replay_from", "idents_enabled", "description"}
JSON_CHANNEL_FIELDS = {"era_weights", "genre_weights", "kind_weights", "daypart_profile"}


# --- sources -----------------------------------------------------------------------------------

def _source_row(conn: sqlite3.Connection, sid: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404, "source not found")
    d = row_to_dict(row)
    d["available"] = Path(d["path"]).is_dir()
    d["item_count"] = conn.execute("SELECT COUNT(*) FROM media WHERE source_id = ? AND missing = 0", (sid,)).fetchone()[0]
    return d


@router.get("/sources")
def list_sources(conn: sqlite3.Connection = Depends(admin_conn)):
    return [_source_row(conn, r["id"]) for r in conn.execute("SELECT id FROM sources ORDER BY id")]


@router.post("/sources")
def create_source(body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    stype = body.get("type")
    if stype not in ("tv", "movie", "advert", "ident"):
        raise HTTPException(400, "type must be tv, movie, advert or ident")
    path = str(body.get("path", "")).strip()
    name = str(body.get("name", "")).strip() or Path(path).name or stype
    if not path:
        raise HTTPException(400, "path required")
    with tx(conn):
        cur = conn.execute("INSERT INTO sources(type, name, path, remote, enabled) VALUES (?,?,?,?,?)",
                           (stype, name, path, body.get("remote"), int(bool(body.get("enabled", True)))))
    return _source_row(conn, int(cur.lastrowid))


@router.put("/sources/{sid}")
def update_source(sid: int, body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    _source_row(conn, sid)
    fields = {k: body[k] for k in ("type", "name", "path", "remote", "enabled") if k in body}
    if "enabled" in fields:
        fields["enabled"] = int(bool(fields["enabled"]))
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        with tx(conn):
            conn.execute(f"UPDATE sources SET {sets} WHERE id = ?", (*fields.values(), sid))
    return _source_row(conn, sid)


@router.delete("/sources/{sid}")
def delete_source(sid: int, conn: sqlite3.Connection = Depends(admin_conn)):
    _source_row(conn, sid)
    with tx(conn):
        conn.execute("DELETE FROM sources WHERE id = ?", (sid,))
    return {"ok": True}


def _scan_job(request: Request, source_ids: list[int] | None, label: str):
    from ...library.scanner import scan_all
    cfg = request.app.state.cfg
    jobs = request.app.state.jobs

    def run(job):
        conn = __import__("pitv.db", fromlist=["connect"]).connect(cfg.db_path)
        try:
            def progress(msg, done, total):
                jobs.progress(job, msg, done, total)
            run_id = scan_all(conn, progress, cfg.ffprobe_binary, source_ids)
            row = conn.execute("SELECT * FROM run_log WHERE id = ?", (run_id,)).fetchone()
            job.notes.extend(json.loads(row["details"]))
            request.app.state.bus.publish_threadsafe("library", {"changed": True})
            return {"status": row["status"], "summary": row["summary"]}
        finally:
            conn.close()
    return jobs.submit("scan", label, run).public()


@router.post("/scan")
def scan_all_sources(request: Request):
    return _scan_job(request, None, "Scan all sources")


@router.post("/sources/{sid}/scan")
def scan_source(sid: int, request: Request, conn: sqlite3.Connection = Depends(admin_conn)):
    src = _source_row(conn, sid)
    return _scan_job(request, [sid], f"Scan {src['name']}")


@router.get("/browse")
def browse(path: str = "/"):
    """List directories for the source path picker."""
    p = Path(path or "/")
    if not p.is_dir():
        raise HTTPException(404, "not a directory")
    try:
        dirs = sorted(d.name for d in p.iterdir() if d.is_dir() and not d.name.startswith("."))
    except PermissionError:
        dirs = []
    return {"path": str(p), "parent": str(p.parent) if p != p.parent else None, "dirs": dirs}


# --- library -------------------------------------------------------------------------------------

@router.get("/library/summary")
def library_summary(conn: sqlite3.Connection = Depends(admin_conn)):
    kinds = {r["kind"]: r["n"] for r in conn.execute("SELECT kind, COUNT(*) AS n FROM media WHERE missing = 0 GROUP BY kind")}
    hw = conn.execute("SELECT SUM(hwdec) AS hw, COUNT(*) AS n FROM media WHERE missing = 0 AND kind IN ('episode','movie')").fetchone()
    attention = conn.execute("SELECT COUNT(*) FROM media WHERE missing = 0 AND attention IS NOT NULL").fetchone()[0]
    shows = conn.execute("SELECT COUNT(*) FROM shows WHERE missing = 0").fetchone()[0]
    hours = conn.execute("SELECT SUM(duration)/3600.0 FROM media WHERE missing = 0 AND kind IN ('episode','movie')").fetchone()[0]
    return {"kinds": kinds, "shows": shows, "hwdec": hw["hw"] or 0, "programmes": hw["n"] or 0,
            "attention": attention, "hours": round(hours or 0, 1)}


@router.get("/shows")
def list_shows(conn: sqlite3.Connection = Depends(admin_conn), q: str = "", channel: int | None = None):
    sql = ("SELECT s.*, (SELECT COUNT(*) FROM media m WHERE m.show_id = s.id AND m.missing = 0) AS episode_count,"
           " (SELECT COUNT(*) FROM media m WHERE m.show_id = s.id AND m.missing = 0 AND m.attention IS NOT NULL) AS attention_count,"
           " (SELECT MAX(season) FROM media m WHERE m.show_id = s.id AND m.missing = 0) AS seasons"
           " FROM shows s WHERE s.missing = 0")
    params: list[Any] = []
    if q:
        sql += " AND s.title LIKE ?"
        params.append(f"%{q}%")
    if channel is not None:
        sql += " AND s.home_channel_id = ?"
        params.append(channel)
    sql += " ORDER BY s.title"
    out = []
    latest = _latest_episode_map(conn)
    for r in conn.execute(sql, params):
        d = show_public(r)
        d["seasons"] = r["seasons"]
        d["end_year"] = (d["year"] + (r["seasons"] or 1) - 1) if d.get("year") else None
        d["last_aired"] = latest.get(r["id"])
        out.append(d)
    return out


def _latest_episode_map(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        "SELECT m.show_id, m.season, m.episode, MAX(x.ts) AS ts FROM ("
        "  SELECT media_id, MAX(start_ts) AS ts FROM schedule WHERE replay = 0 GROUP BY media_id"
        "  UNION ALL SELECT media_id, MAX(started_at) FROM history GROUP BY media_id) x"
        " JOIN media m ON m.id = x.media_id WHERE m.show_id IS NOT NULL GROUP BY m.show_id").fetchall()
    return {r["show_id"]: {"season": r["season"], "episode": r["episode"], "ts": r["ts"]} for r in rows}


@router.get("/shows/{sid}")
def get_show(sid: int, conn: sqlite3.Connection = Depends(admin_conn)):
    row = conn.execute("SELECT * FROM shows WHERE id = ?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404, "show not found")
    d = show_public(row)
    eps = conn.execute("SELECT * FROM media WHERE show_id = ? ORDER BY COALESCE(season, 999), COALESCE(episode, 999), path", (sid,)).fetchall()
    d["episodes"] = [media_public(e) for e in eps]
    cur = conn.execute("SELECT * FROM show_cursor WHERE show_id = ?", (sid,)).fetchone()
    d["cursor"] = dict(cur) if cur else None
    d["last_aired"] = _latest_episode_map(conn).get(sid)
    d["upcoming"] = [slot_public(r) for r in conn.execute(
        SLOT_QUERY + " WHERE m.show_id = ? AND s.start_ts > ? AND s.replay = 0 ORDER BY s.start_ts LIMIT 20",
        (sid, now_ts()))]
    return d


@router.put("/shows/{sid}")
def update_show(sid: int, body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    row = conn.execute("SELECT * FROM shows WHERE id = ?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404, "show not found")
    overrides = json.loads(row["overrides"] or "{}")
    direct: dict[str, Any] = {}
    for k, v in body.items():
        if k in SHOW_OVERRIDE_FIELDS:
            if v is None or v == "" or v == row[k] or (k == "genres" and v == json.loads(row["genres"] or "[]")):
                overrides.pop(k, None)
            else:
                overrides[k] = v
        elif k in SHOW_DIRECT_FIELDS:
            direct[k] = v
    if "mode" in direct and direct["mode"] not in ("auto", "strip", "weekly"):
        raise HTTPException(400, "mode must be auto, strip or weekly")
    if "anchor_days" in direct and direct["anchor_days"] is not None:
        direct["anchor_days"] = json.dumps([int(d) for d in direct["anchor_days"]])
    if "excluded" in direct:
        direct["excluded"] = int(bool(direct["excluded"]))
    direct["overrides"] = json.dumps(overrides)
    sets = ", ".join(f"{k} = ?" for k in direct)
    with tx(conn):
        conn.execute(f"UPDATE shows SET {sets} WHERE id = ?", (*direct.values(), sid))
    return get_show(sid, conn)


@router.post("/shows/{sid}/cursor")
def set_cursor(sid: int, body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    try:
        season, episode = int(body["season"]), int(body["episode"])
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, "season and episode required") from exc
    with tx(conn):
        conn.execute("INSERT INTO show_cursor(show_id, next_season, next_episode, set_at) VALUES (?,?,?,?)"
                     " ON CONFLICT(show_id) DO UPDATE SET next_season=excluded.next_season,"
                     " next_episode=excluded.next_episode, set_at=excluded.set_at",
                     (sid, season, episode, now_ts()))
    return {"ok": True}


@router.delete("/shows/{sid}/cursor")
def clear_cursor(sid: int, conn: sqlite3.Connection = Depends(admin_conn)):
    with tx(conn):
        conn.execute("DELETE FROM show_cursor WHERE show_id = ?", (sid,))
    return {"ok": True}


@router.get("/media")
def list_media(conn: sqlite3.Connection = Depends(admin_conn), kind: str | None = None, q: str = "",
               attention: int = 0, show_id: int | None = None, limit: int = 100, offset: int = 0,
               missing: int = 0):
    sql = "SELECT * FROM media WHERE 1 = 1"
    params: list[Any] = []
    if not missing:
        sql += " AND missing = 0"
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if q:
        sql += " AND title LIKE ?"
        params.append(f"%{q}%")
    if attention:
        sql += " AND attention IS NOT NULL"
    if show_id is not None:
        sql += " AND show_id = ?"
        params.append(show_id)
    total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
    sql += " ORDER BY title, season, episode LIMIT ? OFFSET ?"
    rows = conn.execute(sql, (*params, min(limit, 500), offset)).fetchall()
    return {"total": total, "items": [media_public(r) for r in rows]}


@router.get("/media/{mid}")
def get_media(mid: int, conn: sqlite3.Connection = Depends(admin_conn)):
    row = conn.execute("SELECT * FROM media WHERE id = ?", (mid,)).fetchone()
    if not row:
        raise HTTPException(404, "media not found")
    d = media_public(row, with_path=True)
    d["upcoming"] = [slot_public(r) for r in conn.execute(
        SLOT_QUERY + " WHERE s.media_id = ? AND s.start_ts > ? ORDER BY s.start_ts LIMIT 10", (mid, now_ts()))]
    d["history"] = [dict(r) for r in conn.execute(
        "SELECT * FROM history WHERE media_id = ? ORDER BY started_at DESC LIMIT 10", (mid,))]
    return d


@router.put("/media/{mid}")
def update_media(mid: int, body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    row = conn.execute("SELECT * FROM media WHERE id = ?", (mid,)).fetchone()
    if not row:
        raise HTTPException(404, "media not found")
    overrides = json.loads(row["overrides"] or "{}")
    direct: dict[str, Any] = {}
    for k, v in body.items():
        if k in MEDIA_OVERRIDE_FIELDS:
            if v is None or v == "" or v == row[k]:
                overrides.pop(k, None)
            else:
                overrides[k] = v
        elif k in MEDIA_DIRECT_FIELDS:
            direct[k] = int(bool(v)) if k == "excluded" else v
    direct["overrides"] = json.dumps(overrides)
    if "year" in overrides or "certificate" in overrides:
        # Clear attention flags the override resolves.
        att = [a for a in (row["attention"] or "").split("; ") if a and not (
            ("year" in overrides and a.startswith("No year")) or
            ("certificate" in overrides and a.startswith("No certificate")))]
        direct["attention"] = "; ".join(att) or None
    sets = ", ".join(f"{k} = ?" for k in direct)
    with tx(conn):
        conn.execute(f"UPDATE media SET {sets} WHERE id = ?", (*direct.values(), mid))
    return get_media(mid, conn)


@router.get("/library/attention")
def attention_list(conn: sqlite3.Connection = Depends(admin_conn)):
    rows = conn.execute("SELECT m.*, s.title AS show_title FROM media m LEFT JOIN shows s ON s.id = m.show_id"
                        " WHERE m.missing = 0 AND m.attention IS NOT NULL ORDER BY m.kind, m.title LIMIT 500").fetchall()
    out = []
    for r in rows:
        d = media_public(r)
        d["show_title"] = r["show_title"]
        out.append(d)
    return out


@router.get("/library/search")
def library_search(conn: sqlite3.Connection = Depends(admin_conn), q: str = "", kind: str = "programme", limit: int = 30):
    """Pick-list search for the schedule editor: shows (next episode) and movies."""
    out: list[dict[str, Any]] = []
    like = f"%{q}%"
    if kind in ("programme", "tv"):
        for r in conn.execute("SELECT * FROM shows WHERE missing = 0 AND excluded = 0 AND title LIKE ? ORDER BY title LIMIT ?", (like, limit)):
            eps = conn.execute("SELECT * FROM media WHERE show_id = ? AND missing = 0 AND excluded = 0 ORDER BY COALESCE(season,999), COALESCE(episode,999)", (r["id"],)).fetchall()
            out.append({"type": "show", "id": r["id"], "title": r["title"], "year": r["year"],
                        "episodes": [{"id": e["id"], "season": e["season"], "episode": e["episode"], "title": e["title"], "duration": e["duration"]} for e in eps[:200]]})
    if kind in ("programme", "movie"):
        for r in conn.execute("SELECT * FROM media WHERE kind = 'movie' AND missing = 0 AND excluded = 0 AND title LIKE ? ORDER BY title LIMIT ?", (like, limit)):
            out.append({"type": "movie", "id": r["id"], "title": r["title"], "year": r["year"], "duration": r["duration"], "certificate": r["certificate"]})
    return out


# --- channels ------------------------------------------------------------------------------------

def _channel(conn: sqlite3.Connection, cid: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "channel not found")
    d = row_to_dict(row)
    d["show_count"] = conn.execute("SELECT COUNT(*) FROM shows WHERE home_channel_id = ? AND missing = 0", (cid,)).fetchone()[0]
    d["pattern_tokens"] = parse_pattern(d["pattern"])
    return d


@router.get("/channels")
def list_channels(conn: sqlite3.Connection = Depends(admin_conn)):
    return [_channel(conn, r["id"]) for r in conn.execute("SELECT id FROM channels ORDER BY number")]


def _clean_channel_fields(body: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for k, v in body.items():
        if k not in CHANNEL_FIELDS:
            continue
        if k in JSON_CHANNEL_FIELDS:
            fields[k] = json.dumps(v) if v not in (None, "", {}, []) else None
        elif k in ("enabled", "ads_enabled", "idents_enabled"):
            fields[k] = int(bool(v))
        elif k in ("number", "ads_per_break"):
            fields[k] = int(v)
        elif k == "pattern":
            tokens = parse_pattern(str(v))
            fields[k] = ", ".join(tokens)
        else:
            fields[k] = v
    return fields


@router.post("/channels")
def create_channel(body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    fields = _clean_channel_fields(body)
    if "number" not in fields:
        nxt = conn.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM channels").fetchone()[0]
        fields["number"] = nxt
    fields.setdefault("name", f"PiTV {fields['number']}")
    fields.setdefault("short_name", str(fields["number"]))
    cols = ", ".join(fields)
    vals = ", ".join("?" for _ in fields)
    try:
        with tx(conn):
            cur = conn.execute(f"INSERT INTO channels({cols}) VALUES ({vals})", tuple(fields.values()))
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, f"channel number already used ({exc})") from exc
    return _channel(conn, int(cur.lastrowid))


@router.put("/channels/{cid}")
def update_channel(cid: int, body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    _channel(conn, cid)
    fields = _clean_channel_fields(body)
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        try:
            with tx(conn):
                conn.execute(f"UPDATE channels SET {sets} WHERE id = ?", (*fields.values(), cid))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, f"channel number already used ({exc})") from exc
    return _channel(conn, cid)


@router.delete("/channels/{cid}")
def delete_channel(cid: int, conn: sqlite3.Connection = Depends(admin_conn)):
    _channel(conn, cid)
    with tx(conn):
        conn.execute("UPDATE shows SET home_channel_id = NULL WHERE home_channel_id = ?", (cid,))
        conn.execute("DELETE FROM channels WHERE id = ?", (cid,))
    return {"ok": True}


@router.post("/channels/rebalance")
def rebalance_channels(conn: sqlite3.Connection = Depends(admin_conn)):
    """Clear automatic home-channel assignments and redistribute shows evenly."""
    from ...library.scanner import _assign_home_channels
    with tx(conn):
        conn.execute("UPDATE shows SET home_channel_id = NULL")
        _assign_home_channels(conn)
    return list_channels(conn)


# --- settings -------------------------------------------------------------------------------------

@router.get("/settings")
def get_settings(conn: sqlite3.Connection = Depends(admin_conn)):
    s = all_settings(conn)
    return {k: v for k, v in s.items() if k not in SECRET_SETTINGS}


@router.put("/settings")
def put_settings(body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    unknown = [k for k in body if k not in DEFAULT_SETTINGS or k in SECRET_SETTINGS]
    if unknown:
        raise HTTPException(400, f"unknown settings: {', '.join(unknown)}")
    with tx(conn):
        for k, v in body.items():
            set_setting(conn, k, v)
    return get_settings(conn)


@router.post("/settings/reset")
def reset_settings(body: dict[str, Any] = Body(default={}), conn: sqlite3.Connection = Depends(admin_conn)):
    keys = body.get("keys") or [k for k in DEFAULT_SETTINGS if k not in SECRET_SETTINGS]
    with tx(conn):
        for k in keys:
            if k in DEFAULT_SETTINGS and k not in SECRET_SETTINGS:
                set_setting(conn, k, DEFAULT_SETTINGS[k])
    return get_settings(conn)


# --- schedule editing -------------------------------------------------------------------------------

@router.post("/schedule/build")
def schedule_build(request: Request, body: dict[str, Any] = Body(default={})):
    from ...scheduler.build import build_horizon, parse_day
    cfg = request.app.state.cfg
    jobs = request.app.state.jobs
    start = parse_day(body["start_day"]) if body.get("start_day") else None
    days = int(body["days"]) if body.get("days") else None
    force = bool(body.get("force", False))
    channels = [int(c) for c in body.get("channels", [])] or None

    def run(job):
        from ... import db as dbm
        conn = dbm.connect(cfg.db_path)
        try:
            result = build_horizon(conn, start_day=start, days=days, force=force, channel_numbers=channels,
                                   progress=lambda m: jobs.progress(job, m))
            job.notes.extend(result.get("notes", []))
            request.app.state.bus.publish_threadsafe("schedule", {"changed": True})
            return result
        finally:
            conn.close()
    return jobs.submit("schedule", "Build schedule" + (" (force)" if force else ""), run).public()


def _slot(conn: sqlite3.Connection, slot_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM schedule WHERE id = ?", (slot_id,)).fetchone()
    if not row:
        raise HTTPException(404, "slot not found")
    return row


def _editable(row: sqlite3.Row) -> None:
    if row["start_ts"] <= now_ts():
        raise HTTPException(409, "cannot edit a slot that has already started")
    if row["replay"]:
        raise HTTPException(409, "overnight replays follow the day's schedule; edit the original slot")


def _rebuild(request: Request, conn: sqlite3.Connection, channel_id: int, from_ts: int) -> dict[str, Any]:
    from ...scheduler.build import rebuild_from
    result = rebuild_from(conn, channel_id, from_ts)
    request.app.state.bus.publish_threadsafe("schedule", {"changed": True})
    return result


@router.post("/schedule/slots/{slot_id}/lock")
def lock_slot(slot_id: int, body: dict[str, Any] = Body(default={}), conn: sqlite3.Connection = Depends(admin_conn)):
    row = _slot(conn, slot_id)
    locked = int(bool(body.get("locked", True)))
    with tx(conn):
        conn.execute("UPDATE schedule SET locked = ? WHERE id = ?", (locked, row["id"]))
    return {"ok": True, "locked": locked}


@router.delete("/schedule/slots/{slot_id}")
def delete_slot(slot_id: int, request: Request, conn: sqlite3.Connection = Depends(admin_conn)):
    row = _slot(conn, slot_id)
    _editable(row)
    with tx(conn):
        conn.execute("DELETE FROM schedule WHERE id = ?", (slot_id,))
    return _rebuild(request, conn, row["channel_id"], row["start_ts"])


@router.post("/schedule/slots/{slot_id}/replace")
def replace_slot(slot_id: int, request: Request, body: dict[str, Any] = Body(...),
                 conn: sqlite3.Connection = Depends(admin_conn)):
    row = _slot(conn, slot_id)
    _editable(row)
    media = conn.execute("SELECT m.*, s.title AS show_title FROM media m LEFT JOIN shows s ON s.id = m.show_id WHERE m.id = ?",
                         (int(body.get("media_id", 0)),)).fetchone()
    if not media or not media["duration"]:
        raise HTTPException(404, "media not found or has no duration")
    title, subtitle = _titles(media)
    end = row["start_ts"] + int(round(media["duration"]))
    with tx(conn):
        conn.execute("UPDATE schedule SET media_id = ?, end_ts = ?, title = ?, subtitle = ?, kind = 'programme', locked = 1, offset = 0 WHERE id = ?",
                     (media["id"], end, title, subtitle, slot_id))
    return _rebuild(request, conn, row["channel_id"], end)


@router.post("/schedule/insert")
def insert_slot(request: Request, body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    try:
        channel_id, start_ts, media_id = int(body["channel_id"]), int(body["start_ts"]), int(body["media_id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, "channel_id, start_ts and media_id required") from exc
    if start_ts <= now_ts():
        raise HTTPException(409, "start must be in the future")
    media = conn.execute("SELECT m.*, s.title AS show_title FROM media m LEFT JOIN shows s ON s.id = m.show_id WHERE m.id = ?", (media_id,)).fetchone()
    if not media or not media["duration"]:
        raise HTTPException(404, "media not found or has no duration")
    from ...scheduler.rules import broadcast_day_for, tz_of
    day = broadcast_day_for(start_ts, all_settings(conn), tz_of(conn)).isoformat()
    title, subtitle = _titles(media)
    end = start_ts + int(round(media["duration"]))
    overlapping = conn.execute("SELECT * FROM schedule WHERE channel_id = ? AND replay = 0 AND start_ts < ? AND end_ts > ? ORDER BY start_ts",
                               (channel_id, end, start_ts)).fetchall()
    cut = start_ts
    with tx(conn):
        for o in overlapping:
            if o["locked"] and o["start_ts"] > now_ts():
                raise HTTPException(409, f"overlaps locked slot '{o['title']}'")
            if o["start_ts"] <= now_ts():
                # The programme on air now: truncate it so the inserted item can start on time.
                conn.execute("UPDATE schedule SET end_ts = ? WHERE id = ?", (start_ts, o["id"]))
            else:
                cut = min(cut, o["start_ts"])
                conn.execute("DELETE FROM schedule WHERE id = ?", (o["id"],))
        conn.execute("INSERT INTO schedule(channel_id, day, start_ts, end_ts, media_id, offset, kind, part, replay, locked, title, subtitle)"
                     " VALUES (?,?,?,?,?,0,'programme',1,0,1,?,?)", (channel_id, day, start_ts, end, media_id, title, subtitle))
    return _rebuild(request, conn, channel_id, cut)


@router.post("/schedule/rebuild")
def rebuild(request: Request, body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    try:
        channel_id, from_ts = int(body["channel_id"]), int(body["from_ts"])
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, "channel_id and from_ts required") from exc
    return _rebuild(request, conn, channel_id, from_ts)


def _titles(media: sqlite3.Row) -> tuple[str, str]:
    if media["kind"] == "episode":
        se = ""
        if media["season"] is not None and media["episode"] is not None:
            se = f"S{media['season']:02d}E{media['episode']:02d} "
        return media["show_title"] or media["title"], f"{se}{media['title'] or ''}".strip()
    year = f"({media['year']})" if media["year"] else ""
    return media["title"], " ".join(x for x in (year, media["certificate"] or "") if x)


# --- system ---------------------------------------------------------------------------------------------

def _cmd(args: list[str], timeout: float = 3) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


@router.get("/system")
def system_info(request: Request, conn: sqlite3.Connection = Depends(admin_conn)):
    cfg = request.app.state.cfg
    services = {}
    for svc in ("pitv-player", "pitv-web", "pitv-scan.timer", "pitv-schedule.timer"):
        state = _cmd(["systemctl", "is-active", svc])
        services[svc] = state or "unknown"
    time_info = {}
    for line in _cmd(["timedatectl", "show"]).splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            time_info[k] = v
    mounts = []
    for r in conn.execute("SELECT * FROM sources ORDER BY id"):
        p = Path(r["path"])
        usage = None
        if p.is_dir():
            try:
                u = shutil.disk_usage(p)
                usage = {"total": u.total, "free": u.free}
            except OSError:
                usage = None
        mounts.append({"name": r["name"], "path": r["path"], "available": p.is_dir(), "usage": usage})
    data_usage = shutil.disk_usage(cfg.data_dir) if cfg.data_dir.exists() else None
    load = os.getloadavg() if hasattr(os, "getloadavg") else None
    temp = ""
    tpath = Path("/sys/class/thermal/thermal_zone0/temp")
    if tpath.exists():
        try:
            temp = f"{int(tpath.read_text().strip()) / 1000:.1f}"
        except ValueError:
            temp = ""
    return {
        "version": __version__, "python": sys.version.split()[0], "mpv": _cmd([cfg.mpv_binary, "--version"]).split("\n")[0],
        "hostname": _cmd(["hostname"]), "uptime": _cmd(["uptime", "-p"]), "load": load, "temperature_c": temp,
        "time": {"now": now_ts(), "local": datetime.now().isoformat(timespec="seconds"),
                 "ntp": time_info.get("NTPSynchronized"), "timezone": time_info.get("Timezone")},
        "services": services, "mounts": mounts,
        "data": {"path": str(cfg.data_dir), "db": str(cfg.db_path),
                 "db_size": cfg.db_path.stat().st_size if cfg.db_path.exists() else 0,
                 "free": data_usage.free if data_usage else None, "total": data_usage.total if data_usage else None},
        "jobs": request.app.state.jobs.recent(10),
    }


@router.post("/system/service/{name}/{action}")
def service_action(name: str, action: str):
    if name not in ("pitv-player", "pitv-web") or action not in ("restart", "stop", "start"):
        raise HTTPException(400, "unsupported service or action")
    out = subprocess.run(["sudo", "-n", "systemctl", action, name], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise HTTPException(500, out.stderr.strip() or "systemctl failed")
    return {"ok": True}


@router.get("/runs")
def runs(conn: sqlite3.Connection = Depends(admin_conn), limit: int = 20):
    rows = conn.execute("SELECT * FROM run_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return rows_to_dicts(rows)


@router.get("/history")
def history(conn: sqlite3.Connection = Depends(admin_conn), limit: int = 100):
    rows = conn.execute("SELECT h.*, c.number AS channel_number, c.name AS channel_name FROM history h"
                        " LEFT JOIN channels c ON c.id = h.channel_id ORDER BY h.started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@router.get("/jobs")
def jobs(request: Request):
    return request.app.state.jobs.recent(20)


@router.get("/export")
def export_overrides(conn: sqlite3.Connection = Depends(admin_conn)):
    """Everything an admin has changed, for backup: settings, channels, sources, overrides."""
    shows = [{"folder": r["path"].rsplit("/", 1)[-1], "overrides": json.loads(r["overrides"]), "home_channel_id": r["home_channel_id"],
              "mode": r["mode"], "anchor_time": r["anchor_time"], "anchor_days": r["anchor_days"], "rest_weeks": r["rest_weeks"], "excluded": r["excluded"]}
             for r in conn.execute("SELECT * FROM shows WHERE overrides != '{}' OR excluded = 1 OR mode != 'auto'")]
    media = [{"filename": r["path"].rsplit("/", 1)[-1], "overrides": json.loads(r["overrides"]), "excluded": r["excluded"]}
             for r in conn.execute("SELECT * FROM media WHERE overrides != '{}' OR excluded = 1")]
    return {"exported_at": now_ts(), "settings": {k: v for k, v in all_settings(conn).items() if k not in SECRET_SETTINGS},
            "channels": rows_to_dicts(conn.execute("SELECT * FROM channels")), "sources": rows_to_dicts(conn.execute("SELECT * FROM sources")),
            "shows": shows, "media": media}
