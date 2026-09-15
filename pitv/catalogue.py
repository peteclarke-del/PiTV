"""The programme catalogue: what PiTV can schedule.

pitv_content owns the sources and indexes them; PiTV imports its library index (schema 2,
docs/CONTENT_CONTRACT.md) into the `sources`, `shows` and `media` tables and never reads the
NAS itself. Custom programming lives in channel line-ups (lineup.py); material fetched online
enters the catalogue from delivery reports (content.py).

Import keeps row ids stable across imports (rows are matched by index uid, then by path for
rows created before uids existed), so schedules, history, overrides and line-ups keep their
references. A complete index marks anything it no longer lists as missing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import tool_client
from .db import (CARTOON_GENRES, KIDS_GENRES, all_settings, now_ts, row_to_dict, rows_to_dicts, run_log_finish,
                 run_log_start, tx)
from .player.hwdec import PI_HW_CODECS

log = logging.getLogger("pitv.catalogue")

SCHEMA = 2
SOURCE_TYPES = ("tv", "movie", "advert", "ident", "music")
KINDS = ("episode", "movie", "advert", "ident", "music")
CATEGORIES = ("general", "sport", "kids")
REINDEX_TIMEOUT = 1800        # seconds to wait for a NAS re-index before importing what is there
REINDEX_POLL = 3
CONCERT_MINUTES = 35          # a music item this long is a concert even when not tagged
UNSAFE_TAGS = {"alcohol", "tobacco", "adult", "gambling", "18"}


class IndexFormatError(ValueError):
    """The document is not a schema 2 library index."""


def index_file(settings: dict[str, Any]) -> Path | None:
    cache = settings.get("cache_dir") or ""
    return Path(cache) / "index" / "library.json" if cache else None


def fetch_index(settings: dict[str, Any], reindex: bool = False) -> tuple[dict[str, Any] | None, str]:
    """The current index from pitv_content's API, or from the file it writes when the API is
    down. Returns (document or None, where it came from or why it failed)."""
    base = tool_client.base_url(settings)
    if reindex:
        _reindex(base)
    status, payload = tool_client.request(base, "GET", "library", timeout=60)
    if status == 200 and isinstance(payload, dict):
        return payload, f"pitv_content API at {base}"
    path = index_file(settings)
    if path is not None and path.exists():
        try:
            return json.loads(path.read_text()), f"index file {path}"
        except (OSError, ValueError) as exc:
            return None, f"index file {path} unreadable: {exc}"
    reason = payload.get("error") if isinstance(payload, dict) else f"HTTP {status}"
    return None, f"no index: API {reason}; file {path or '(no cache_dir)'} absent"


def _reindex(base: str, timeout: float = REINDEX_TIMEOUT) -> None:
    """Start a re-index and wait for that job to finish, so the import that follows reads the
    new index rather than the one it replaces. A failure or timeout is logged; the current index
    is imported regardless."""
    status, started = tool_client.request(base, "POST", "index", body={}, timeout=10)
    job_id = started.get("job_id") if status == 200 and isinstance(started, dict) else None
    if not job_id:
        log.warning("re-index not started (HTTP %s): %s", status, started.get("error", "") if isinstance(started, dict) else "")
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, jobs = tool_client.request(base, "GET", "jobs", timeout=10)
        job = next((j for j in jobs if isinstance(j, dict) and j.get("job_id") == job_id), None) \
            if status == 200 and isinstance(jobs, list) else None
        if job is not None and job.get("finished_ts"):
            if job.get("status") != "ok":
                log.warning("re-index %s ended %s, no new index: %s", job_id, job.get("status"), job.get("summary", ""))
            return
        time.sleep(REINDEX_POLL)
    log.warning("re-index %s still running after %ds; importing the current index", job_id, timeout)


def _genres(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = [value]
    return [str(g) for g in (value or []) if str(g).strip()]


def _family_safe(item: dict[str, Any], keywords: list[str]) -> int:
    """pitv_content's verdict wins; tags next; PiTV's keyword list only when neither is given."""
    if isinstance(item.get("family_safe"), bool):
        return int(item["family_safe"])
    tags = {str(t).lower() for t in (item.get("tags") or [])}
    if tags & UNSAFE_TAGS:
        return 0
    name = f"{item.get('title') or ''} {Path(str(item.get('path') or '')).stem}".lower()
    # Whole words only: "ale" must not match "sale", nor "gin" "engineering".
    return int(not any(re.search(rf"\b{re.escape(k.lower())}\b", name) for k in keywords))


def _attention(item: dict[str, Any], hwdec: bool) -> str | None:
    notes = []
    if not item.get("duration"):
        notes.append("No duration in the library index")
    if item.get("year") is None and item["kind"] in ("episode", "movie", "advert"):
        notes.append("No year found")
    if item["kind"] == "movie" and not item.get("certificate"):
        notes.append("No certificate (treated as 15, post-watershed)")
    if not hwdec and (item.get("height") or 0) >= 720:
        notes.append(f"Software decode only ({item.get('vcodec')}, {item.get('height')}p): pitv_content transcodes it when scheduled")
    return "; ".join(notes) or None


def _upsert(conn: sqlite3.Connection, table: str, key_col: str, key: Any, fields: dict[str, Any],
            fallback: tuple[str, Any] | None = None) -> int:
    """Insert or update one row matched on `key_col`, or on `fallback` (column, value) for rows
    created before the key existed. Returns the row id."""
    row = conn.execute(f"SELECT id FROM {table} WHERE {key_col} = ?", (key,)).fetchone()
    if row is None and fallback is not None:
        row = conn.execute(f"SELECT id FROM {table} WHERE {fallback[0]} = ?", (fallback[1],)).fetchone()
    if row is not None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*fields.values(), row["id"]))
        return int(row["id"])
    cols = ", ".join(fields)
    cur = conn.execute(f"INSERT INTO {table}({cols}) VALUES ({', '.join('?' * len(fields))})", tuple(fields.values()))
    return int(cur.lastrowid)


def import_index(conn: sqlite3.Connection, doc: dict[str, Any]) -> dict[str, Any]:
    """Apply a library index. Idempotent; returns counts for the run log."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise IndexFormatError(f"expected a schema {SCHEMA} library index, got schema {doc.get('schema') if isinstance(doc, dict) else '?'}")
    settings = all_settings(conn)
    keywords = settings.get("adult_advert_keywords") or []
    cartoon = {g.lower() for g in (settings.get("cartoon_genres") or CARTOON_GENRES)}
    complete = bool(doc.get("complete", True))
    counts = {"sources": 0, "shows": 0, "items": 0, "new": 0, "missing": 0, "rejected": 0}
    source_ids: dict[str, int] = {}
    source_meta: dict[str, dict[str, Any]] = {}
    show_ids: dict[str, int] = {}
    seen: set[str] = set()
    with tx(conn):
        for src in doc.get("sources") or []:
            sid = str(src.get("id") or "")
            stype = src.get("type")
            if not sid or stype not in SOURCE_TYPES:
                counts["rejected"] += 1
                continue
            category = src.get("category") if src.get("category") in CATEGORIES else "general"
            location = "cache" if src.get("location") == "cache" else "nas"
            source_ids[sid] = _upsert(conn, "sources", "uid", fallback=("path", str(src.get("root") or "")), fields={
                "uid": sid, "type": stype, "name": str(src.get("name") or sid), "path": str(src.get("root") or ""),
                "remote": src.get("remote"), "location": location, "category": category,
                "enabled": int(bool(src.get("enabled", True))), "last_indexed_at": now_ts()}, key=sid)
            source_meta[sid] = {"category": category, "location": location}
            counts["sources"] += 1
        if complete:
            conn.execute("UPDATE sources SET enabled = 0 WHERE uid IS NULL OR uid NOT IN (%s)"
                         % ",".join("?" * len(source_ids)) if source_ids else "UPDATE sources SET enabled = 0",
                         tuple(source_ids))

        for sh in doc.get("shows") or []:
            uid = str(sh.get("uid") or "")
            if not uid or sh.get("source") not in source_ids:
                counts["rejected"] += 1
                continue
            genres = _genres(sh.get("genres"))
            category = sh.get("category") if sh.get("category") in CATEGORIES else source_meta[sh["source"]]["category"]
            kids = int(category == "kids" or any(g.lower() in KIDS_GENRES for g in genres))
            if category != "sport" and any(g.lower() in cartoon for g in genres):
                category = "cartoon"   # informational (dayparts, the admin); pitv_content may send it as kids
            fields = {"source_id": source_ids[sh["source"]], "path": uid, "title": str(sh.get("title") or uid),
                      "year": sh.get("year"), "certificate": sh.get("certificate"), "genres": json.dumps(genres),
                      "plot": sh.get("plot"), "kids": kids, "category": category, "missing": 0, "updated_at": now_ts()}
            legacy = conn.execute("SELECT id FROM shows WHERE path NOT LIKE 'show:%' AND lower(title) = lower(?)"
                                  " AND (year IS ? OR year = ?)", (fields["title"], sh.get("year"), sh.get("year"))).fetchone()
            show_ids[uid] = _upsert(conn, "shows", "path", uid, fields,
                                    fallback=("id", legacy["id"]) if legacy else None)
            counts["shows"] += 1

        for it in doc.get("items") or []:
            uid = str(it.get("uid") or "")
            kind = it.get("kind")
            src = it.get("source")
            if not uid or kind not in KINDS or src not in source_ids or not it.get("path"):
                counts["rejected"] += 1
                continue
            show_id = show_ids.get(str(it.get("show_uid") or "")) if kind == "episode" else None
            if kind == "episode" and show_id is None:
                counts["rejected"] += 1
                continue
            vcodec = it.get("vcodec")
            hwdec = (vcodec or "") in PI_HW_CODECS
            location = source_meta[src]["location"]
            concert = it.get("concert")
            if concert is None and kind == "music":
                concert = (it.get("duration") or 0) >= CONCERT_MINUTES * 60
            fields = {
                "uid": uid, "source_id": source_ids[src], "kind": kind, "show_id": show_id,
                "season": it.get("season"), "episode": it.get("episode"), "title": str(it.get("title") or uid),
                "year": it.get("year"), "path": str(it["path"]), "origin": location,
                "size": it.get("size"), "mtime": it.get("mtime"), "duration": it.get("duration"),
                "vcodec": vcodec, "acodec": it.get("acodec"), "width": it.get("width"), "height": it.get("height"),
                "interlaced": int(bool(it.get("interlaced"))), "hwdec": int(hwdec),
                "certificate": it.get("certificate"), "genres": json.dumps(_genres(it.get("genres"))),
                "plot": it.get("plot"), "channel_hint": it.get("channel_hint"), "artist": it.get("artist"),
                "concert": int(bool(concert)),
                "family_safe": _family_safe(it, keywords) if kind == "advert" else 1,
                "attention": _attention({**it, "kind": kind}, hwdec), "missing": 0, "updated_at": now_ts(),
            }
            if location == "cache":
                fields["cache_path"] = str(it["path"])   # already where playback wants it
            existed = conn.execute("SELECT 1 FROM media WHERE uid = ? OR path = ?", (uid, fields["path"])).fetchone()
            _upsert(conn, "media", "uid", uid, fields, fallback=("path", fields["path"]))
            counts["items"] += 1
            counts["new"] += existed is None
            seen.add(uid)

        if complete:
            # Online material is managed through delivery reports, not the index.
            for r in conn.execute("SELECT id, uid FROM media WHERE missing = 0 AND origin != 'online'").fetchall():
                if r["uid"] not in seen:
                    conn.execute("UPDATE media SET missing = 1 WHERE id = ?", (r["id"],))
                    counts["missing"] += 1
            conn.execute("UPDATE shows SET missing = 1 WHERE missing = 0 AND id NOT IN"
                         " (SELECT DISTINCT show_id FROM media WHERE show_id IS NOT NULL AND missing = 0)")
        for rid in source_ids.values():
            n = conn.execute("SELECT COUNT(*) FROM media WHERE source_id = ? AND missing = 0", (rid,)).fetchone()[0]
            conn.execute("UPDATE sources SET index_summary = ? WHERE id = ?", (f"{n} items in the index", rid))
    return counts


