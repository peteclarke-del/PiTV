"""The programme catalogue: what PiTV can schedule.

pitv_content owns the sources and indexes them; PiTV imports its library index (schema 2,
docs/CONTENT_CONTRACT.md) into the `sources`, `shows` and `media` tables and never reads the
NAS itself. Custom programming lives in channel line-ups (lineup.py); material fetched online
enters the catalogue from delivery reports (content.py).

Import keeps row ids stable across imports (rows are matched by index uid, then by path for
rows created before uids existed), so schedules, history, overrides and line-ups keep their
references. A complete index marks anything it no longer lists as missing.

The index comes from another process, so every field is read defensively: a record without a
usable uid, kind, source or path is rejected and counted, an optional field of the wrong type
is dropped, and neither stops the rest of the import.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import urllib.parse
from pathlib import Path
from typing import Any

from . import genres as genre_rules
from . import tool_client
from .db import (
    all_settings,
    as_bool,
    as_float,
    as_int,
    as_text,
    assign_ident_channels,
    effective,
    find_id,
    genre_list,
    insert_row,
    now_ts,
    row_to_dict,
    rows_to_dicts,
    run_log_finish,
    run_log_start,
    tx,
    update_row,
    write_data_file,
)
from .lineup import generate, restore_if_empty
from .player.hwdec import PI_HW_CODECS
from .scheduler.horizon import refill_empty_days
from .scheduler.rules import normalise_cert

log = logging.getLogger("pitv.catalogue")

SCHEMA = 2
SOURCE_TYPES = ("tv", "movie", "advert", "ident", "music")
KINDS = ("episode", "movie", "advert", "ident", "music")
SOURCE_CATEGORIES = ("general", "sport", "kids")
REINDEX_TIMEOUT = 1800        # seconds to wait for a NAS re-index before importing what is there
REINDEX_POLL = 3
CONCERT_MINUTES = 35          # a music item this long is a concert even when not tagged
UNSAFE_TAGS = {"alcohol", "tobacco", "adult", "gambling", "18"}
REJECTS_KEPT = 50             # rejected records described in the run log; the rest are only counted
METADATA_RECHECK_DAYS = 30
MIRROR = "catalogue.json"


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


def keyword_pattern(keywords: Any) -> re.Pattern[str] | None:
    """One pattern for the adult advert keywords, matching whole words only ("ale" must not match
    "sale", nor "gin" "engineering"). Lookarounds rather than \\b, so keywords that start or end
    with punctuation ("18+") still match when followed by a space."""
    names = (as_text(k) for k in (keywords if isinstance(keywords, list) else []))
    words = [re.escape(k.lower()) for k in names if k and k.strip()]
    return re.compile(rf"(?<!\w)(?:{'|'.join(words)})(?!\w)") if words else None


def family_safe(item: dict[str, Any], unsafe: re.Pattern[str] | None) -> int:
    """Whether an advert may air on a family channel: pitv_content's verdict (a bool) wins; its
    tags next; PiTV's keyword list only when neither is given. Used for indexed adverts and for
    adverts fetched online alike."""
    if isinstance(item.get("family_safe"), bool):
        return int(item["family_safe"])
    tags = item.get("tags")
    if not isinstance(tags, list):
        tags = [tags]
    if {t.lower() for t in map(as_text, tags) if t} & UNSAFE_TAGS:
        return 0
    name = f"{as_text(item.get('title')) or ''} {Path(as_text(item.get('path')) or '').stem}".lower()
    return int(unsafe is None or unsafe.search(name) is None)


def _attention(fields: dict[str, Any]) -> str | None:
    """Why an imported item needs a look in the admin, from its stored fields."""
    notes = []
    if not fields["duration"]:
        notes.append("No duration in the library index")
    if fields["year"] is None and fields["kind"] in ("episode", "movie", "advert"):
        notes.append("No year found")
    if fields["kind"] == "movie" and not fields["certificate"]:
        notes.append("No certificate (treated as 15, post-watershed)")
    if not fields["hwdec"] and (fields["height"] or 0) >= 720:
        notes.append(f"Software decode only ({fields['vcodec']}, {fields['height']}p): pitv_content transcodes it when scheduled")
    return "; ".join(notes) or None


def _refresh_attention(conn: sqlite3.Connection, media_id: int) -> None:
    row = row_to_dict(conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone())
    if row:
        conn.execute("UPDATE media SET attention = ? WHERE id = ?", (_attention(effective(row)), media_id))


def _save(conn: sqlite3.Connection, table: str, row_id: int | None, fields: dict[str, Any]) -> int:
    """Update row `row_id`, or insert when there is none. Returns the row id."""
    if row_id is None:
        return insert_row(conn, table, fields)
    update_row(conn, table, row_id, fields)
    return row_id


def _records(doc: dict[str, Any], key: str) -> list[Any]:
    value = doc.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise IndexFormatError(f"library index '{key}' is not a list")
    return value


def import_index(conn: sqlite3.Connection, doc: dict[str, Any]) -> dict[str, Any]:
    """Apply a library index in one transaction. Idempotent; returns counts for the run log and
    `rejects`, a description of the first REJECTS_KEPT records that could not be used."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        got = doc.get("schema") if isinstance(doc, dict) else type(doc).__name__
        raise IndexFormatError(f"expected a schema {SCHEMA} library index, got schema {got}")
    sources_in, shows_in, items_in = (_records(doc, k) for k in ("sources", "shows", "items"))
    settings = all_settings(conn)
    unsafe = keyword_pattern(settings.get("adult_advert_keywords"))
    complete = bool(doc.get("complete", True))
    now = now_ts()
    counts: dict[str, Any] = {"sources": 0, "shows": 0, "items": 0, "new": 0, "missing": 0, "rejected": 0}
    rejects: list[str] = []

    def reject(what: str, uid: Any, why: str) -> None:
        counts["rejected"] += 1
        if len(rejects) < REJECTS_KEPT:
            rejects.append(f"{what} {as_text(uid) or '?'}: {why}")

    sources: dict[str, dict[str, Any]] = {}     # index source id -> row id, category, location
    show_ids: dict[str, int] = {}
    seen: set[str] = set()
    with tx(conn):
        for src in sources_in:
            sid = as_text(src.get("id")) if isinstance(src, dict) else None
            if not sid or src.get("type") not in SOURCE_TYPES:
                reject("source", sid, "no id or an unknown type")
                continue
            category = src.get("category") if src.get("category") in SOURCE_CATEGORIES else "general"
            location = "cache" if src.get("location") == "cache" else "nas"
            root = as_text(src.get("root")) or ""
            fields = {"uid": sid, "type": src["type"], "name": as_text(src.get("name")) or sid, "path": root,
                      "remote": as_text(src.get("remote")), "location": location, "category": category,
                      "enabled": int(as_bool(src.get("enabled"), True)), "last_indexed_at": now}
            row_id = find_id(conn, "sources", "uid", sid)
            if row_id is None:
                row_id = find_id(conn, "sources", "path", root)
            # `mount` is PiTV's own: where this machine finds the share. pitv_content may index
            # it from somewhere else entirely, so the import must not overwrite it.
            mount = as_text(conn.execute("SELECT mount FROM sources WHERE id = ?", (row_id,)).fetchone()["mount"]) \
                if row_id is not None else None
            sources[sid] = {"id": _save(conn, "sources", row_id, fields), "category": category,
                            "location": location, "root": root, "mount": mount}
            counts["sources"] += 1
        if complete:
            listed = {s["id"] for s in sources.values()}
            conn.executemany("UPDATE sources SET enabled = 0 WHERE id = ?",
                             [(r["id"],) for r in conn.execute("SELECT id FROM sources WHERE enabled = 1").fetchall()
                              if r["id"] not in listed])

        for sh in shows_in:
            uid = as_text(sh.get("uid")) if isinstance(sh, dict) else None
            src = sources.get(as_text(sh.get("source")) or "") if uid else None
            if not uid or src is None:
                reject("show", uid, "no uid or an unknown source")
                continue
            genres = genre_list(sh.get("genres"))
            raw_category = sh.get("category") if sh.get("category") in SOURCE_CATEGORIES else src["category"]
            kids = int(raw_category == "kids" or as_bool(sh.get("kids")) or genre_rules.is_childrens(genres))
            category = genre_rules.scheduling_class(raw_category, genres)
            year = as_int(sh.get("year"))
            fields = {"source_id": src["id"], "path": uid, "title": as_text(sh.get("title")) or uid,
                      "year": year, "certificate": normalise_cert(as_text(sh.get("certificate"))), "genres": json.dumps(genres),
                      "plot": as_text(sh.get("plot")), "kids": kids, "category": category, "missing": 0,
                      "updated_at": now}
            row_id = find_id(conn, "shows", "path", uid)
            if row_id is None:
                # A series first seen before index uids (a scan-era folder path, or one fetched
                # online) keeps its id, and with it its line-up entry and history.
                legacy = conn.execute("SELECT id FROM shows WHERE path NOT LIKE 'show:%' AND lower(title) = lower(?)"
                                      " AND year IS ?", (fields["title"], year)).fetchone()
                row_id = legacy["id"] if legacy else None
            try:
                show_ids[uid] = _save(conn, "shows", row_id, fields)
            except sqlite3.IntegrityError as exc:
                reject("show", uid, str(exc))
                continue
            counts["shows"] += 1

        for it in items_in:
            uid = as_text(it.get("uid")) if isinstance(it, dict) else None
            if not uid:
                reject("item", None, "no uid")
                continue
            kind = it.get("kind")
            src = sources.get(as_text(it.get("source")) or "")
            path = it["path"] if isinstance(it.get("path"), str) else None
            if path and src is not None:
                path = local_path(path, src["root"], src["mount"])
            show_id = show_ids.get(as_text(it.get("show_uid")) or "") if kind == "episode" else None
            if kind not in KINDS or src is None or not path or (kind == "episode" and show_id is None):
                reject("item", uid, "unknown kind, source or series, or no path")
                continue
            vcodec = as_text(it.get("vcodec"))
            duration = as_float(it.get("duration"))
            concert = it.get("concert")
            fields = {
                "uid": uid, "source_id": src["id"], "kind": kind, "show_id": show_id,
                "season": as_int(it.get("season")), "episode": as_int(it.get("episode")),
                "title": as_text(it.get("title")) or uid, "year": as_int(it.get("year")), "path": path,
                "origin": src["location"], "size": as_int(it.get("size")), "mtime": as_int(it.get("mtime")),
                "duration": duration, "vcodec": vcodec, "acodec": as_text(it.get("acodec")),
                "width": as_int(it.get("width")), "height": as_int(it.get("height")),
                "interlaced": int(as_bool(it.get("interlaced"))), "hwdec": int((vcodec or "") in PI_HW_CODECS),
                "certificate": normalise_cert(as_text(it.get("certificate"))), "genres": json.dumps(genre_list(it.get("genres"))),
                "plot": as_text(it.get("plot")), "channel_hint": as_int(it.get("channel_hint")),
                "artist": as_text(it.get("artist")),
                "concert": int(as_bool(concert) if concert is not None
                               else kind == "music" and (duration or 0) >= CONCERT_MINUTES * 60),
                "family_safe": family_safe(it, unsafe) if kind == "advert" else 1,
                "missing": 0, "updated_at": now,
            }
            fields["attention"] = _attention(fields)
            if src["location"] == "cache":
                fields["cache_path"] = path   # already where playback wants it
            row_id = find_id(conn, "media", "uid", uid)
            if row_id is None:
                row_id = find_id(conn, "media", "path", path)
            try:
                saved_id = _save(conn, "media", row_id, fields)
                _refresh_attention(conn, saved_id)
            except sqlite3.IntegrityError as exc:   # e.g. its path already belongs to another uid
                reject("item", uid, str(exc))
                continue
            counts["items"] += 1
            counts["new"] += row_id is None
            seen.add(uid)

        assign_ident_channels(conn)
        if complete:
            # Online material is managed through delivery reports, not the index.
            gone = [(r["id"],) for r in conn.execute("SELECT id, uid FROM media WHERE missing = 0 AND origin != 'online'")
                    .fetchall() if r["uid"] not in seen]
            conn.executemany("UPDATE media SET missing = 1 WHERE id = ?", gone)
            counts["missing"] = len(gone)
            conn.execute("UPDATE shows SET missing = 1 WHERE missing = 0 AND id NOT IN"
                         " (SELECT DISTINCT show_id FROM media WHERE show_id IS NOT NULL AND missing = 0)")
        per_source = {r["source_id"]: r["n"] for r in conn.execute(
            "SELECT source_id, COUNT(*) AS n FROM media WHERE missing = 0 AND source_id IS NOT NULL GROUP BY source_id")}
        conn.executemany("UPDATE sources SET index_summary = ? WHERE id = ?",
                         [(f"{per_source.get(s['id'], 0)} items in the index", s["id"]) for s in sources.values()])
    if rejects:
        log.warning("library index: %d records rejected, first: %s", counts["rejected"], rejects[0])
    return {**counts, "rejects": rejects}


