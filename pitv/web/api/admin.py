"""Admin API: sources, library, channels, weighting/settings, schedule editing, system."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ... import __version__
from ... import db as dbm
from ...db import (DEFAULT_SETTINGS, all_settings, get_setting, now_ts, row_to_dict, rows_to_dicts,
                   set_setting, tx)
from ...guide import SLOT_QUERY
from ... import catalogue, tool_client
from ... import lineup as lineup_mod
from ...logsetup import log_dir, tail
from ...scheduler.build import build_horizon, parse_day, rebuild_from, slot_titles
from ...scheduler.rules import broadcast_day_for, parse_pattern, tz_of
from .deps import admin_conn, media_public, run_cmd, show_public, slot_public
from .services import CONTENT_RUN, SERVICE_ACTIONS, services
from .settings_rules import SettingError, check_setting

router = APIRouter(prefix="/api", dependencies=[Depends(admin_conn)])

SECRET_SETTINGS = {"admin_password_hash", "session_secret"}
SHOW_OVERRIDE_FIELDS = {"title", "year", "certificate", "genres", "plot", "kids"}
SHOW_DIRECT_FIELDS = {"home_channel_id", "mode", "anchor_time", "anchor_days", "rest_weeks", "excluded", "category"}
SHOW_CATEGORIES = ("general", "sport", "kids", "cartoon")
MEDIA_OVERRIDE_FIELDS = {"title", "year", "certificate", "genres", "plot", "season", "episode", "artist"}
MEDIA_DIRECT_FIELDS = {"excluded", "channel_hint", "concert", "family_safe", "home_channel_id"}
CHANNEL_FIELDS = {"number", "name", "short_name", "colour", "enabled", "ads_enabled", "ads_per_break",
                  "pattern", "era_weights", "genre_weights", "kind_weights", "daypart_profile",
                  "overnight_replay_from", "idents_enabled", "description", "content", "family_safe_ads",
                  "allowed_genres", "excluded_genres", "nas_only"}
JSON_CHANNEL_FIELDS = {"era_weights", "genre_weights", "kind_weights", "daypart_profile", "allowed_genres", "excluded_genres"}
# What the web service may ask systemd to do; must stay in step with the sudoers rule in
# setup/install.sh. Stopping the web service from the web is deliberately not offered.
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")


# --- allowed folders --------------------------------------------------------------------------

def browse_roots(settings: dict[str, Any]) -> list[Path]:
    """Directories an admin may browse or register as a source: the configured roots (media
    mounts by default) plus the cache, the acquire folder and the service user's home."""
    roots = [str(r) for r in (settings.get("browse_roots") or []) if isinstance(r, str)]
    roots += [settings.get("cache_dir") or "", settings.get("acquire_dir") or "", str(Path.home())]
    out: list[Path] = []
    for r in roots:
        if r and r.startswith("/"):
            real = Path(r).resolve()
            if real not in out:
                out.append(real)
    return out


def allowed_dir(path: str, roots: list[Path]) -> Path:
    """The real path if it lies under one of the roots; 400/403 otherwise. Resolving first
    means neither '..' segments nor a symlink planted on a share can escape."""
    if not path or "\0" in path or not path.startswith("/"):
        raise HTTPException(400, "path must be absolute")
    real = Path(path).resolve()
    if not any(real == r or real.is_relative_to(r) for r in roots):
        raise HTTPException(403, "that folder is outside the browsable roots (see the browse_roots setting)")
    return real


# --- sources (owned by pitv_content) -------------------------------------------------------------

SOURCE_TYPES = ("tv", "movie", "advert", "ident", "music")
SOURCE_CATEGORIES = ("general", "sport", "kids")