def import_and_place(conn: sqlite3.Connection, doc: dict[str, Any], origin: str = "") -> dict[str, Any]:
    """Import, then place new series and films into line-ups and refresh the JSON mirror.
    Logged as a `catalogue` run."""
    from .lineup import generate, restore_if_empty
    run_id = run_log_start(conn, "catalogue")
    try:
        counts = import_index(conn, doc)
    except IndexFormatError as exc:
        run_log_finish(conn, run_id, "error", str(exc), [str(exc)])
        raise
    restore_if_empty(conn)
    placed = generate(conn)
    write_mirror(conn)
    summary = (f"{counts['items']} items ({counts['new']} new, {counts['missing']} now missing,"
               f" {counts['rejected']} rejected) from {counts['sources']} sources; line-ups: {placed['assigned']} placed,"
               f" {placed['unmatched']} matched no channel")
    run_log_finish(conn, run_id, "warning" if counts["rejected"] or placed["unmatched"] else "ok", summary,
                   [f"source: {origin}"] if origin else [])
    log.info("catalogue import: %s", summary)
    return {**counts, **{f"lineup_{k}": v for k, v in placed.items()}, "summary": summary, "run_id": run_id}


def refresh(conn: sqlite3.Connection, reindex: bool = False) -> dict[str, Any]:
    """Fetch pitv_content's index (API first, file second) and import it."""
    doc, origin = fetch_index(all_settings(conn), reindex=reindex)
    if doc is None:
        run_id = run_log_start(conn, "catalogue")
        run_log_finish(conn, run_id, "error", origin, [origin])
        log.error("catalogue refresh failed: %s", origin)
        return {"status": "error", "summary": origin}
    result = import_and_place(conn, doc, origin)
    return {"status": "ok", **result}