def local_path(path: str, root: str, mount: str | None) -> str:
    """An indexed path as this machine sees it.

    pitv_content publishes the path it indexed. PiTV may mount the same share somewhere else
    (a desktop without /mnt, or the two running on different machines), so a source may carry
    its own mount point and the items under it are filed beneath it."""
    if not mount or not root or not path.startswith(root):
        return path
    return mount.rstrip("/") + path[len(root.rstrip("/")):]


def import_and_place(conn: sqlite3.Connection, doc: dict[str, Any], origin: str = "") -> dict[str, Any]:
    """Import, then place new series and films into line-ups and refresh the JSON mirror.
    Logged as a `catalogue` run; a failed import is logged as an error and re-raised."""
    run_id = run_log_start(conn, "catalogue")
    try:
        counts = import_index(conn, doc)
        restore_if_empty(conn)
        placed = generate(conn)
    except Exception as exc:
        run_log_finish(conn, run_id, "error", str(exc), [str(exc)])
        raise
    write_mirror(conn)
    # A day built before this material arrived may contain whole-day filler or holding cards
    # inside strict bands. Revisit those gaps now so fetched material is on the schedule before
    # airtime rather than merely present in the catalogue.
    refill = refill_empty_days(conn)
    summary = (f"{counts['items']} items ({counts['new']} new, {counts['missing']} now missing,"
               f" {counts['rejected']} rejected) from {counts['sources']} sources; line-ups: {placed['assigned']} placed,"
               f" {placed['unmatched']} matched no channel; {refill['summary']}")
    details = ([f"source: {origin}"] if origin else []) + [f"rejected {r}" for r in counts["rejects"]]
    run_log_finish(conn, run_id, "warning" if counts["rejected"] or placed["unmatched"] else "ok", summary, details)
    log.info("catalogue import: %s", summary)
    return {**counts, **{f"lineup_{k}": v for k, v in placed.items()}, "refilled_days": refill["days"],
            "summary": summary, "run_id": run_id}