def _mirror_sources(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The sources as last seen in pitv_content's index, for when its API is down."""
    out = []
    for r in rows_to_dicts(conn.execute("SELECT * FROM sources ORDER BY location, name")):
        n = conn.execute("SELECT COUNT(*) FROM media WHERE source_id = ? AND missing = 0", (r["id"],)).fetchone()[0]
        out.append({"id": r["uid"], "name": r["name"], "type": r["type"], "category": r["category"], "root": r["path"],
                    "remote": r["remote"], "location": r["location"], "enabled": bool(r["enabled"]),
                    "health": {"mounted": Path(r["path"]).is_dir(), "items": n, "last_indexed_ts": r["last_indexed_at"]}})
    return out


@router.get("/sources")
def list_sources(conn: sqlite3.Connection = Depends(admin_conn)):
    """pitv_content's sources. PiTV does not read them; they are shown and edited here because
    pitv_content has no interface of its own."""
    status, payload = tool_client.request(tool_client.base_url(all_settings(conn)), "GET", "sources", timeout=10)
    if status == 200 and isinstance(payload, (list, dict)):
        items = payload if isinstance(payload, list) else payload.get("sources", [])
        return {"owner": "pitv_content", "offline": False, "sources": items}
    return {"owner": "pitv_content", "offline": True, "sources": _mirror_sources(conn),
            "error": payload.get("error") if isinstance(payload, dict) else f"HTTP {status}"}


@router.put("/sources")
def put_source(body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    """Add, edit or delete one of pitv_content's sources through its API."""
    if not isinstance(body.get("id"), str) or not body["id"].strip():
        raise HTTPException(400, "id required")
    if not body.get("delete"):
        if "type" in body and body["type"] not in SOURCE_TYPES:
            raise HTTPException(400, "type must be tv, movie, advert, ident or music")
        if "category" in body and (body["category"] or "general") not in SOURCE_CATEGORIES:
            raise HTTPException(400, "category must be general, sport or kids")
        if body.get("root"):
            body["root"] = str(allowed_dir(str(body["root"]), browse_roots(all_settings(conn))))
    status, payload = tool_client.request(tool_client.base_url(all_settings(conn)), "PUT", "sources", body=body, timeout=15)
    if isinstance(payload, dict) and payload.get("offline"):
        raise HTTPException(503, "pitv_content is not running; sources can only be changed through it")
    if status >= 400:
        # Pass pitv_content's field errors through in the contract's shape.
        return JSONResponse(status_code=status, content=payload if isinstance(payload, dict) else {"error": "rejected"})
    return payload


# --- catalogue (imported from pitv_content's library index) ----------------------------------------

def catalogue_job(request: Request, reindex: bool, label: str, doc: dict[str, Any] | None = None) -> dict[str, Any]:
    """Import the index as a background job (its own connection; progress over SSE)."""
    cfg = request.app.state.cfg
    jobs = request.app.state.jobs

    def run(job):
        conn = dbm.connect(cfg.db_path)
        try:
            jobs.progress(job, "asking pitv_content to re-index" if reindex else "importing the library index")
            result = catalogue.import_and_place(conn, doc, "uploaded file") if doc is not None \
                else catalogue.refresh(conn, reindex=reindex)
            if result.get("status") == "error":
                raise RuntimeError(result["summary"])   # the job reads "failed", with the reason
            request.app.state.bus.publish_threadsafe("library", {"changed": True})
            return {"status": result.get("status", "ok"), "summary": result["summary"]}
        finally:
            conn.close()
    return jobs.submit("catalogue", label, run).public()


@router.get("/catalogue")
def catalogue_status(conn: sqlite3.Connection = Depends(admin_conn)):
    settings = all_settings(conn)
    path = catalogue.index_file(settings)
    return {"last_import": catalogue.last_import(conn), "index_file": str(path) if path else None,
            "index_file_exists": bool(path and path.exists())}


@router.post("/catalogue/refresh")
def catalogue_refresh(request: Request, body: dict[str, Any] = Body(default={})):
    """Import pitv_content's current index; with `reindex`, ask it to re-index the sources first."""
    reindex = bool(body.get("reindex"))
    return catalogue_job(request, reindex, "Re-index and import the catalogue" if reindex else "Import the catalogue")


@router.post("/catalogue/import")
def catalogue_import(request: Request, body: dict[str, Any] = Body(...)):
    """Import an index document supplied directly (development, or a saved index)."""
    if body.get("schema") != catalogue.SCHEMA:
        raise HTTPException(400, f"expected a schema {catalogue.SCHEMA} library index")
    return catalogue_job(request, False, "Import an uploaded index", doc=body)


@router.get("/catalogue/export")
def catalogue_export(conn: sqlite3.Connection = Depends(admin_conn)):
    return catalogue.export(conn)


@router.get("/browse")
def browse(path: str = "/", conn: sqlite3.Connection = Depends(admin_conn)):
    """List directories for the source path picker, within the allowed roots only. "/" lists
    the roots themselves (as paths relative to "/", which is how the picker joins them)."""
    roots = browse_roots(all_settings(conn))
    if path in ("", "/"):
        return {"path": "/", "parent": None, "dirs": [str(r)[1:] for r in roots if r.is_dir()]}
    p = allowed_dir(path, roots)
    if not p.is_dir():
        raise HTTPException(404, "not a directory")
    try:
        dirs = sorted(d.name for d in p.iterdir() if d.is_dir() and not d.name.startswith("."))
    except PermissionError:
        dirs = []
    parent = p.parent
    up = str(parent) if any(parent == r or parent.is_relative_to(r) for r in roots) else "/"
    return {"path": str(p), "parent": up, "dirs": dirs}


# --- library -------------------------------------------------------------------------------------

@router.get("/library/summary")
def library_summary(conn: sqlite3.Connection = Depends(admin_conn)):
    kinds = {r["kind"]: r["n"] for r in conn.execute("SELECT kind, COUNT(*) AS n FROM media WHERE missing = 0 GROUP BY kind")}
    hw = conn.execute("SELECT SUM(hwdec) AS hw, COUNT(*) AS n FROM media WHERE missing = 0 AND kind IN ('episode','movie')").fetchone()
    attention = conn.execute("SELECT COUNT(*) FROM media WHERE missing = 0 AND attention IS NOT NULL").fetchone()[0]
    shows = conn.execute("SELECT COUNT(*) FROM shows WHERE missing = 0").fetchone()[0]
    hours = conn.execute("SELECT SUM(duration)/3600.0 FROM media WHERE missing = 0 AND kind IN ('episode','movie')").fetchone()[0]
    concerts = conn.execute("SELECT COUNT(*) FROM media WHERE missing = 0 AND kind = 'music' AND concert = 1").fetchone()[0]
    avail = conn.execute(
        "SELECT SUM(cache_path IS NOT NULL OR origin != 'nas') AS cached, SUM(origin = 'nas' AND cache_path IS NULL) AS nas_only,"
        " SUM(origin = 'online') AS online FROM media WHERE missing = 0").fetchone()
    return {"kinds": kinds, "shows": shows, "hwdec": hw["hw"] or 0, "programmes": hw["n"] or 0,
            "attention": attention, "hours": round(hours or 0, 1), "concerts": concerts,
            "cached": avail["cached"] or 0, "nas_only": avail["nas_only"] or 0, "online": avail["online"] or 0,
            "last_import": catalogue.last_import(conn)}


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
    if "home_channel_id" in direct:
        target = direct.pop("home_channel_id")
        if target:
            lineup_mod.add(conn, int(target), show_id=sid)
    if "mode" in direct and direct["mode"] not in ("auto", "strip", "weekly"):
        raise HTTPException(400, "mode must be auto, strip or weekly")
    if "category" in direct and direct["category"] not in SHOW_CATEGORIES:
        raise HTTPException(400, "category must be general, sport, kids or cartoon")
    try:
        if "anchor_days" in direct and direct["anchor_days"] is not None:
            direct["anchor_days"] = json.dumps(sorted({int(d) % 7 for d in direct["anchor_days"]}))
        if "rest_weeks" in direct:
            direct["rest_weeks"] = max(0, int(direct["rest_weeks"]))
        if "home_channel_id" in direct and direct["home_channel_id"] is not None:
            direct["home_channel_id"] = int(direct["home_channel_id"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "anchor_days, rest_weeks and home_channel_id must be numbers") from exc
    if direct.get("anchor_time") is not None and not _HHMM.match(str(direct.get("anchor_time"))):
        raise HTTPException(400, "anchor_time must be HH:MM")
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
    rows = conn.execute(sql, (*params, max(1, min(limit, 500)), max(0, offset))).fetchall()
    return {"total": total, "items": [media_public(r) for r in rows]}


@router.get("/music/facets")
def music_facets(conn: sqlite3.Connection = Depends(admin_conn)):
    """Counts of music videos by genre and decade, for the music channel editor."""
    genres: dict[str, int] = {}
    decades: dict[str, int] = {}
    concerts = 0
    for r in conn.execute("SELECT genres, year, concert FROM media WHERE kind = 'music' AND missing = 0 AND excluded = 0"):
        for g in json.loads(r["genres"] or "[]"):
            genres[g] = genres.get(g, 0) + 1
        if r["year"]:
            d = f"{(r['year'] // 10) * 10}s"
            decades[d] = decades.get(d, 0) + 1
        concerts += int(r["concert"] or 0)
    return {"genres": dict(sorted(genres.items())), "decades": dict(sorted(decades.items())), "concerts": concerts}


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
            direct[k] = int(bool(v)) if k in ("excluded", "concert", "family_safe") else v
    if "home_channel_id" in direct:
        target = direct.pop("home_channel_id")
        if target and row["kind"] == "movie":
            lineup_mod.add(conn, int(target), media_id=mid)
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
    limit = max(1, min(limit, 100))
    if kind in ("programme", "tv"):
        shows = conn.execute("SELECT * FROM shows WHERE missing = 0 AND excluded = 0 AND title LIKE ? ORDER BY title LIMIT ?", (like, limit)).fetchall()
        eps_by_show: dict[int, list[dict[str, Any]]] = {r["id"]: [] for r in shows}
        if shows:
            marks = ",".join("?" for _ in shows)
            for e in conn.execute(f"SELECT id, show_id, season, episode, title, duration FROM media WHERE show_id IN ({marks})"
                                  " AND missing = 0 AND excluded = 0 ORDER BY show_id, COALESCE(season,999), COALESCE(episode,999)",
                                  [r["id"] for r in shows]):
                lst = eps_by_show[e["show_id"]]
                if len(lst) < 200:
                    lst.append({"id": e["id"], "season": e["season"], "episode": e["episode"], "title": e["title"], "duration": e["duration"]})
        for r in shows:
            out.append({"type": "show", "id": r["id"], "title": r["title"], "year": r["year"], "episodes": eps_by_show[r["id"]]})
    if kind in ("programme", "movie"):
        for r in conn.execute("SELECT * FROM media WHERE kind = 'movie' AND missing = 0 AND excluded = 0 AND title LIKE ? ORDER BY title LIMIT ?", (like, limit)):
            out.append({"type": "movie", "id": r["id"], "title": r["title"], "year": r["year"], "duration": r["duration"], "certificate": r["certificate"]})
    if kind in ("programme", "music"):
        for r in conn.execute("SELECT * FROM media WHERE kind = 'music' AND missing = 0 AND excluded = 0 AND title LIKE ? ORDER BY title LIMIT ?", (like, limit)):
            out.append({"type": "music", "id": r["id"], "title": r["title"], "year": r["year"], "duration": r["duration"], "concert": r["concert"]})
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
            if v not in (None, "", {}, []) and not isinstance(v, (dict, list)):
                raise HTTPException(400, f"{k} must be an object or list")
            fields[k] = json.dumps(v) if v not in (None, "", {}, []) else None
        elif k in ("enabled", "ads_enabled", "idents_enabled", "family_safe_ads"):
            fields[k] = int(bool(v))
        elif k in ("number", "ads_per_break"):
            try:
                fields[k] = int(v)
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, f"{k} must be a number") from exc
            if fields[k] < 1 or (k == "number" and fields[k] > 999) or (k == "ads_per_break" and fields[k] > 10):
                raise HTTPException(400, f"{k} out of range")
        elif k == "pattern":
            fields[k] = ", ".join(parse_pattern(str(v)))
        elif k == "content":
            if v not in ("general", "music", "cartoons"):
                raise HTTPException(400, "content must be general, music or cartoons")
            fields[k] = v
        elif k == "nas_only":
            if v not in ("inherit", "yes", "no"):
                raise HTTPException(400, "nas_only must be inherit, yes or no")
            fields[k] = v
        elif k == "colour":
            if not isinstance(v, str) or not _COLOUR.match(v):
                raise HTTPException(400, "colour must be #rrggbb")
            fields[k] = v.lower()
        elif k == "overnight_replay_from":
            if not isinstance(v, str) or not _HHMM.match(v):
                raise HTTPException(400, "overnight_replay_from must be HH:MM")
            fields[k] = v
        else:
            if not isinstance(v, str):
                raise HTTPException(400, f"{k} must be a string")
            fields[k] = v.strip()[:200]
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
    """Redistribute unpinned line-up entries across channels by their genre lists."""
    lineup_mod.generate(conn, rebalance=True)
    return list_channels(conn)


# --- settings -------------------------------------------------------------------------------------

@router.get("/settings")
def get_settings(conn: sqlite3.Connection = Depends(admin_conn)):
    s = all_settings(conn)
    return {k: v for k, v in s.items() if k not in SECRET_SETTINGS}


@router.put("/settings")
def put_settings(request: Request, body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    unknown = [k for k in body if k not in DEFAULT_SETTINGS or k in SECRET_SETTINGS]
    if unknown:
        raise HTTPException(400, f"unknown settings: {', '.join(unknown)}")
    clean = {}
    for k, v in body.items():
        try:
            clean[k] = check_setting(k, v)
        except SettingError as exc:
            raise HTTPException(400, str(exc)) from exc
    with tx(conn):
        for k, v in clean.items():
            set_setting(conn, k, v)
    request.app.state.player.call("settings-changed")
    return get_settings(conn)


@router.post("/settings/reset")
def reset_settings(request: Request, body: dict[str, Any] = Body(default={}), conn: sqlite3.Connection = Depends(admin_conn)):
    keys = body.get("keys") or [k for k in DEFAULT_SETTINGS if k not in SECRET_SETTINGS]
    with tx(conn):
        for k in keys:
            if k in DEFAULT_SETTINGS and k not in SECRET_SETTINGS:
                set_setting(conn, k, DEFAULT_SETTINGS[k])
    request.app.state.player.call("settings-changed")
    return get_settings(conn)


# --- schedule editing -------------------------------------------------------------------------------

@router.post("/schedule/build")
def schedule_build(request: Request, body: dict[str, Any] = Body(default={})):
    cfg = request.app.state.cfg
    jobs = request.app.state.jobs
    try:
        start = parse_day(str(body["start_day"])) if body.get("start_day") else None
        days = max(1, min(int(body["days"]), 31)) if body.get("days") else None
        channels = [int(c) for c in body.get("channels", [])] or None
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "start_day must be YYYY-MM-DD; days and channels must be numbers") from exc
    force = bool(body.get("force", False))

    def run(job):
        conn = dbm.connect(cfg.db_path)
        try:
            result = build_horizon(conn, start_day=start, days=days, force=force, channel_numbers=channels,
                                   progress=lambda m: jobs.progress(job, m))
            job.notes.extend(result.get("notes", []))
            _schedule_changed(request)
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


def _media_with_show(conn: sqlite3.Connection, media_id: int) -> sqlite3.Row:
    """A media row plus its series title, for placing it in the schedule; 404 if unusable."""
    media = conn.execute("SELECT m.*, s.title AS show_title FROM media m LEFT JOIN shows s ON s.id = m.show_id WHERE m.id = ?",
                         (media_id,)).fetchone()
    if not media or not media["duration"]:
        raise HTTPException(404, "media not found or has no duration")
    return media


def _schedule_changed(request: Request) -> None:
    """Tell the browser tabs and the player (which caches the slot on air) to re-read."""
    request.app.state.bus.publish_threadsafe("schedule", {"changed": True})
    request.app.state.player.call("schedule-changed")


def _rebuild(request: Request, conn: sqlite3.Connection, channel_id: int, from_ts: int) -> dict[str, Any]:
    result = rebuild_from(conn, channel_id, from_ts)
    _schedule_changed(request)
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
    media = _media_with_show(conn, int(body.get("media_id", 0)))
    title, subtitle = slot_titles(dict(media), media["show_title"])
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
    media = _media_with_show(conn, media_id)
    day = broadcast_day_for(start_ts, all_settings(conn), tz_of(conn)).isoformat()
    title, subtitle = slot_titles(dict(media), media["show_title"])
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


# --- system ---------------------------------------------------------------------------------------------

def _cmd(args: list[str], timeout: float = 3) -> str:
    """stdout of a command, or '' when it is missing or fails (this page must render anywhere)."""
    return run_cmd(args, timeout)[1]


def _live_checks(request: Request, conn: sqlite3.Connection) -> dict[str, bool | None]:
    """Whether each application's processes answer, whatever systemd says."""
    player = request.app.state.player.call("state", timeout=1)
    status, tool = tool_client.request(tool_client.base_url(all_settings(conn)), "GET", "status", timeout=3)
    tool_up = status == 200 and isinstance(tool, dict) and ("api_version" in tool or "tool" in tool)
    return {
        "pitv-web.service": True,
        "pitv-player.service": bool(player.get("ok") and player.get("online", True)),
        "pitv-content-api.service": tool_up,
        CONTENT_RUN: bool(tool.get("active_job")) if tool_up else None,
    }


@router.get("/system")
def system_info(request: Request, conn: sqlite3.Connection = Depends(admin_conn)):
    cfg = request.app.state.cfg
    time_info = {}
    for line in _cmd(["timedatectl", "show"]).splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            time_info[k] = v
    mounts = []
    for r in conn.execute("SELECT * FROM sources WHERE location = 'nas' ORDER BY id"):
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
        "services": services(_live_checks(request, conn)), "mounts": mounts,
        "data": {"path": str(cfg.data_dir), "db": str(cfg.db_path),
                 "db_size": cfg.db_path.stat().st_size if cfg.db_path.exists() else 0,
                 "free": data_usage.free if data_usage else None, "total": data_usage.total if data_usage else None},
        "jobs": request.app.state.jobs.recent(10),
        "cache_dir": get_setting(conn, "cache_dir") or "",
    }


@router.post("/system/service/{name}/{action}")
def service_action(name: str, action: str):
    if action not in SERVICE_ACTIONS.get(name, ()):
        raise HTTPException(400, "unsupported service or action")
    rc, _, err = run_cmd(["sudo", "-n", "systemctl", action, name], timeout=30)
    if rc != 0:
        raise HTTPException(500, err or "systemctl failed")
    return {"ok": True}


@router.get("/runs")
def runs(conn: sqlite3.Connection = Depends(admin_conn), limit: int = 20):
    rows = conn.execute("SELECT * FROM run_log ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
    return rows_to_dicts(rows)


@router.get("/history")
def history(conn: sqlite3.Connection = Depends(admin_conn), limit: int = 100):
    rows = conn.execute("SELECT h.*, c.number AS channel_number, c.name AS channel_name FROM history h"
                        " LEFT JOIN channels c ON c.id = h.channel_id ORDER BY h.started_at DESC LIMIT ?",
                        (max(1, min(limit, 1000)),)).fetchall()
    return [dict(r) for r in rows]


@router.get("/jobs")
def jobs(request: Request):
    return request.app.state.jobs.recent(20)


@router.get("/export")
def export_overrides(conn: sqlite3.Connection = Depends(admin_conn)):
    """Everything an admin has changed, for backup: settings, channels, sources, overrides."""
    shows = [{"uid": r["path"], "title": r["title"], "overrides": json.loads(r["overrides"]), "home_channel_id": r["home_channel_id"],
              "mode": r["mode"], "anchor_time": r["anchor_time"], "anchor_days": r["anchor_days"], "rest_weeks": r["rest_weeks"], "excluded": r["excluded"]}
             for r in conn.execute("SELECT * FROM shows WHERE overrides != '{}' OR excluded = 1 OR mode != 'auto'")]
    media = [{"uid": r["uid"], "title": r["title"], "overrides": json.loads(r["overrides"]), "excluded": r["excluded"]}
             for r in conn.execute("SELECT * FROM media WHERE overrides != '{}' OR excluded = 1")]
    return {"exported_at": now_ts(), "settings": {k: v for k, v in all_settings(conn).items() if k not in SECRET_SETTINGS},
            "channels": rows_to_dicts(conn.execute("SELECT * FROM channels")), "sources": rows_to_dicts(conn.execute("SELECT * FROM sources")),
            "shows": shows, "media": media}


# --- logs ---------------------------------------------------------------------------------------------

LOG_NAMES = ("player", "web", "catalogue", "schedule", "install")
INSTALL_LOG = Path("/work/install/install.log")   # written by the SD-card installer and first boot


def _log_path(cfg, name: str) -> Path:
    if name == "install":
        return INSTALL_LOG if INSTALL_LOG.exists() else log_dir(cfg) / "install.log"
    return log_dir(cfg) / f"{name}.log"


@router.get("/logs")
def list_logs(request: Request):
    out = []
    for name in LOG_NAMES:
        p = _log_path(request.app.state.cfg, name)
        out.append({"name": name, "path": str(p), "size": p.stat().st_size if p.exists() else 0,
                    "modified": int(p.stat().st_mtime) if p.exists() else None})
    return out


@router.get("/logs/{name}")
def read_log(name: str, request: Request, lines: int = 300, q: str = "", level: str = ""):
    """Tail of a log file, newest last. `q` filters by substring, `level` by minimum level."""
    if name not in LOG_NAMES:
        raise HTTPException(404, "unknown log")
    p = _log_path(request.app.state.cfg, name)
    entries = tail(p, max(10, min(lines, 5000)), q, level.upper())
    return {"name": name, "lines": entries, "exists": p.exists()}


@router.get("/logs/journal/{unit}")
def read_journal(unit: str, lines: int = 300):
    """systemd journal for a PiTV unit (Pi only; empty elsewhere)."""
    if unit not in ("pitv-player", "pitv-web"):
        raise HTTPException(404, "unknown unit")
    rc, out, err = run_cmd(["journalctl", "-u", unit, "-n", str(min(lines, 2000)), "--no-pager", "-o", "short-iso"], timeout=15)
    return {"unit": unit, "text": out if rc == 0 else "", "error": err[:300]}


# --- line-ups ------------------------------------------------------------------------------------------

@router.get("/library/genres")
def library_genres(conn: sqlite3.Connection = Depends(admin_conn)):
    return lineup_mod.genre_facets(conn)


@router.get("/lineup")
def lineup_list(conn: sqlite3.Connection = Depends(admin_conn), channel_id: int | None = None):
    return lineup_mod.entries(conn, channel_id=channel_id)


@router.get("/lineup/options")
def lineup_options(conn: sqlite3.Connection = Depends(admin_conn), q: str = "", limit: int = 50):
    """Dropdown choices: library series and films plus pitv_content's catalogue when it is up."""
    from .content import tool_catalogue
    out = lineup_mod.options(conn, q, limit)
    for c in tool_catalogue(conn):
        if q and q.lower() not in str(c.get("name", "")).lower():
            continue
        if c.get("kind") in ("music", "adverts"):
            continue
        out.append({"type": "show", "title": c["name"], "year": c.get("first_year"), "on_disk": bool(c.get("count_on_disk")),
                    "catalogue": True, "episodes_known": c.get("episodes_known"), "years": c.get("years")})
    return out


@router.post("/lineup")
def lineup_add(body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    try:
        return lineup_mod.add(conn, int(body["channel_id"]), show_id=body.get("show_id"), media_id=body.get("media_id"),
                              title=body.get("title"), year=body.get("year"), kind=body.get("kind"), genres=body.get("genres"),
                              transient=body.get("transient"), episode_minutes=body.get("episode_minutes"),
                              source="catalogue" if body.get("catalogue") else "manual")
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/lineup/{lid}")
def lineup_update(lid: int, body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    try:
        return lineup_mod.update(conn, lid, body)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/lineup/{lid}")
def lineup_delete(lid: int, conn: sqlite3.Connection = Depends(admin_conn)):
    lineup_mod.remove(conn, lid)
    return {"ok": True}


@router.post("/lineup/generate")
def lineup_generate(body: dict[str, Any] = Body(default={}), conn: sqlite3.Connection = Depends(admin_conn)):
    return lineup_mod.generate(conn, rebalance=bool(body.get("rebalance")))


@router.get("/lineup/export")
def lineup_export(conn: sqlite3.Connection = Depends(admin_conn)):
    return lineup_mod.export(conn)


@router.post("/lineup/import")
def lineup_import(body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    if not isinstance(body, dict) or "channels" not in body:
        raise HTTPException(400, "expected a line-up document with a channels list")
    return lineup_mod.import_doc(conn, body)
