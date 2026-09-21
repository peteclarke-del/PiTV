"""Admin API: sources, library, channels, weighting/settings, schedule editing, system."""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ... import __version__, catalogue, display, settings_schema, tool_client, youtube
from ... import db as dbm
from ... import genres as genre_rules
from ... import lineup as lineup_mod
from ... import wanted as wanted_mod
from ...content import push_screen
from ...db import (
    CHANNEL_CONTENT,
    DEFAULT_SETTINGS,
    LIVE,
    all_settings,
    get_setting,
    now_ts,
    row_to_dict,
    rows_to_dicts,
    set_setting,
    tx,
)
from ...guide import SLOT_QUERY
from ...hostinfo import host_info
from ...logsetup import log_dir
from ...scheduler import bands as band_rules
from ...scheduler.horizon import build_horizon, fresh_rebuild_horizon, rebuild_from
from ...scheduler.rules import broadcast_day_for, normalise_cert, parse_pattern, tz_of
from ...scheduler.slots import parse_day, slot_titles
from ..keeper import PLAYER_UNIT, start_player
from .content import tool_catalogue
from .deps import (
    admin_conn,
    admin_json,
    log_tail,
    media_public,
    optional_int,
    optional_text,
    run_cmd,
    show_public,
    slot_public,
    tool_url,
)
from .services import (
    CONTENT_RUN,
    CONTENT_TIMER,
    DEV_UNITS,
    SERVICE_ACTIONS,
    services,
    systemd_state,
)
from .settings_rules import HHMM, SECRET_SETTINGS, SettingError, check_setting

log = logging.getLogger("pitv.web")
router = APIRouter(prefix="/api", dependencies=[Depends(admin_conn)])

SHOW_OVERRIDE_FIELDS = {"title", "year", "certificate", "genres", "plot", "kids", "programme_type"}
SHOW_DIRECT_FIELDS = {"home_channel_id", "mode", "anchor_time", "anchor_days", "rest_weeks", "excluded", "category"}
SHOW_CATEGORIES = genre_rules.SCHEDULING_CLASSES
MEDIA_OVERRIDE_FIELDS = {"title", "year", "certificate", "genres", "plot", "season", "episode", "artist", "programme_type"}
MEDIA_DIRECT_FIELDS = {"excluded", "concert", "family_safe", "home_channel_id"}
CHANNEL_FIELDS = {"number", "name", "short_name", "colour", "enabled", "ads_enabled", "ads_per_break",
                  "pattern", "era_weights", "genre_weights", "kind_weights", "daypart_profile",
                  "overnight_replay_from", "idents_enabled", "description", "content", "family_safe_ads",
                  "allowed_genres", "excluded_genres", "nas_only", "kids_any_time", "decades", "networks", "bands",
                  "band_item_repeat_hours", "band_feature_repeat_days",
                  "short_episode_minutes", "short_episode_run_minutes", "series_cadence_days", "also_carries", "fetch_kind",
                  "band_item_max_minutes", "strict_matching"}
JSON_CHANNEL_FIELDS = {"era_weights", "genre_weights", "kind_weights", "daypart_profile", "allowed_genres",
                       "excluded_genres", "decades", "networks"}
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

SOURCE_CATEGORIES = genre_rules.SCHEDULING_CLASSES