def _title_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _compatible_candidate(kind: str, title: str, year: int | None,
                          candidate: dict[str, Any]) -> bool:
    """Conservative automatic identity check: never enrich a merely fuzzy search result."""
    if _title_key(candidate.get("title")) != _title_key(title):
        return False
    found = as_int(candidate.get("year"))
    if not year or not found:
        return True
    if kind == "movie":
        return abs(found - year) <= 1
    end = as_int(candidate.get("end_year")) or found
    return found - 1 <= year <= end + 1


def _lookup_metadata(settings: dict[str, Any], kind: str, title: str,
                     year: int | None) -> tuple[dict[str, Any] | None, str]:
    query = urllib.parse.urlencode({"kind": kind, "title": title, "year": year or "", "limit": 8})
    status, payload = tool_client.request(tool_client.base_url(settings), "GET", "lookup", query=query, timeout=75)
    if status != 200 or not isinstance(payload, dict):
        reason = payload.get("error") if isinstance(payload, dict) else f"HTTP {status}"
        return None, str(reason or f"HTTP {status}")
    for candidate in payload.get("candidates") or []:
        if not isinstance(candidate, dict) or not _compatible_candidate(kind, title, year, candidate):
            continue
        cert = normalise_cert(as_text(candidate.get("certificate")))
        if cert:
            return {**candidate, "certificate": cert}, ""
    errors = payload.get("errors") or {}
    return None, "; ".join(f"{k}: {v}" for k, v in errors.items()) or "no unambiguous rated match"


