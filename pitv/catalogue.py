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
from .scheduler.rules import keyword_pattern, names_a_product, normalise_cert

log = logging.getLogger("pitv.catalogue")

SCHEMA = 2
SOURCE_TYPES = ("tv", "movie", "advert", "ident", "music")
KINDS = ("episode", "movie", "advert", "ident", "music")
SOURCE_CATEGORIES = ("general", "sport", "kids")
# How long to wait for a re-index before importing the index in hand. The figure is what the
# work takes, not what a queue might do with it: a full walk of a seventeen thousand file library
# runs in seconds to a few minutes. Waiting half an hour was measuring the wrong thing, and it
# cost forty minutes of a rebuild one evening for an index that had never started.
REINDEX_TIMEOUT = 300         # it is running: give it time to finish
REINDEX_QUEUED = 20           # it has not started: it is behind other work, which may be hours
REINDEX_POLL = 3
REINDEX_UNSEEN = 30           # it is not in the job list at all: nobody can see it, so do not wait
CONCERT_MINUTES = 35          # a music item this long is a concert even when not tagged
UNSAFE_TAGS = {"alcohol", "tobacco", "adult", "gambling", "18"}
REJECTS_KEPT = 50             # rejected records described in the run log; the rest are only counted
METADATA_RECHECK_DAYS = 30
# A re-release or a festival date moves a year by one or two; more than that between the library
# and the programme an NFO's identifier names is a different programme.
ID_YEAR_TOLERANCE = 2
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
    is imported regardless.

    Three things can be waited on and only one of them is worth waiting for. An index that is
    running is nearly done, and gets `REINDEX_TIMEOUT`. One that is queued has not started and
    may be behind hours of delivery, so it gets `REINDEX_QUEUED` and no more: the index in hand
    is minutes old, because maintenance imports every rewrite, and a rebuild from that is far
    better than a rebuild half an hour late. One that is not in the job list at all cannot be
    observed, which a deduplicated reply naming a long-dead request produces, and gets
    `REINDEX_UNSEEN`.

    In every case the index already published is imported and the build goes ahead; none of this
    fails, it only decides how long to hope for something fresher."""
    status, started = tool_client.request(base, "POST", "index", body={}, timeout=10)
    job_id = started.get("job_id") if status == 200 and isinstance(started, dict) else None
    if not job_id:
        log.warning("re-index not started (HTTP %s): %s", status, started.get("error", "") if isinstance(started, dict) else "")
        return
    start = time.monotonic()
    deadline = start + timeout
    seen = False
    while time.monotonic() < deadline:
        waited = time.monotonic() - start
        status, jobs = tool_client.request(base, "GET", "jobs", timeout=10)
        job = next((j for j in jobs if isinstance(j, dict) and j.get("job_id") == job_id), None) \
            if status == 200 and isinstance(jobs, list) else None
        if job is None:
            if not seen and waited > REINDEX_UNSEEN:
                log.warning("re-index %s is not in pitv_content's job list%s; importing the index in hand",
                            job_id, " (it was deduplicated against an older request)"
                            if isinstance(started, dict) and started.get("deduplicated") else "")
                return
            time.sleep(REINDEX_POLL)
            continue
        seen = True
        if job.get("finished_ts"):
            if job.get("status") != "ok":
                log.warning("re-index %s ended %s, no new index: %s", job_id, job.get("status"), job.get("summary", ""))
            return
        if not job.get("started_ts") and waited > REINDEX_QUEUED:
            # Queued behind other work. That queue is not PiTV's to wait on, and the index it
            # would replace is minutes old.
            log.info("re-index %s is queued behind pitv_content's other work; importing the index in hand", job_id)
            return
        time.sleep(REINDEX_POLL)
    log.warning("re-index %s still running after %ds; importing the current index", job_id, timeout)




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




def _attention(fields: dict[str, Any], unnamed: re.Pattern[str] | None = None) -> str | None:
    """Why an imported item needs a look in the admin, from its stored fields."""
    notes = []
    if fields["kind"] == "advert" and not names_a_product(fields.get("title"), unnamed):
        notes.append("No product named; not put in a break until it has one")
    if not fields["duration"]:
        notes.append("No duration in the library index")
    if fields["year"] is None and fields["kind"] in ("episode", "movie", "advert"):
        notes.append("No year found")
    if fields["kind"] == "movie" and not fields["certificate"]:
        notes.append("No certificate (treated as 15, post-watershed)")
    return "; ".join(notes) or None


def _refresh_attention(conn: sqlite3.Connection, media_id: int,
                       unnamed: re.Pattern[str] | None = None) -> None:
    """Recompute an item's note from what is known about it now: the index, an online check and
    the owner's edits. An episode with no year of its own takes its series' year, exactly as it
    does when it is scheduled, so a series the online check has dated stops flagging every one
    of its episodes at the next import."""
    row = row_to_dict(conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone())
    if not row:
        return
    known = effective(row)
    if known.get("year") is None and known.get("show_id"):
        show = row_to_dict(conn.execute("SELECT * FROM shows WHERE id = ?", (known["show_id"],)).fetchone())
        known = {**known, "year": effective(show).get("year") if show else None}
    conn.execute("UPDATE media SET attention = ? WHERE id = ?", (_attention(known, unnamed), media_id))


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
    unnamed = keyword_pattern(settings.get("unnamed_advert_keywords"))
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
                      "ids": json.dumps(_online_ids(sh.get("ids"))), "updated_at": now}
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
                "encoded": None if it.get("encoded") is None else int(as_bool(it.get("encoded"))),
                "certificate": normalise_cert(as_text(it.get("certificate"))), "genres": json.dumps(genre_list(it.get("genres"))),
                "plot": as_text(it.get("plot")), "channel_hint": as_int(it.get("channel_hint")),
                "artist": as_text(it.get("artist")),
                "concert": int(as_bool(concert) if concert is not None
                               else kind == "music" and (duration or 0) >= CONCERT_MINUTES * 60),
                "family_safe": family_safe(it, unsafe) if kind == "advert" else 1,
                "ids": json.dumps(_online_ids(it.get("ids"))), "missing": 0, "updated_at": now,
            }
            fields["attention"] = _attention(fields, unnamed)
            if src["location"] == "cache":
                fields["cache_path"] = path   # already where playback wants it
            row_id = find_id(conn, "media", "uid", uid)
            if row_id is None:
                row_id = find_id(conn, "media", "path", path)
            try:
                saved_id = _save(conn, "media", row_id, fields)
                _refresh_attention(conn, saved_id, unnamed)
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
    """A title for comparing: case, punctuation and a bracketed note ("(Directors Cut)",
    "(Original Theatrical Version)") aside, since a library names the edition and a listing
    does not."""
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"\([^)]*\)", " ", str(value or "")).casefold())


def _same_film(ours: str, theirs: str) -> bool:
    """Whether two film titles of the same year name one film. Libraries and listings differ in
    how much of a title they give ("Rogue One" and "Rogue One: A Star Wars Story", "Hotel
    Transylvania 3" and "Hotel Transylvania 3: Summer Vacation"), so one may open the other,
    provided the shorter is long enough to mean something. Only ever used with an exact year."""
    a, b = sorted((_title_key(ours), _title_key(theirs)), key=len)
    return bool(a) and (a == b or (len(a) >= 6 and b.startswith(a)))


def _compatible_candidate(kind: str, title: str, year: int | None,
                          candidate: dict[str, Any]) -> bool:
    """Conservative automatic identity check: never enrich a merely fuzzy search result."""
    found = as_int(candidate.get("year"))
    same_film = kind == "movie" and year and found == year and _same_film(title, as_text(candidate.get("title")) or "")
    if _title_key(candidate.get("title")) != _title_key(title) and not same_film:
        return False
    if not year or not found:
        return True
    if kind == "movie":
        return abs(found - year) <= 1
    end = as_int(candidate.get("end_year")) or found
    return found - 1 <= year <= end + 1


# What each identifier looks like (contract section 1). The index is a document another process
# wrote and these values go back out in a query, so anything else is dropped on import.
_ID_SHAPES = {"imdb": re.compile(r"tt\d{5,10}"), "tmdb": re.compile(r"\d{1,9}"), "tvdb": re.compile(r"\d{1,9}")}


def _online_ids(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {key: str(value[key]) for key, shape in _ID_SHAPES.items()
            if isinstance(value.get(key), (str, int)) and shape.fullmatch(str(value[key]))}


def _ask_lookup(settings: dict[str, Any], params: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    status, payload = tool_client.request(tool_client.base_url(settings), "GET", "lookup",
                                          query=urllib.parse.urlencode(params), timeout=75)
    if status != 200 or not isinstance(payload, dict):
        reason = payload.get("error") if isinstance(payload, dict) else f"HTTP {status}"
        return None, str(reason or f"HTTP {status}")
    return payload, ""


def _put_together(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """One programme's facts from every source that knows it: the first value found for each
    single fact, every genre any of them gives."""
    found: dict[str, Any] = {}
    genres: list[str] = []
    for candidate in candidates:
        found.setdefault("match", candidate.get("match"))
        if (cert := normalise_cert(as_text(candidate.get("certificate")))) and "certificate" not in found:
            found["certificate"] = cert
        if (when := as_int(candidate.get("year"))) and "year" not in found:
            found["year"] = when
        if candidate.get("summary") and "summary" not in found:
            found["summary"] = candidate["summary"]
        if (where := as_text(candidate.get("network"))) and "network" not in found:
            # Who first broadcast it. A channel modelled on a real one is placed from this.
            found["network"] = where
        genres += [g for g in (candidate.get("genres") or []) if isinstance(g, str)]
    return {**found, "genres": genres} if found else None


def _lookup_metadata(settings: dict[str, Any], kind: str, title: str, year: int | None,
                     seasons: int = 0, ids: dict[str, str] | None = None) -> tuple[dict[str, Any] | None, str]:
    # The NFO's identifier names the programme outright, whatever the library calls it (an
    # edition note, a translated or shortened title), so it is asked first and its answer needs
    # no title matching. A year far from the library's means the NFO holds the wrong
    # identifier, and the title search below is the safer reading.
    if ids:
        payload, _ = _ask_lookup(settings, {"kind": kind, **ids, "limit": 8})
        named = [c for c in (payload or {}).get("candidates") or [] if isinstance(c, dict)
                 and not (year and (theirs := as_int(c.get("year"))) and abs(theirs - year) > ID_YEAR_TOLERANCE)]
        if named and (found := _put_together(named)):
            return found, ""
    payload, reason = _ask_lookup(settings, {"kind": kind, "title": title, "year": year or "", "limit": 8})
    if payload is None:
        return None, reason
    # Sources know different things about the same title (one has the certificate, another the
    # genres), so what the compatible matches say is put together. A match with no certificate
    # still fills a missing year and genres, which is what decides where a series belongs.
    # One title can be several programmes (an original and its remake). With no year to go by,
    # a run too short for the seasons on disk is ruled out, and of what is left the earliest is
    # taken, the original being what a library of period television is likelier to hold. Only
    # matches for that one programme are put together; mixing them mixes two programmes' facts.
    compatible = [c for c in payload.get("candidates") or []
                  if isinstance(c, dict) and _compatible_candidate(kind, title, year, c)]
    if not year:
        def fits(c: dict[str, Any]) -> bool:
            first, last = as_int(c.get("year")), as_int(c.get("end_year"))
            return not (seasons > 1 and first and last and last - first + 2 < seasons)
        dated = sorted({y for c in compatible if fits(c) and (y := as_int(c.get("year")))})
        if dated:
            compatible = [c for c in compatible if fits(c) and abs((as_int(c.get("year")) or dated[0]) - dated[0]) <= 1]
    if found := _put_together(compatible):
        return found, ""
    errors = payload.get("errors") or {}
    return None, "; ".join(f"{k}: {v}" for k, v in errors.items()) or "no unambiguous match"


def enrich_missing_metadata(conn: sqlite3.Connection, *, limit: int = 50, force: bool = False,
                            progress: Any = None) -> dict[str, Any]:
    """Look up what the index could not say (certificate, year, genres) through pitv_content and
    retain trusted matches.

    Shows are checked once (episodes inherit their series' certificate and year); films are
    checked individually. Failed or ambiguous checks are dated so routine imports do not hammer
    providers, while an explicit force run may retry them. A series that learns its genres is
    given a channel again, since it was placed without them, and its episodes stop being flagged
    for a year the series now has.

    A series whose broadcaster is unknown is also worth asking about, because the channels model
    real ones and a programme is placed on the channel it went out on. Films are not: nobody
    broadcast them first.
    """
    cutoff = now_ts() - METADATA_RECHECK_DAYS * 86400
    items: list[dict[str, Any]] = []
    for table, kind in (("shows", "show"), ("media", "movie")):
        where = "missing = 0" if table == "shows" else "missing = 0 AND kind = 'movie'"
        for row in conn.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY title").fetchall():
            raw = row_to_dict(row) or {}
            known = effective(raw)
            complete = (normalise_cert(as_text(known.get("certificate"))) and known.get("year")
                        and genre_list(known.get("genres")))
            if complete and (table == "media" or known.get("network")):
                continue
            checked = as_int(raw.get("metadata_checked_at")) or 0
            if not force and checked >= cutoff:
                continue
            items.append({"table": table, "kind": kind, "row": raw})
    # Family material first: it suffers most from the conservative unknown-film fallback. Then
    # series before films, since one answer settles hundreds of episodes, and a series the index
    # knew nothing about (no genres) before one that only lacks a certificate: until it is
    # identified it cannot even be given the right channel.
    items.sort(key=lambda i: (not (i["row"].get("kids") or genre_rules.is_childrens(i["row"].get("genres") or [])),
                              i["kind"] == "movie", bool(genre_list(effective(i["row"]).get("genres"))),
                              str(i["row"].get("title") or "").casefold()))
    total = len(items)
    items = items[:max(0, limit)]
    settings = all_settings(conn)
    found = checked = 0
    replace = False
    notes: list[str] = []
    for i, item in enumerate(items, 1):
        row, table, kind = item["row"], item["table"], item["kind"]
        if progress:
            progress(f"checking ratings: {row['title']}", i - 1, len(items))
        seasons = (conn.execute("SELECT COUNT(DISTINCT season) FROM media WHERE show_id = ? AND missing = 0 AND season > 0",
                                (row["id"],)).fetchone()[0] if table == "shows" else 0)
        candidate, reason = _lookup_metadata(settings, kind, row["title"], as_int(row.get("year")), seasons,
                                             ids=_online_ids(row.get("ids")))
        checked += 1
        now = now_ts()
        if candidate:
            enriched = row.get("enriched") if isinstance(row.get("enriched"), dict) else {}
            had_genres = bool(genre_list(effective(row).get("genres")))
            genres = genre_list([*(effective(row).get("genres") or []), *(candidate.get("genres") or [])])
            if candidate.get("certificate") and not normalise_cert(as_text(effective(row).get("certificate"))):
                enriched = {**enriched, "certificate": candidate["certificate"]}
            if candidate.get("year") and not effective(row).get("year"):
                enriched = {**enriched, "year": candidate["year"]}
            if genres:
                enriched["genres"] = genres
            if not effective(row).get("plot") and candidate.get("summary"):
                enriched["plot"] = str(candidate["summary"])
            if table == "shows":
                enriched["kids"] = int(bool(effective(row).get("kids")) or genre_rules.is_childrens(genres))
                if candidate.get("network") and not effective(row).get("network"):
                    enriched["network"] = str(candidate["network"])
            source = str((candidate.get("match") or {}).get("source") or "online")
            with tx(conn):
                conn.execute(f"UPDATE {table} SET enriched = ?, metadata_checked_at = ?, metadata_source = ? WHERE id = ?",
                             (json.dumps(enriched), now, source, row["id"]))
                if table == "media":
                    _refresh_attention(conn, row["id"], keyword_pattern(settings.get("unnamed_advert_keywords")))
                else:
                    if enriched.get("year"):
                        # The series has a year now, which its episodes inherit when scheduled.
                        conn.execute("UPDATE media SET attention = NULLIF(TRIM(REPLACE(REPLACE(attention, 'No year found; ', ''),"
                                     " 'No year found', ''), '; '), '') WHERE show_id = ? AND attention LIKE '%No year found%'",
                                     (row["id"],))
                    if genres and not had_genres:
                        # Placed when nothing was known about it; let the line-up place it again.
                        conn.execute("DELETE FROM lineup WHERE show_id = ? AND pinned = 0 AND source = 'library'", (row["id"],))
                        replace = True
            found += 1
        else:
            conn.execute(f"UPDATE {table} SET metadata_checked_at = ?, metadata_source = NULL WHERE id = ?",
                         (now, row["id"]))
            if len(notes) < REJECTS_KEPT:
                notes.append(f"{row['title']}: {reason}")
    if replace:
        from . import lineup
        lineup.generate(conn)
    counted = learn_from_matches(conn, settings, limit=limit, progress=progress)
    if progress:
        progress(f"ratings: {found} found from {checked} checked", checked, len(items))
    return {"checked": checked, "found": found, "remaining": max(0, total - checked), "notes": notes,
            "episode_counts": counted,
            "summary": f"Filled in {found} title{'s' if found != 1 else ''} from {checked} online check{'s' if checked != 1 else ''}."
                       + (f" Learned the length or certificate of {counted} remote title{'s' if counted != 1 else ''}." if counted else "")}


def learn_from_matches(conn: sqlite3.Connection, settings: dict[str, Any], *, limit: int = 50,
                       progress: Any = None) -> int:
    """What the online match knows about each remote title that the line-up entry does not yet
    hold: how long a series ran, and its certificate. The entry's confirmed match names the
    programme, so the lookup is asked by title and only candidates for that programme are
    believed (the same source and id, or the same IMDb id: the source with the episode list
    often has no certificate and another has). Without the length PiTV asks for episodes never
    made; without the certificate a late-night series nobody holds yet may air at breakfast."""
    entries = rows_to_dicts(conn.execute(
        "SELECT id, kind, title, year, match, episode_count, certificate FROM lineup WHERE source != 'library'"
        " AND match IS NOT NULL AND ((kind = 'show' AND episode_count IS NULL) OR certificate IS NULL)"
        " ORDER BY certificate IS NOT NULL, id LIMIT ?", (max(0, limit),)))
    learned = 0
    for i, entry in enumerate(entries, 1):
        if progress:
            progress(f"checking the online match for {entry['title']}", i - 1, len(entries))
        match = json.loads(entry["match"]) if isinstance(entry["match"], str) else (entry["match"] or {})
        payload, _ = _ask_lookup(settings, {"kind": entry["kind"], "title": entry["title"], "year": entry["year"] or "", "limit": 8})
        found: dict[str, Any] = {}
        for candidate in (payload or {}).get("candidates") or []:
            theirs = candidate.get("match") if isinstance(candidate, dict) else None
            if not isinstance(theirs, dict):
                continue
            same = ((theirs.get("source") == match.get("source") and str(theirs.get("id")) == str(match.get("id")))
                    or (match.get("imdb") and theirs.get("imdb") == match.get("imdb")))
            if not same:
                continue
            if entry["kind"] == "show" and entry["episode_count"] is None and (count := as_int(candidate.get("episodes"))):
                found.setdefault("episode_count", count)
            if entry["certificate"] is None and (cert := normalise_cert(as_text(candidate.get("certificate")))):
                found.setdefault("certificate", cert)
        if found:
            with tx(conn):
                update_row(conn, "lineup", entry["id"], {**found, "updated_at": now_ts()})
            learned += 1
    return learned


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