def _mirror_sources(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The sources as last seen in pitv_content's index, for when its API is down."""
    rows = conn.execute("SELECT s.*, (SELECT COUNT(*) FROM media m WHERE m.source_id = s.id AND m.missing = 0) AS items"
                        " FROM sources s ORDER BY s.location, s.name")
    return [{"id": r["uid"], "name": r["name"], "type": r["type"], "category": r["category"], "root": r["path"],
             "mount": r["mount"], "remote": r["remote"], "location": r["location"], "enabled": bool(r["enabled"]),
             "health": {"mounted": Path(r["mount"] or r["path"]).is_dir(), "items": r["items"],
                        "last_indexed_ts": r["last_indexed_at"]}}
            for r in rows]


@router.get("/sources")
def list_sources(conn: sqlite3.Connection = Depends(admin_conn)):
    """pitv_content's sources. PiTV does not read them; they are shown and edited here because
    pitv_content has no interface of its own."""
    status, payload = tool_client.request(tool_url(conn), "GET", "sources", timeout=10)
    if status == 200 and isinstance(payload, (list, dict)):
        items = payload if isinstance(payload, list) else payload.get("sources", [])
        mounts = {r["uid"]: r["mount"] for r in conn.execute("SELECT uid, mount FROM sources WHERE uid IS NOT NULL")}
        for item in items:      # the mount is PiTV's, so pitv_content's answer does not carry it
            if isinstance(item, dict):
                item["mount"] = mounts.get(item.get("id")) or None
        return {"owner": "pitv_content", "offline": False, "sources": items}
    return {"owner": "pitv_content", "offline": True, "sources": _mirror_sources(conn),
            "error": payload.get("error") if isinstance(payload, dict) else f"HTTP {status}"}


@router.put("/sources")
def put_source(body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    """Add, edit or delete one of pitv_content's sources through its API."""
    if not isinstance(body.get("id"), str) or not body["id"].strip():
        raise HTTPException(400, "id required")
    if not body.get("delete"):
        if "type" in body and body["type"] not in catalogue.SOURCE_TYPES:
            raise HTTPException(400, "type must be tv, movie, advert, ident or music")
        if "category" in body and (body["category"] or "general") not in SOURCE_CATEGORIES:
            raise HTTPException(400, "category must be general or sport")
        if body.get("root"):
            body["root"] = str(allowed_dir(str(body["root"]), browse_roots(all_settings(conn))))
    # Where this machine mounts the share is PiTV's own business, not pitv_content's.
    if "mount" in body:
        mount = str(body.pop("mount") or "").strip()
        if mount:
            mount = str(allowed_dir(mount, browse_roots(all_settings(conn))))
        with tx(conn):
            conn.execute("UPDATE sources SET mount = ? WHERE uid = ?", (mount or None, body["id"]))
    # Credentials (username, password, workgroup) pass straight through: pitv_content keeps them
    # and never returns the password, and PiTV neither stores nor logs them.
    return _relay_source(conn, "PUT", "sources", body, timeout=15)


@router.post("/sources/test")
def test_source(body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    """Ask pitv_content to try a share with the given (or saved) credentials, without saving."""
    return _relay_source(conn, "POST", "sources/test", body, timeout=30)


def _relay_source(conn: sqlite3.Connection, method: str, path: str, body: dict[str, Any], timeout: float) -> Any:
    status, payload = tool_client.request(tool_url(conn), method, path, body=body, timeout=timeout)
    if isinstance(payload, dict) and payload.get("offline"):
        raise HTTPException(503, "pitv_content is not running; sources can only be changed through it")
    if status >= 400:
        # Pass pitv_content's field errors through in the contract's shape.
        return JSONResponse(status_code=status, content=payload if isinstance(payload, dict) else {"error": "rejected"})
    return payload


# --- catalogue (imported from pitv_content's library index) ----------------------------------------

def catalogue_job(request: Request, reindex: bool, label: str, doc: dict[str, Any] | None = None) -> dict[str, Any]:
    """Import the index as a background job (its own connection; progress over SSE)."""
    state = request.app.state

    def run(job):
        conn = dbm.connect(state.cfg.db_path)
        try:
            state.jobs.progress(job, "asking pitv_content to re-index" if reindex else "importing the library index")
            result = catalogue.import_and_place(conn, doc, "uploaded file") if doc is not None \
                else catalogue.refresh(conn, reindex=reindex)
            if result.get("status") == "error":
                raise RuntimeError(result["summary"])   # the job reads "failed", with the reason
            enrichment = catalogue.enrich_missing_metadata(
                conn, limit=50, progress=lambda m, d, t: state.jobs.progress(job, m, d, t))
            state.bus.publish_threadsafe("library", {"changed": True})
            return {"status": result.get("status", "ok"),
                    "summary": f"{result['summary']} {enrichment['summary']}", "enrichment": enrichment}
        finally:
            conn.close()
    return state.jobs.submit("catalogue", label, run).public()


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


@router.post("/catalogue/enrich")
def catalogue_enrich(request: Request, body: dict[str, Any] = Body(default={})):
    """Check unrated series/films online without replacing indexed data or admin overrides."""
    state = request.app.state
    try:
        limit = max(1, min(int(body.get("limit", 500)), 2000))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "limit must be a whole number") from exc

    def run(job):
        conn = dbm.connect(state.cfg.db_path)
        try:
            result = catalogue.enrich_missing_metadata(
                conn, limit=limit, force=bool(body.get("force", True)),
                progress=lambda m, d, t: state.jobs.progress(job, m, d, t))
            job.notes.extend(result.get("notes", []))
            state.bus.publish_threadsafe("library", {"changed": True})
            return result
        finally:
            conn.close()
    return state.jobs.submit("catalogue", "Fill in missing years, genres and certificates online", run).public()


@router.post("/catalogue/import")
def catalogue_import(request: Request, body: dict[str, Any] = Depends(admin_json)):
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
        dirs = sorted(d.name for d in p.iterdir() if not d.name.startswith(".") and d.is_dir())
    except OSError:
        dirs = []   # unreadable, or a share that dropped while listing: show it empty
    parent = p.parent
    up = str(parent) if any(parent == r or parent.is_relative_to(r) for r in roots) else "/"
    return {"path": str(p), "parent": up, "dirs": dirs}


# --- library -------------------------------------------------------------------------------------

@router.get("/library/summary")
def library_summary(conn: sqlite3.Connection = Depends(admin_conn)):
    kinds = {r["kind"]: r["n"] for r in conn.execute("SELECT kind, COUNT(*) AS n FROM media WHERE missing = 0 GROUP BY kind")}
    shows = conn.execute("SELECT COUNT(*) FROM shows WHERE missing = 0").fetchone()[0]
    # One pass over media rather than a query per figure.
    m = conn.execute(
        "SELECT SUM(kind IN ('episode','movie')) AS programmes,"
        " SUM(CASE WHEN kind IN ('episode','movie') THEN hwdec END) AS hwdec,"
        " SUM(CASE WHEN kind IN ('episode','movie') THEN duration END) / 3600.0 AS hours,"
        " SUM(attention IS NOT NULL) AS attention, SUM(kind = 'music' AND concert = 1) AS concerts,"
        " SUM(cache_path IS NOT NULL OR origin != 'nas') AS cached, SUM(origin = 'nas' AND cache_path IS NULL) AS nas_only,"
        " SUM(origin = 'online') AS online FROM media WHERE missing = 0").fetchone()
    return {"kinds": kinds, "shows": shows, "hwdec": m["hwdec"] or 0, "programmes": m["programmes"] or 0,
            "attention": m["attention"] or 0, "hours": round(m["hours"] or 0, 1), "concerts": m["concerts"] or 0,
            "cached": m["cached"] or 0, "nas_only": m["nas_only"] or 0, "online": m["online"] or 0,
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


def _override_value(key: str, value: Any) -> Any:
    """An admin override in the type of the indexed column it stands in for; 400 otherwise."""
    if key in ("year", "season", "episode"):
        return optional_int(value, key)
    if key == "genres":
        if not isinstance(value, list) or not all(isinstance(g, str) for g in value):
            raise HTTPException(400, "genres must be a list of names")
        return genre_rules.canonical_all(value)
    if key == "kids":
        return int(bool(value))
    if key == "programme_type":
        chosen = str(value).strip().casefold()
        if chosen not in genre_rules.PROGRAMME_TYPES:
            raise HTTPException(400, f"programme_type must be one of {', '.join(genre_rules.PROGRAMME_TYPES)}")
        return chosen
    if key == "certificate":
        cert = normalise_cert(optional_text(value, key))
        if cert is None:
            raise HTTPException(400, "certificate must be U, PG, 12, 12A, 15 or 18")
        return cert
    return optional_text(value, key, limit=4000)


def _merge_overrides(row: sqlite3.Row, body: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    """The row's overrides with the body's edits applied. An empty value, or one equal to the
    indexed value, drops the override so later index imports show through again."""
    overrides = json.loads(row["overrides"] or "{}")
    indexed = row_to_dict(row) or {}
    for k in fields & body.keys():
        v = None if body[k] in (None, "") else _override_value(k, body[k])
        if v in (None, "", []) or v == indexed.get(k):
            overrides.pop(k, None)
        else:
            overrides[k] = v
    return overrides


def _retyped(row: sqlite3.Row, overrides: dict[str, Any]) -> bool:
    """Whether this edit changed what the owner says the title is, with its channel left alone."""
    return overrides.get("programme_type") != json.loads(row["overrides"] or "{}").get("programme_type")


def _update_row(conn: sqlite3.Connection, table: str, row_id: int, fields: dict[str, Any]) -> None:
    """UPDATE one row by id in its own transaction. Column names come from this module's
    allowlists; dbm.update_row checks them as identifiers and binds every value."""
    if fields:
        with tx(conn):
            dbm.update_row(conn, table, row_id, fields)


@router.put("/shows/{sid}")
def update_show(sid: int, body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    row = conn.execute("SELECT * FROM shows WHERE id = ?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404, "show not found")
    direct = {k: body[k] for k in SHOW_DIRECT_FIELDS & body.keys()}
    # The home channel is a line-up entry; shows.home_channel_id is derived from the line-up.
    home = optional_int(direct.pop("home_channel_id", None), "home_channel_id")
    if "mode" in direct and direct["mode"] not in ("auto", "strip", "weekly"):
        raise HTTPException(400, "mode must be auto, strip or weekly")
    if "category" in direct and direct["category"] not in SHOW_CATEGORIES:
        raise HTTPException(400, "category must be general or sport")
    if direct.get("anchor_time") is not None and not HHMM.match(str(direct["anchor_time"])):
        raise HTTPException(400, "anchor_time must be HH:MM")
    if direct.get("anchor_days") is not None:
        try:
            direct["anchor_days"] = json.dumps(sorted({int(d) % 7 for d in direct["anchor_days"]}))
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "anchor_days must be a list of weekday numbers") from exc
    if "rest_weeks" in direct:
        direct["rest_weeks"] = max(0, optional_int(direct["rest_weeks"], "rest_weeks") or 0)
    if "excluded" in direct:
        direct["excluded"] = int(bool(direct["excluded"]))
    overrides = _merge_overrides(row, body, SHOW_OVERRIDE_FIELDS)
    direct["overrides"] = json.dumps(overrides)
    _update_row(conn, "shows", sid, direct)
    if _retyped(row, overrides) and home in (None, row["home_channel_id"]):
        lineup_mod.place_again(conn, show_id=sid)     # what it is decides where it belongs
    elif home:
        lineup_mod.add(conn, home, show_id=sid)
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
    # The admin lists a whole kind and filters, sorts and pages it in the browser.
    rows = conn.execute(sql, (*params, max(1, min(limit, 20000)), max(0, offset))).fetchall()
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
    direct = {k: body[k] for k in MEDIA_DIRECT_FIELDS & body.keys()}
    home = optional_int(direct.pop("home_channel_id", None), "home_channel_id")
    for k in ("excluded", "concert", "family_safe"):
        if k in direct:
            direct[k] = int(bool(direct[k]))
    if row["kind"] == "ident" and "home_channel_id" in body:
        if home is not None:
            _require_channel(conn, home)
        direct["home_channel_id"] = home          # an ident's channel is set here, not by a line-up
    overrides = _merge_overrides(row, body, MEDIA_OVERRIDE_FIELDS)
    direct["overrides"] = json.dumps(overrides)
    if "year" in overrides or "certificate" in overrides:
        # Clear attention flags the override resolves.
        att = [a for a in (row["attention"] or "").split("; ") if a and not (
            ("year" in overrides and a.startswith("No year")) or
            ("certificate" in overrides and a.startswith("No certificate")))]
        direct["attention"] = "; ".join(att) or None
    _update_row(conn, "media", mid, direct)
    if row["kind"] == "movie" and _retyped(row, overrides) and home in (None, row["home_channel_id"]):
        lineup_mod.place_again(conn, media_id=mid)
    elif home and row["kind"] == "movie":
        lineup_mod.add(conn, home, media_id=mid)
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
        shows = conn.execute(f"SELECT * FROM shows WHERE {LIVE} AND title LIKE ? ORDER BY title LIMIT ?", (like, limit)).fetchall()
        eps_by_show: dict[int, list[dict[str, Any]]] = {r["id"]: [] for r in shows}
        if shows:
            marks = ",".join("?" for _ in shows)
            for e in conn.execute(f"SELECT id, show_id, season, episode, title, duration FROM media WHERE show_id IN ({marks})"
                                  f" AND {LIVE} ORDER BY show_id, COALESCE(season,999), COALESCE(episode,999)",
                                  [r["id"] for r in shows]):
                lst = eps_by_show[e["show_id"]]
                if len(lst) < 200:
                    lst.append({"id": e["id"], "season": e["season"], "episode": e["episode"], "title": e["title"], "duration": e["duration"]})
        for r in shows:
            out.append({"type": "show", "id": r["id"], "title": r["title"], "year": r["year"], "episodes": eps_by_show[r["id"]]})
    if kind in ("programme", "movie"):
        for r in conn.execute(f"SELECT * FROM media WHERE kind = 'movie' AND {LIVE} AND title LIKE ? ORDER BY title LIMIT ?", (like, limit)):
            out.append({"type": "movie", "id": r["id"], "title": r["title"], "year": r["year"], "duration": r["duration"], "certificate": r["certificate"]})
    if kind in ("programme", "music"):
        for r in conn.execute(f"SELECT * FROM media WHERE kind = 'music' AND {LIVE} AND title LIKE ? ORDER BY title LIMIT ?", (like, limit)):
            out.append({"type": "music", "id": r["id"], "title": r["title"], "year": r["year"], "duration": r["duration"], "concert": r["concert"]})
    return out


# --- channels ------------------------------------------------------------------------------------

def _channel(conn: sqlite3.Connection, cid: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "channel not found")
    d = row_to_dict(row)
    d["show_count"] = conn.execute("SELECT COUNT(*) FROM shows WHERE home_channel_id = ? AND missing = 0", (cid,)).fetchone()[0]
    d["pattern_tokens"] = parse_pattern(d["pattern"]) if (d["pattern"] or "").strip() else []
    # A line-up belongs to any channel whose pattern schedules programmes.  Content is only a
    # descriptive label, so using it here would hide valid line-ups on new/specialist channels.
    d["has_lineup"] = lineup_mod.carries_programmes(d)
    d["bands"] = band_rules.export(conn, cid)
    # Every ident in the library with whose it is, so the channel can be pointed at its own:
    # this channel's, another's by name, or generic (no channel: any channel may show it).
    d["idents"] = [{"id": r["id"], "title": r["title"], "seconds": round(r["duration"] or 0),
                    "channel_id": r["home_channel_id"], "channel_name": r["channel_name"]}
                   for r in conn.execute("SELECT m.id, m.title, m.duration, m.home_channel_id, c.name AS channel_name FROM media m"
                                         " LEFT JOIN channels c ON c.id = m.home_channel_id"
                                         " WHERE m.kind = 'ident' AND m.missing = 0 AND m.excluded = 0 ORDER BY m.title")]
    return d


@router.get("/channels")
def list_channels(conn: sqlite3.Connection = Depends(admin_conn)):
    return [_channel(conn, r["id"]) for r in conn.execute("SELECT id FROM channels ORDER BY number")]


def _clean_bands(body: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The channel's bands, checked, or None when the request does not mention them."""
    if "bands" not in body:
        return None
    if not isinstance(body["bands"], list):
        raise HTTPException(400, "bands must be a list")
    try:
        return [band_rules.clean(b) for b in body["bands"]]
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _clean_channel_fields(body: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for k, v in body.items():
        if k in ("bands", "ident_ids"):
            continue          # written apart: bands by _clean_bands, idents by _point_idents
        if k not in CHANNEL_FIELDS:
            continue
        if k in JSON_CHANNEL_FIELDS:
            if v not in (None, "", {}, []) and not isinstance(v, (dict, list)):
                raise HTTPException(400, f"{k} must be an object or list")
            if k in ("allowed_genres", "excluded_genres") and v not in (None, "", []):
                v = genre_rules.canonical_all(v)
            elif k == "genre_weights" and isinstance(v, dict):
                v = {name: weight for raw, weight in v.items()
                     if (name := genre_rules.canonical(raw)) is not None}
            fields[k] = json.dumps(v) if v not in (None, "", {}, []) else None
        elif k in ("enabled", "ads_enabled", "idents_enabled", "family_safe_ads", "kids_any_time",
                   "strict_matching"):
            fields[k] = int(bool(v))
        elif k in ("band_item_repeat_hours", "band_feature_repeat_days", "band_item_max_minutes",
                   "short_episode_minutes", "short_episode_run_minutes"):
            fields[k] = optional_int(v, k)      # empty follows the global setting
            if fields[k] is not None and not 0 <= fields[k] <= 8760:
                raise HTTPException(400, f"{k} out of range")
        elif k == "also_carries":
            if not isinstance(v, list) or any(t not in genre_rules.PROGRAMME_TYPES for t in v):
                raise HTTPException(400, f"also_carries must be a list of {', '.join(genre_rules.PROGRAMME_TYPES)}")
            fields[k] = json.dumps(sorted(set(v)))      # an empty list is the owner's word: borrow nothing
        elif k == "series_cadence_days":
            fields[k] = optional_int(v, k)      # empty follows the global setting
            if fields[k] is not None and not 1 <= fields[k] <= 28:
                raise HTTPException(400, f"{k} must be between 1 and 28")
        elif k in ("number", "ads_per_break"):
            try:
                fields[k] = int(v)
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, f"{k} must be a number") from exc
            if fields[k] < 1 or (k == "number" and fields[k] > 999) or (k == "ads_per_break" and fields[k] > 10):
                raise HTTPException(400, f"{k} out of range")
        elif k == "pattern":
            # Empty means the channel places no programmes of its own: its day is its bands.
            fields[k] = ", ".join(parse_pattern(str(v))) if str(v).strip() else ""
        elif k == "content":
            if v not in CHANNEL_CONTENT:
                raise HTTPException(400, f"content must be one of {', '.join(CHANNEL_CONTENT)}")
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
            if not isinstance(v, str) or not HHMM.match(v):
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
    new_bands = _clean_bands(body)
    try:
        with tx(conn):
            cid = dbm.insert_row(conn, "channels", fields)
            if new_bands is not None:
                band_rules.save(conn, cid, new_bands, now_ts())
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, f"channel number {fields['number']} is already used") from exc
    return _channel(conn, cid)


@router.put("/channels/{cid}")
def update_channel(cid: int, body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    _channel(conn, cid)
    fields = _clean_channel_fields(body)
    new_bands = _clean_bands(body)
    try:
        with tx(conn):
            dbm.update_row(conn, "channels", cid, fields)
            if new_bands is not None:
                band_rules.save(conn, cid, new_bands, now_ts())
            _point_idents(conn, cid, body)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, f"channel number {fields.get('number')} is already used") from exc
    return _channel(conn, cid)


def _point_idents(conn: sqlite3.Connection, cid: int, body: dict[str, Any]) -> None:
    """`ident_ids` is the whole answer to "which idents are this channel's": those listed become
    its own, and any it had that are not listed go back to being generic."""
    if "ident_ids" not in body:
        return
    ids = body["ident_ids"]
    if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise HTTPException(400, "ident_ids must be a list of ident ids")
    conn.execute("UPDATE media SET home_channel_id = NULL WHERE kind = 'ident' AND home_channel_id = ?", (cid,))
    conn.executemany("UPDATE media SET home_channel_id = ? WHERE kind = 'ident' AND id = ?", [(cid, i) for i in ids])


@router.delete("/channels/{cid}")
def delete_channel(cid: int, conn: sqlite3.Connection = Depends(admin_conn)):
    _channel(conn, cid)
    with tx(conn):
        conn.execute("UPDATE shows SET home_channel_id = NULL WHERE home_channel_id = ?", (cid,))
        conn.execute("DELETE FROM channels WHERE id = ?", (cid,))
    return {"ok": True}


# --- settings -------------------------------------------------------------------------------------

def _public_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    """Every setting but the secrets, plus the quality the screen profile implies (read-only)."""
    settings = {k: v for k, v in all_settings(conn).items() if k not in SECRET_SETTINGS}
    return {**settings, "content_profile": display.content_profile(settings)}


@router.get("/settings")
def get_settings(conn: sqlite3.Connection = Depends(admin_conn)):
    return _public_settings(conn)


@router.get("/settings/schema")
def get_settings_schema(conn: sqlite3.Connection = Depends(admin_conn)):
    """The settings panes: each field's pane, level, label, help, range, default and value."""
    return settings_schema.schema(_public_settings(conn), DEFAULT_SETTINGS)


@router.put("/settings")
def put_settings(request: Request, body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    try:
        clean = {k: check_setting(k, v) for k, v in body.items()}
    except SettingError as exc:
        raise HTTPException(400, str(exc)) from exc
    if "display_profile" in clean:
        # A new screen brings its shape and margins; values sent alongside it win.
        clean = {**display.implied_settings(clean["display_profile"]), **clean}
    with tx(conn):
        for k, v in clean.items():
            set_setting(conn, k, v)
    request.app.state.player.call("settings-changed")
    # Best effort and at once, so the admin has one screen to choose. The player's maintenance
    # pass repeats it until pitv_content has taken it.
    if "display_profile" in clean and (failed := push_screen(all_settings(conn))):
        log.warning("pitv_content did not take the screen %s: %s", clean["display_profile"], failed)
    return _public_settings(conn)


@router.post("/settings/reset")
def reset_settings(request: Request, body: dict[str, Any] = Body(default={}), conn: sqlite3.Connection = Depends(admin_conn)):
    """Restore defaults: the named `keys`, or every setting when none are named."""
    keys = body.get("keys") or list(DEFAULT_SETTINGS)
    if not isinstance(keys, list):
        raise HTTPException(400, "keys must be a list of setting names")
    with tx(conn):
        for k in keys:
            if isinstance(k, str) and k in DEFAULT_SETTINGS and k not in SECRET_SETTINGS:
                set_setting(conn, k, DEFAULT_SETTINGS[k])
    request.app.state.player.call("settings-changed")
    return _public_settings(conn)


# --- schedule editing -------------------------------------------------------------------------------

@router.post("/schedule/build")
def schedule_build(request: Request, body: dict[str, Any] = Body(default={})):
    app = request.app
    try:
        start = parse_day(str(body["start_day"])) if body.get("start_day") else None
        days = max(1, min(int(body["days"]), 31)) if body.get("days") else None
        channels = [int(c) for c in body.get("channels", [])] or None
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "start_day must be YYYY-MM-DD; days and channels must be numbers") from exc
    force = bool(body.get("force", False))

    def run(job):
        conn = dbm.connect(app.state.cfg.db_path)
        try:
            result = build_horizon(conn, start_day=start, days=days, force=force, channel_numbers=channels,
                                   progress=lambda m: app.state.jobs.progress(job, m))
            job.notes.extend(result.get("notes", []))
            _schedule_changed(app)
            return result
        finally:
            conn.close()
    return app.state.jobs.submit("schedule", "Build schedule" + (" (force)" if force else ""), run).public()


@router.post("/schedule/fresh-rebuild")
def schedule_fresh_rebuild(request: Request, body: dict[str, Any] = Body(default={})):
    """Drop all derived schedule state and rebuild the configured horizon from its inputs.

    `clear_material` also has pitv_content throw away everything it has fetched and copied
    (contract section 7), which starts the library again as well as the schedule. It defaults
    to off: a fresh schedule is usually wanted precisely so that the material already gathered
    can be arranged again, and a night's fetching is expensive to replace."""
    app = request.app
    clear_material = bool(body.get("clear_material"))

    def run(job):
        conn = dbm.connect(app.state.cfg.db_path)
        try:
            reset: Any = "kept"
            if clear_material:
                # pitv_content stops what it is doing and removes its partial work, its acquired
                # and cache files, reports, indexes and fingerprints. Source media on the NAS,
                # its sources, providers and settings stay.
                app.state.jobs.progress(job, "stopping pitv_content and clearing what it fetched")
                status, reset = tool_client.request(tool_url(conn), "POST", "reset", body={}, timeout=30)
                if status != 200 or not isinstance(reset, dict) or not reset.get("ok"):
                    # Nothing of PiTV's is cleared: the two sides must agree about what exists,
                    # and pitv_content names what it could not remove.
                    if isinstance(reset, dict):
                        detail = reset.get("error") or ("could not remove " + ", ".join(map(str, reset.get("failed") or []))
                                                        if reset.get("failed") else "not ok")
                    else:
                        detail = f"HTTP {status}"
                    raise RuntimeError(f"pitv_content reset failed: {detail}")
            app.state.jobs.progress(job, "asking pitv_content for a fresh index")
            imported = catalogue.refresh(conn, reindex=True)
            if imported.get("status") == "error":
                raise RuntimeError(imported["summary"])
            result = fresh_rebuild_horizon(
                conn, progress=lambda m: app.state.jobs.progress(job, m))
            # Declare every band's shortfall now. pitv_content's coordinator serialises the
            # actual downloads, while PiTV has the whole horizon prepared up front.
            app.state.jobs.progress(job, "asking pitv_content for material the bands lack")
            asked = wanted_mod.request_all_band_material(conn, all_settings(conn))
            return {**result, "content_reset": reset, "import": imported["summary"],
                    "band_material": asked["summary"]}
        finally:
            conn.close()
            # The clear may have succeeded even if a later channel-day failed to build.
            _schedule_changed(app)

    return app.state.jobs.submit("schedule", "Fresh rebuild", run).public()


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


def _schedule_changed(app: FastAPI) -> None:
    """Tell the browser tabs and the player (which caches the slot on air) to re-read."""
    app.state.bus.publish_threadsafe("schedule", {"changed": True})
    app.state.player.call("schedule-changed")


def _require_channel(conn: sqlite3.Connection, channel_id: int) -> None:
    if not conn.execute("SELECT 1 FROM channels WHERE id = ?", (channel_id,)).fetchone():
        raise HTTPException(404, "channel not found")


def _rebuild(request: Request, conn: sqlite3.Connection, channel_id: int, from_ts: int) -> dict[str, Any]:
    result = rebuild_from(conn, channel_id, from_ts)
    _schedule_changed(request.app)
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
    media = _media_with_show(conn, optional_int(body.get("media_id"), "media_id") or 0)
    title, subtitle = slot_titles(dict(media), media["show_title"])
    end = row["start_ts"] + round(media["duration"])
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
    _require_channel(conn, channel_id)
    media = _media_with_show(conn, media_id)
    day = broadcast_day_for(start_ts, all_settings(conn), tz_of(conn)).isoformat()
    title, subtitle = slot_titles(dict(media), media["show_title"])
    end = start_ts + round(media["duration"])
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
    _require_channel(conn, channel_id)
    return _rebuild(request, conn, channel_id, from_ts)


# --- system ---------------------------------------------------------------------------------------------

def _cmd(args: list[str], timeout: float = 3) -> str:
    """stdout of a command, or '' when it is missing or fails (this page must render anywhere)."""
    return run_cmd(args, timeout)[1]


def _live_checks(request: Request, conn: sqlite3.Connection) -> dict[str, bool | None]:
    """Whether each application's processes answer, whatever systemd says."""
    player = request.app.state.player.call("state", timeout=1)
    status, tool = tool_client.request(tool_url(conn), "GET", "status", timeout=3)
    tool_up = status == 200 and isinstance(tool, dict) and ("api_version" in tool or "tool" in tool)
    return {
        "pitv-web.service": True,
        "pitv-player.service": bool(player.get("ok") and player.get("online", True)),
        "pitv-content-api.service": tool_up,
        CONTENT_RUN: bool(tool.get("active_job")) if tool_up else None,
        CONTENT_TIMER: tool_up or None,   # without the timer, runs are started through the API
    }


def _disk_usage(path: Path) -> dict[str, int] | None:
    """Size and free space of the filesystem holding `path`; None when it is not there (an
    unmounted share reads as missing, not as an error on the page)."""
    try:
        if not path.is_dir():
            return None
        u = shutil.disk_usage(path)
    except OSError:
        return None
    return {"total": u.total, "free": u.free}


@router.get("/system")
def system_info(request: Request, conn: sqlite3.Connection = Depends(admin_conn)):
    cfg = request.app.state.cfg
    time_info = {}
    for line in _cmd(["timedatectl", "show"]).splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            time_info[k] = v
    mounts = []
    for r in conn.execute("SELECT name, path FROM sources WHERE location = 'nas' ORDER BY id"):
        usage = _disk_usage(Path(r["path"]))
        mounts.append({"name": r["name"], "path": r["path"], "available": usage is not None, "usage": usage})
    data_usage = _disk_usage(cfg.data_dir) or {}
    mpv = re.match(r"mpv (\S+)", _cmd([cfg.mpv_binary, "--version"]))
    host = host_info(__version__, {"mpv": mpv.group(1) if mpv else None})
    return {
        "host": host,
        "time": {"now": now_ts(), "local": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "ntp": time_info.get("NTPSynchronized"), "timezone": time_info.get("Timezone")},
        "services": services(_live_checks(request, conn), on_pi=host["pi"]), "mounts": mounts,
        "data": {"path": str(cfg.data_dir), "db": str(cfg.db_path),
                 "db_size": cfg.db_path.stat().st_size if cfg.db_path.exists() else 0,
                 "free": data_usage.get("free"), "total": data_usage.get("total")},
        "jobs": request.app.state.jobs.recent(10),
        "cache_dir": get_setting(conn, "cache_dir") or "",
    }


@router.post("/system/service/{name}/{action}")
def service_action(name: str, action: str, request: Request):
    if action not in SERVICE_ACTIONS.get(name, ()):
        raise HTTPException(400, "unsupported service or action")
    props = systemd_state([name]).get(name) or {}
    if props.get("LoadState") == "loaded":
        cmd = ["sudo", "-n", "systemctl", action, name]
    else:
        dev_name = DEV_UNITS.get(name)
        dev = systemd_state([dev_name], user=True).get(dev_name) if dev_name else None
        if not dev or dev.get("LoadState") != "loaded":
            if name == PLAYER_UNIT and action == "start":
                # No unit at all (a desktop): the keeper's own way of starting the player.
                result = start_player(request.app.state.cfg)
                if not result["ok"]:
                    raise HTTPException(503, f"could not start the player: {result['error']}")
                return {"ok": True, "via": result["via"]}
            raise HTTPException(409, f"{name} is not installed")
        cmd = ["systemctl", "--user", action, dev_name]
    rc, _, err = run_cmd(cmd, timeout=30)
    if rc != 0:
        raise HTTPException(500, err or "systemctl failed")
    return {"ok": True}


@router.post("/system/services/{action}")
def all_service_action(action: str):
    """Control the persistent stack in dependency order. Web goes last when stopping or
    restarting because this request is running inside it."""
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, "action must be start, stop or restart")
    forward = ["pitv-web.service", "pitv-content-api.service", "pitv-player.service", CONTENT_TIMER]
    order = list(reversed(forward)) if action == "stop" else [*forward[1:], forward[0]]
    done, skipped = [], []
    for name in order:
        try:
            service_action(name, action)
            done.append(name)
        except HTTPException as exc:
            if exc.status_code == 409:
                skipped.append(name)       # for example the Pi-only timer on a desktop
                continue
            raise
    return {"ok": True, "services": done, "skipped": skipped}


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


@router.get("/doctor")
def doctor_report(request: Request, conn: sqlite3.Connection = Depends(admin_conn)):
    """The `pitv doctor` report (pitv/doctor.py) for the admin: read-only, and behind the admin
    session like everything else on this router."""
    from ... import doctor
    return doctor.report(conn, request.app.state.cfg)


@router.get("/jobs")
def list_jobs(request: Request):
    return request.app.state.jobs.recent(20)


@router.get("/export")
def export_overrides(conn: sqlite3.Connection = Depends(admin_conn)):
    """Everything an admin has changed, for backup: settings, channels, sources, overrides."""
    shows = [{"uid": r["path"], "title": r["title"], "overrides": json.loads(r["overrides"]), "home_channel_id": r["home_channel_id"],
              "mode": r["mode"], "anchor_time": r["anchor_time"], "anchor_days": r["anchor_days"], "rest_weeks": r["rest_weeks"], "excluded": r["excluded"]}
             for r in conn.execute("SELECT * FROM shows WHERE overrides != '{}' OR excluded = 1 OR mode != 'auto'")]
    media = [{"uid": r["uid"], "title": r["title"], "overrides": json.loads(r["overrides"]), "excluded": r["excluded"]}
             for r in conn.execute("SELECT * FROM media WHERE overrides != '{}' OR excluded = 1")]
    return {"exported_at": now_ts(), "settings": _public_settings(conn),
            "channels": rows_to_dicts(conn.execute("SELECT * FROM channels")), "sources": rows_to_dicts(conn.execute("SELECT * FROM sources")),
            "shows": shows, "media": media}


# --- logs ---------------------------------------------------------------------------------------------

LOG_NAMES = ("player", "web", "catalogue", "schedule", "stream", "install")
INSTALL_LOG = Path("/work/install/install.log")   # written by the SD-card installer and first boot


def _log_path(cfg, name: str) -> Path:
    """Only the fixed names in LOG_NAMES reach here, so no request text becomes a path."""
    if name == "install":
        return INSTALL_LOG if INSTALL_LOG.exists() else log_dir(cfg) / "install.log"
    return log_dir(cfg) / f"{name}.log"


@router.get("/logs")
def list_logs(request: Request):
    out = []
    for name in LOG_NAMES:
        p = _log_path(request.app.state.cfg, name)
        try:
            st = p.stat()
        except OSError:
            st = None
        out.append({"name": name, "path": str(p), "size": st.st_size if st else 0,
                    "modified": int(st.st_mtime) if st else None})
    return out


@router.get("/logs/{name}")
def read_log(name: str, request: Request, lines: int = 300, q: str = "", level: str = ""):
    """Tail of a log file, newest last. `q` filters by substring, `level` by minimum level."""
    if name not in LOG_NAMES:
        raise HTTPException(404, "unknown log")
    p = _log_path(request.app.state.cfg, name)
    return {"name": name, "lines": log_tail(p, lines, q, level), "exists": p.exists()}


JOURNAL_UNITS = ("pitv-player", "pitv-web")


@router.get("/logs/journal/{unit}")
def read_journal(unit: str, lines: int = 300):
    """systemd journal for a PiTV unit (Pi only; empty elsewhere)."""
    if unit not in JOURNAL_UNITS:
        raise HTTPException(404, "unknown unit")
    rc, out, err = run_cmd(["journalctl", "-u", unit, "-n", str(max(10, min(lines, 2000))), "--no-pager",
                            "-o", "short-iso"], timeout=15)
    return {"unit": unit, "text": out if rc == 0 else "", "error": err[:300]}


# --- line-ups ------------------------------------------------------------------------------------------

@router.get("/bands/fetch-kinds")
def bands_fetch_kinds(conn: sqlite3.Connection = Depends(admin_conn)):
    """What pitv_content can go and fetch, for the channel and band editors. Asked of the tool
    itself so the list follows what it actually supports; its last answer is remembered for when
    it is not running."""
    status, payload = tool_client.request(tool_url(conn), "GET", "settings", timeout=10)
    kinds = []
    if status == 200 and isinstance(payload, dict):
        for field in payload.get("schema") or []:
            if isinstance(field, dict) and field.get("key") == "catalogue_kinds":
                kinds = [k for k in (field.get("choices") or []) if isinstance(k, str)]
    if kinds:
        dbm.set_setting(conn, "content_fetch_kinds", json.dumps(kinds))
        return kinds
    return get_setting(conn, "content_fetch_kinds") or []


@router.post("/bands/material")
def bands_material(conn: sqlite3.Connection = Depends(admin_conn)):
    """Declare every band's current shortfall to pitv_content now."""
    return wanted_mod.request_all_band_material(conn, all_settings(conn))


@router.get("/bands/needs")
def bands_needs(conn: sqlite3.Connection = Depends(admin_conn)):
    """What each band is short of, for the channel editor."""
    settings = all_settings(conn)
    return [{"channel_id": n["band"].channel_id, "name": n["band"].name, "have": n["have"], "want": n["want"],
             "kind": n["kind"], "minutes": n["minutes"], "last_fetch_at": n["band"].last_fetch_at}
            for n in wanted_mod.band_needs(conn, settings)]


@router.get("/library/facets")
def library_facets(conn: sqlite3.Connection = Depends(admin_conn)):
    return lineup_mod.facets(conn)


@router.get("/lineup")
def lineup_list(conn: sqlite3.Connection = Depends(admin_conn), channel_id: int | None = None):
    return lineup_mod.entries(conn, channel_id=channel_id)


@router.get("/lineup/options")
def lineup_options(conn: sqlite3.Connection = Depends(admin_conn), q: str = "", limit: int = 50):
    """Dropdown choices: library series and films plus pitv_content's catalogue when it is up."""
    out = lineup_mod.options(conn, q, limit)
    for c in tool_catalogue(conn):
        name = c.get("name")
        if not isinstance(name, str) or (q and q.lower() not in name.lower()) or c.get("kind") in ("music", "adverts"):
            continue
        out.append({"type": "show", "title": name, "year": c.get("first_year"), "on_disk": bool(c.get("count_on_disk")),
                    "catalogue": True, "episodes_known": c.get("episodes_known"), "years": c.get("years")})
    return out


@router.post("/lineup/placement")
def lineup_placement(body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    """What a title about to be added would be taken for, and the channel it would go to, so the
    add dialog can show both before anything is added. The type is the owner's if given, else
    read from the genres by the same rule placement uses."""
    kind = body.get("kind")
    if kind not in ("show", "movie"):
        raise HTTPException(400, "kind must be show or movie")
    genres = body.get("genres") if isinstance(body.get("genres"), list) else []
    ptype = genre_rules.programme_type(kind, genres, None, body.get("programme_type"))
    channel_id = lineup_mod.best_channel(conn, genres, ptype=ptype, kids=genre_rules.is_childrens(genres),
                                         year=optional_int(body.get("year"), "year"))
    row = conn.execute("SELECT number, name FROM channels WHERE id = ?", (channel_id,)).fetchone() if channel_id else None
    return {"programme_type": ptype, "channel_id": channel_id,
            "channel_number": row["number"] if row else None, "channel_name": row["name"] if row else None}


@router.get("/lineup/known")
def lineup_known(kind: str = "show", conn: sqlite3.Connection = Depends(admin_conn)):
    """What the catalogue already holds of a kind, so the add dialog can grey out a search result
    instead of adding the same programme twice: line-up entries of every source with the
    identity they were confirmed against, library titles no line-up carries, and for adverts and
    music videos the wanted list and the library."""
    if kind not in ("show", "movie", "advert", "music"):
        raise HTTPException(400, "kind must be show, movie, advert or music")
    if kind in ("show", "movie"):
        rows = rows_to_dicts(conn.execute(
            "SELECT l.title, l.year, l.match, l.source, c.number AS channel_number, c.name AS channel_name"
            " FROM lineup l JOIN channels c ON c.id = l.channel_id WHERE l.kind = ?", (kind,)))
        library = ("SELECT title, year FROM shows WHERE missing = 0 AND id NOT IN (SELECT show_id FROM lineup WHERE show_id IS NOT NULL)"
                   if kind == "show" else
                   "SELECT title, year FROM media WHERE kind = 'movie' AND missing = 0"
                   " AND id NOT IN (SELECT media_id FROM lineup WHERE media_id IS NOT NULL)")
        rows += [{**r, "source": "library"} for r in rows_to_dicts(conn.execute(library))]
    else:
        rows = [{**r, "source": "wanted"} for r in rows_to_dicts(conn.execute(
            "SELECT title, year, artist, ref FROM wanted WHERE kind = ? AND status != 'failed'", (kind,)))]
        rows += [{**r, "source": "library"} for r in rows_to_dicts(conn.execute(
            "SELECT title, year, artist FROM media WHERE kind = ? AND missing = 0", (kind,)))]
    for r in rows:
        match = r.pop("match", None)
        try:
            match = json.loads(match) if isinstance(match, str) and match else match
        except ValueError:
            match = None
        r["match"] = {"source": match.get("source"), "id": str(match.get("id"))} if isinstance(match, dict) and match.get("id") else None
    return rows


@router.post("/lineup")
def lineup_add(body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    # A YouTube channel or playlist is a series whose episodes are its videos, so it is an
    # ordinary entry whose confirmed identity names the channel instead of a television
    # database. The address is parsed here rather than in the browser so one reading of it is
    # tested: pitv_content fetches the url, and the id it keeps must survive a rename.
    if url := str(body.get("youtube_url") or "").strip():
        found = youtube.parse(url)
        if found is None:
            raise HTTPException(400, "not a YouTube channel or playlist address")
        body = {**body, "kind": "show", "match": found, "catalogue": True,
                "genres": genre_rules.canonical_all([*(body.get("genres") or []), "YouTube"])}
    try:
        return lineup_mod.add(conn, optional_int(body.get("channel_id"), "channel_id"), show_id=body.get("show_id"), media_id=body.get("media_id"),
                              title=body.get("title"), year=body.get("year"), kind=body.get("kind"), genres=body.get("genres"),
                              transient=body.get("transient"), episode_minutes=body.get("episode_minutes"),
                              source="catalogue" if body.get("catalogue") else "manual", match=body.get("match"),
                              programme_type=body.get("programme_type") or None,
                              episode_count=optional_int(body.get("episode_count"), "episode_count"),
                              certificate=optional_text(body.get("certificate"), "certificate"))
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/lineup/{lid}")
def lineup_update(lid: int, body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    if dbm.find_id(conn, "lineup", "id", lid) is None:
        raise HTTPException(404, "line-up entry not found")
    try:
        return lineup_mod.update(conn, lid, body)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


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
def lineup_import(body: dict[str, Any] = Depends(admin_json), conn: sqlite3.Connection = Depends(admin_conn)):
    try:
        return lineup_mod.import_doc(conn, body)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