def enrich_missing_metadata(conn: sqlite3.Connection, *, limit: int = 50, force: bool = False,
                            progress: Any = None) -> dict[str, Any]:
    """Look up missing certificates through pitv_content and retain trusted matches.

    Shows are checked once (episodes inherit their series certificate); films are checked
    individually.  Failed/ambiguous checks are dated so routine imports do not hammer providers,
    while an explicit force run may retry them.
    """
    cutoff = now_ts() - METADATA_RECHECK_DAYS * 86400
    items: list[dict[str, Any]] = []
    for table, kind in (("shows", "show"), ("media", "movie")):
        where = "missing = 0" if table == "shows" else "missing = 0 AND kind = 'movie'"
        for row in conn.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY title").fetchall():
            raw = row_to_dict(row) or {}
            if normalise_cert(as_text(effective(raw).get("certificate"))):
                continue
            checked = as_int(raw.get("metadata_checked_at")) or 0
            if not force and checked >= cutoff:
                continue
            items.append({"table": table, "kind": kind, "row": raw})
    # Family material first: it suffers most from the conservative unknown-film fallback.
    items.sort(key=lambda i: (not (i["row"].get("kids") or genre_rules.is_childrens(i["row"].get("genres") or [])),
                              i["kind"] != "movie", str(i["row"].get("title") or "").casefold()))
    total = len(items)
    items = items[:max(0, limit)]
    settings = all_settings(conn)
    found = checked = 0
    notes: list[str] = []
    for i, item in enumerate(items, 1):
        row, table, kind = item["row"], item["table"], item["kind"]
        if progress:
            progress(f"checking ratings: {row['title']}", i - 1, len(items))
        candidate, reason = _lookup_metadata(settings, kind, row["title"], as_int(row.get("year")))
        checked += 1
        now = now_ts()
        if candidate:
            enriched = row.get("enriched") if isinstance(row.get("enriched"), dict) else {}
            genres = genre_list([*(effective(row).get("genres") or []), *(candidate.get("genres") or [])])
            enriched = {**enriched, "certificate": candidate["certificate"]}
            if genres:
                enriched["genres"] = genres
            if not effective(row).get("plot") and candidate.get("summary"):
                enriched["plot"] = str(candidate["summary"])
            if table == "shows":
                enriched["kids"] = int(bool(effective(row).get("kids")) or genre_rules.is_childrens(genres))
            source = str((candidate.get("match") or {}).get("source") or "online")
            with tx(conn):
                conn.execute(f"UPDATE {table} SET enriched = ?, metadata_checked_at = ?, metadata_source = ? WHERE id = ?",
                             (json.dumps(enriched), now, source, row["id"]))
                if table == "media":
                    _refresh_attention(conn, row["id"])
            found += 1
        else:
            conn.execute(f"UPDATE {table} SET metadata_checked_at = ?, metadata_source = NULL WHERE id = ?",
                         (now, row["id"]))
            if len(notes) < REJECTS_KEPT:
                notes.append(f"{row['title']}: {reason}")
    if progress:
        progress(f"ratings: {found} found from {checked} checked", checked, len(items))
    return {"checked": checked, "found": found, "remaining": max(0, total - checked), "notes": notes,
            "summary": f"Found {found} missing certificate{'s' if found != 1 else ''} from {checked} online check{'s' if checked != 1 else ''}."}