def index_changed_since(settings: dict[str, Any], last_mtime: float) -> float | None:
    """mtime of the index file when it is newer than `last_mtime` (maintenance polls this)."""
    path = index_file(settings)
    try:
        mtime = path.stat().st_mtime if path is not None else None
    except OSError:
        return None
    return mtime if mtime and mtime > last_mtime else None


# --- mirror -----------------------------------------------------------------------------------

def export(conn: sqlite3.Connection) -> dict[str, Any]:
    """The catalogue as a document: every series and film PiTV can schedule, where each one
    comes from, and whether it is cached, plus custom line-up entries not in the index."""
    shows = rows_to_dicts(conn.execute(
        "SELECT sh.id, sh.title, sh.year, sh.genres, sh.category, sh.certificate, c.number AS channel,"
        " (SELECT COUNT(*) FROM media m WHERE m.show_id = sh.id AND m.missing = 0) AS episodes,"
        " (SELECT COUNT(*) FROM media m WHERE m.show_id = sh.id AND m.missing = 0 AND m.cache_path IS NOT NULL) AS cached"
        " FROM shows sh LEFT JOIN channels c ON c.id = sh.home_channel_id WHERE sh.missing = 0 ORDER BY sh.title"))
    films = rows_to_dicts(conn.execute(
        "SELECT m.id, m.title, m.year, m.genres, m.certificate, m.origin, m.duration, c.number AS channel,"
        " m.cache_path IS NOT NULL AS cached FROM media m LEFT JOIN channels c ON c.id = m.home_channel_id"
        " WHERE m.kind = 'movie' AND m.missing = 0 ORDER BY m.title"))
    custom = rows_to_dicts(conn.execute(
        "SELECT l.title, l.year, l.kind, l.transient, l.episode_minutes, c.number AS channel FROM lineup l"
        " JOIN channels c ON c.id = l.channel_id WHERE l.source != 'library' ORDER BY l.title"))
    return {"schema": 1, "generated_ts": now_ts(), "shows": shows, "films": films, "custom": custom}


def write_mirror(conn: sqlite3.Connection) -> None:
    """catalogue.json beside the database, rewritten atomically."""
    path = None
    for row in conn.execute("PRAGMA database_list"):
        if row["name"] == "main" and row["file"]:
            path = Path(row["file"]).parent / "catalogue.json"
    if path is None:
        return
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(export(conn), indent=2))
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not write catalogue mirror: %s", exc)


def last_import(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM run_log WHERE kind = 'catalogue' ORDER BY id DESC LIMIT 1").fetchone()
    return row_to_dict(row) if row else None