def refresh(conn: sqlite3.Connection, reindex: bool = False) -> dict[str, Any]:
    """Fetch pitv_content's index (API first, file second) and import it. A missing or malformed
    index is reported as {"status": "error"} rather than raised, so the maintenance pass that
    calls this carries on with its other work."""
    doc, origin = fetch_index(all_settings(conn), reindex=reindex)
    if doc is None:
        run_id = run_log_start(conn, "catalogue")
        run_log_finish(conn, run_id, "error", origin, [origin])
        log.error("catalogue refresh failed: %s", origin)
        return {"status": "error", "summary": origin}
    try:
        result = import_and_place(conn, doc, origin)
    except IndexFormatError as exc:
        log.error("catalogue refresh failed: %s: %s", origin, exc)
        return {"status": "error", "summary": f"{origin}: {exc}"}
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
        " COUNT(m.id) AS episodes, COUNT(m.cache_path) AS cached"
        " FROM shows sh LEFT JOIN media m ON m.show_id = sh.id AND m.missing = 0"
        " LEFT JOIN channels c ON c.id = sh.home_channel_id WHERE sh.missing = 0 GROUP BY sh.id ORDER BY sh.title"))
    films = rows_to_dicts(conn.execute(
        "SELECT m.id, m.title, m.year, m.genres, m.certificate, m.origin, m.duration, c.number AS channel,"
        " m.cache_path IS NOT NULL AS cached FROM media m LEFT JOIN channels c ON c.id = m.home_channel_id"
        " WHERE m.kind = 'movie' AND m.missing = 0 ORDER BY m.title"))
    custom = rows_to_dicts(conn.execute(
        "SELECT l.title, l.year, l.kind, l.transient, l.episode_minutes, c.number AS channel FROM lineup l"
        " JOIN channels c ON c.id = l.channel_id WHERE l.source != 'library' ORDER BY l.title"))
    return {"schema": 1, "generated_ts": now_ts(), "shows": shows, "films": films, "custom": custom}


def write_mirror(conn: sqlite3.Connection) -> None:
    """catalogue.json beside the database."""
    write_data_file(conn, MIRROR, lambda: export(conn))


def last_import(conn: sqlite3.Connection) -> dict[str, Any] | None:
    return row_to_dict(conn.execute("SELECT * FROM run_log WHERE kind = 'catalogue' ORDER BY id DESC LIMIT 1").fetchone())
