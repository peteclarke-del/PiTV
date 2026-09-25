"""One report on the state of the whole television, for whoever has to find out what is wrong.

`pitv doctor` on the machine, or `GET /api/doctor` from the admin, gathers what would otherwise
take a dozen commands over SSH: the services, what the player is doing, holes in the schedule,
how much of the next day is in the cache, which bands are short, what pitv_content is working
on and which of its sources it cannot read, the last warnings from every log, and the disks. It
opens with the findings, in plain sentences, so the first screen says whether anything needs
attention.

It only reads. Every section stands alone: one that cannot be gathered says why and the rest
still are, because the report matters most when something is broken. It deliberately offers no
way to change anything and nothing listens for it: remote support is SSH to the machine
(docs/PLAN.md, Remote support), and this is the first thing to run once there."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__, tool_client
from .config import Config
from .content import HELD as HELD_PREFIX
from .content import UNREACHED as UNREACHED_PREFIX
from .db import all_settings, as_int, now_ts, rows_to_dicts
from .hostinfo import host_info
from .logsetup import log_dir, tail
from .player.cache import MediaCache
from .player.hwdec import is_raspberry_pi
from .scheduler.rules import keyword_pattern, names_a_product, tz_of

DAY = 86400
CACHED_TARGET = 95      # per cent of the next day's files expected in the cache
CAP_DAYS_TARGET = 1.5   # days of built schedule the cache should hold; below this it thrashes
LOW_DISK_BYTES = 5 * 1024 ** 3
LOG_LINES = 12
STARVED_HISTORY = 20    # how far back a streak is counted; past this it is reported as "at least"
# Mirrors content.MAX_WANTED_ATTEMPTS for the finding text; imported lazily where it is read.
MAX_ATTEMPTS = 3


def report(conn: sqlite3.Connection, cfg: Config, now: int | None = None) -> dict[str, Any]:
    """The whole report as a document: `findings` first, then one key per section."""
    now = now or now_ts()
    settings = all_settings(conn)
    sections: dict[str, Callable[[], Any]] = {
        "host": lambda: host_info(__version__, {}),
        "services": _services,
        "player": lambda: _player(cfg),
        "schedule": lambda: _schedule(conn, now),
        "cache": lambda: _cache(conn, settings, now),
        "bands": lambda: _bands(conn, settings, now),
        "library": lambda: _library(conn),
        "fragile_matches": lambda: _fragile_matches(conn),
        "providers": lambda: _providers(conn, settings),
        "wanted": lambda: _wanted(conn),
        "starved": lambda: _starved(conn),
        "held_too_long": lambda: _held_too_long(conn),
        "abandoned_runs": lambda: _abandoned(conn, settings),
        "runs": lambda: _runs(conn),
        "content": lambda: _content(settings),
        "unreadable_sources": lambda: _unreadable_sources(settings),
        "logs": lambda: _logs(cfg),
        "disks": lambda: _disks(cfg, settings),
    }
    doc: dict[str, Any] = {"generated_ts": now, "version": __version__, "timezone": str(tz_of(conn))}
    for name, gather in sections.items():
        try:
            doc[name] = gather()
        except Exception as exc:  # noqa: BLE001 - a broken section is a finding, never the end of the report
            doc[name] = {"error": f"{type(exc).__name__}: {exc}"}
    doc = {"findings": _findings(doc), **doc}
    return doc


# --- sections -------------------------------------------------------------------------------

def _services() -> list[dict[str, Any]]:
    from .web.api.services import DEV_UNITS, UNITS, systemd_state
    on_pi = is_raspberry_pi()
    names = [u.unit for u in UNITS] if on_pi else list(DEV_UNITS.values())
    states = systemd_state(names, user=not on_pi)
    return [{"unit": n, "loaded": (states.get(n) or {}).get("LoadState"), "active": (states.get(n) or {}).get("ActiveState"),
             "sub": (states.get(n) or {}).get("SubState"), "result": (states.get(n) or {}).get("Result"),
             "restarts": (states.get(n) or {}).get("NRestarts")} for n in names]


def _player(cfg: Config) -> dict[str, Any]:
    from .web.player_client import PlayerClient
    state = PlayerClient(cfg.player_socket).state()
    slot = state.get("slot") or {}
    return {"online": not state.get("offline"), "error": state.get("error") or "",
            "channel": (state.get("channel") or {}).get("name"), "showing": slot.get("title"), "kind": slot.get("kind")}


def _schedule(conn: sqlite3.Connection, now: int) -> dict[str, Any]:
    horizon = conn.execute("SELECT MAX(end_ts) FROM schedule").fetchone()[0]
    rows = conn.execute(
        "SELECT c.number, c.name, s.day,"
        " SUM(s.kind = 'programme' AND s.replay = 0) AS programmes,"
        " SUM(CASE WHEN s.kind = 'filler' AND s.replay = 0 AND s.block IS NULL THEN s.end_ts - s.start_ts ELSE 0 END) / 60 AS hole_minutes,"
        " SUM(CASE WHEN s.kind = 'filler' AND s.replay = 0 AND s.block IS NOT NULL THEN s.end_ts - s.start_ts ELSE 0 END) / 60 AS band_card_minutes,"
        " SUM(s.media_id IS NULL AND s.wanted_id IS NOT NULL AND s.replay = 0) AS placeholders"
        " FROM schedule s JOIN channels c ON c.id = s.channel_id WHERE s.end_ts > ? AND c.enabled = 1"
        " GROUP BY c.id, s.day ORDER BY s.day, c.number", (now,)).fetchall()
    return {"horizon_end_ts": horizon, "days_ahead": round((horizon - now) / DAY, 1) if horizon else 0,
            "channel_days": [dict(r) for r in rows]}


def _cache(conn: sqlite3.Connection, settings: dict[str, Any], now: int) -> dict[str, Any]:
    from .content import manifest
    doc = manifest(conn, days=1, now=now)
    # Fetches are listed for the whole schedule; the cache is judged on the manifest window.
    items = [i for i in doc["items"] if i["first_air_ts"] < doc["horizon_ts"]]
    waiting: dict[str, int] = {}
    for item in items:
        if not item.get("already_cached"):
            waiting[item["action"]] = waiting.get(item["action"], 0) + 1
    cached = sum(1 for i in items if i.get("already_cached"))
    cache = MediaCache.from_settings(settings)
    return {"usage": cache.usage(), "next_day_files": len(items), "cached": cached,
            "cached_percent": round(100 * cached / len(items)) if items else 100, "waiting_by_action": waiting,
            **_cap_against_schedule(conn, cache.max_bytes, now)}


def _cap_against_schedule(conn: sqlite3.Connection, cap: int, now: int) -> dict[str, Any]:
    """How much of the built schedule the cache cap can hold.

    A cap smaller than the schedule does not fail: the copies beyond it are evicted and those
    slots play from the NAS, which is the designed fallback. It is expensive and invisible,
    though, because each of those copies is made again the next time its slot comes round, and
    nothing in a delivery report shows it: a run only ever sees what is missing now. Stated as
    days, it answers the question a person actually has, which is whether to find more disk.

    What is left after a day of schedule matters as much as the number of days, because fetched
    episodes are kept so that a later airing costs nothing: that is the one part of the cache
    meant to grow. A cap that holds the schedule but leaves less room than the kept material
    already occupies is about to start evicting the very thing it was keeping.

    Sizes are GiB throughout, the unit `cache_max_gb` is set in and `df -h` prints."""
    day = conn.execute(
        "SELECT COALESCE(SUM(size), 0) FROM (SELECT DISTINCT m.id, m.size FROM schedule s"
        " JOIN media m ON m.id = s.media_id WHERE s.start_ts BETWEEN ? AND ? AND m.size > 0)",
        (now, now + DAY)).fetchone()[0]
    kept = conn.execute(
        "SELECT COALESCE(SUM(size), 0) FROM media WHERE origin IN ('cache', 'online')"
        " AND missing = 0 AND size > 0").fetchone()[0]
    if not cap or not day:
        return {}
    return {"schedule_day_bytes": int(day), "kept_bytes": int(kept), "cap_holds_days": round(cap / day, 1),
            "room_for_kept_bytes": int(cap - day)}


def _bands(conn: sqlite3.Connection, settings: dict[str, Any], now: int) -> dict[str, Any]:
    from .wanted import band_needs, unairable
    cards = conn.execute(
        "SELECT c.name AS channel, s.block AS band, s.day, (s.end_ts - s.start_ts) / 60 AS card_minutes"
        " FROM schedule s JOIN channels c ON c.id = s.channel_id WHERE s.kind = 'filler' AND s.block IS NOT NULL"
        " AND s.replay = 0 AND s.end_ts > ? AND s.start_ts < ? ORDER BY s.start_ts", (now, now + 2 * DAY)).fetchall()
    needs = [{"channel": n["channel"]["name"], "band": n["band"].name, "kind": n["kind"], "have": n["have"], "want": n["want"]}
             for n in band_needs(conn, settings)]
    return {"holding_cards_next_two_days": [dict(r) for r in cards], "due_a_top_up": needs,
            "no_band_can_air": unairable(conn, settings)}


def _fragile_matches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Line-up entries keyed on a name their creator could change (`youtube.survives_a_rename`)."""
    from . import youtube
    out = []
    for r in rows_to_dicts(conn.execute(
            "SELECT l.title, l.match, c.name AS channel FROM lineup l JOIN channels c ON c.id = l.channel_id"
            " WHERE l.enabled = 1 AND l.match IS NOT NULL ORDER BY c.number, l.title")):
        match = r["match"]
        if isinstance(match, str):
            try:
                match = json.loads(match)
            except ValueError:
                continue
        if youtube.is_channel(match) and not youtube.survives_a_rename(match):
            out.append({"channel": r["channel"], "title": r["title"], "keyed_on": match.get("id")})
    return out


def _providers(conn: sqlite3.Connection, settings: dict[str, Any]) -> dict[str, Any]:
    """Whether pitv_content can still fetch the entries that name a creator's channel.

    Such an entry carries the channel's own address and looks self-contained, but pitv_content
    only lists that address: each video is fetched by its id, and an id is routed to whichever
    enabled provider says it serves that id space. Switch that provider off and every one of
    these entries stops, with nothing on the entry or on the provider to connect the two.

    A version of pitv_content that does not report `serves` yet says nothing rather than
    guessing from the provider's type name, which is its to rename."""
    from . import youtube
    entries = conn.execute(
        "SELECT COUNT(*) FROM lineup WHERE enabled = 1 AND match IS NOT NULL AND match LIKE ?",
        (f'%"{youtube.SOURCE}"%',)).fetchone()[0]
    if not entries:
        return {"channel_entries": 0}
    status, body = tool_client.request(tool_client.base_url(settings), "GET", "providers", timeout=5)
    if status != 200 or not isinstance(body, list):
        return {"channel_entries": entries, "reachable": False}
    if not any("serves" in p for p in body if isinstance(p, dict)):
        return {"channel_entries": entries, "reachable": True, "reports_serves": False}
    serving = [p.get("id") for p in body
               if isinstance(p, dict) and p.get("enabled") and p.get("serves") == youtube.SERVES]
    return {"channel_entries": entries, "reachable": True, "reports_serves": True, "serving": serving}


def _library(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT kind, origin, SUM(missing = 0 AND excluded = 0) AS live, SUM(missing) AS missing"
                        " FROM media GROUP BY kind, origin ORDER BY kind, origin").fetchall()
    # Adverts held out of the breaks for want of a name. They are on the drive and cost cache
    # room, so the count belongs in the report rather than only in the admin's attention list.
    unnamed = keyword_pattern(all_settings(conn).get("unnamed_advert_keywords"))
    adverts = [r["title"] for r in conn.execute(
        "SELECT title FROM media WHERE kind = 'advert' AND missing = 0 AND excluded = 0")]
    return {"media": [dict(r) for r in rows],
            "adverts_usable": sum(1 for t in adverts if names_a_product(t, unnamed)),
            "adverts_unnamed": sum(1 for t in adverts if not names_a_product(t, unnamed)),
            "channels_enabled": conn.execute("SELECT COUNT(*) FROM channels WHERE enabled = 1").fetchone()[0]}


def _wanted(conn: sqlite3.Connection) -> dict[str, Any]:
    """The request list by status, and what is going wrong in it.

    Counts by status hid the thing worth knowing. "1,653 queued" was true for hours while every
    one of them carried an ImportError from pitv_content and none could ever have succeeded, and
    nothing in the admin or this report said so: the only symptom was a channel that stayed
    empty. A request's own message is the evidence, so it is gathered here rather than left for
    somebody to find with a query.

    A search that came back empty is not a fault. pitv_content says so in the first words of the
    message, and those are counted apart: a title nothing has uploaded will be asked for again
    and needs nobody. Anything else is a fault, whatever the row's status says, because a request
    that cannot be prepared is not waiting for a better day."""
    from .content import (  # imported here: content reads this module's settings
        MAX_WANTED_ATTEMPTS,
        MISS_PREFIX,
    )
    by_status = [dict(r) for r in conn.execute(
        "SELECT kind, status, auto, COUNT(*) AS n FROM wanted GROUP BY kind, status, auto ORDER BY n DESC")]
    faults = [dict(r) for r in conn.execute(
        "SELECT message, COUNT(*) AS n, MAX(updated_at) AS last_at, MIN(id) AS example FROM wanted"
        f" WHERE status IN ('queued', 'failed') AND message IS NOT NULL AND message != ''"
        f" AND message NOT LIKE '{MISS_PREFIX}%' GROUP BY message ORDER BY n DESC LIMIT 10")]
    misses = conn.execute(
        f"SELECT COUNT(*) FROM wanted WHERE status = 'queued' AND message LIKE '{MISS_PREFIX}%'").fetchone()[0]
    given_up = conn.execute("SELECT COUNT(*) FROM wanted WHERE attempts >= ?", (MAX_WANTED_ATTEMPTS,)).fetchone()[0]
    return {"by_status": by_status, "faults": faults, "searched_and_not_found": misses, "given_up": given_up}


def _runs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The latest run of each kind, and any that ended in error in the last two days."""
    latest = conn.execute("SELECT kind, status, summary, started_at, finished_at FROM run_log WHERE id IN"
                          " (SELECT MAX(id) FROM run_log GROUP BY kind) ORDER BY kind").fetchall()
    errors = conn.execute("SELECT kind, status, summary, started_at, finished_at FROM run_log WHERE status = 'error'"
                          " AND started_at > ? ORDER BY id DESC LIMIT 10", (now_ts() - 2 * DAY,)).fetchall()
    return [dict(r) for r in latest] + [{**dict(r), "recent_error": True} for r in errors]


def _abandoned(conn: sqlite3.Connection, settings: dict[str, Any]) -> list[dict[str, Any]]:
    """pitv_content runs that ended without ever reporting to PiTV.

    PiTV learns what happened in a run from the report it is sent at the end, so a run that dies
    before it sends one is invisible here: the run log simply has no entry, which looks exactly
    like a quiet night. One was killed today by a restart landing between slices, and the only
    trace was a line in pitv_content's own job list that nothing on this side reads.

    So the job list is read, and a job pitv_content says failed is matched against the reports
    that did arrive. A run whose report came through is not listed however it ended, because
    then PiTV knows what it did; what is listed is the runs PiTV was never told about."""
    status, body = tool_client.request(tool_client.base_url(settings), "GET", "jobs", timeout=5)
    jobs = body if isinstance(body, list) else (body or {}).get("jobs") if isinstance(body, dict) else None
    if status != 200 or not isinstance(jobs, list):
        return []
    reported = {r["started_at"] for r in conn.execute(
        "SELECT started_at FROM run_log WHERE kind = 'content' AND started_at > ?", (now_ts() - 2 * DAY,))}
    out = []
    for job in jobs:
        if not isinstance(job, dict) or job.get("status") != "failed":
            continue
        started = as_int(job.get("started_ts"))
        if started is None or started < now_ts() - 2 * DAY:
            continue
        # A report is matched on the second it started, which is what the report carries.
        if any(abs(started - seen) <= 1 for seen in reported):
            continue
        out.append({"job_id": job.get("job_id"), "mode": job.get("mode"), "started_ts": started,
                    "detail": str(job.get("summary") or "").strip() or "no reason given"})
    return out


def _run_details(conn: sqlite3.Connection, prefix: str) -> list[dict[str, str]]:
    """The run-log messages carrying `prefix`, newest run first, keyed by the band they name."""
    out: list[dict[str, str]] = []
    for row in conn.execute("SELECT details FROM run_log WHERE kind = 'content' ORDER BY id DESC LIMIT ?",
                            (STARVED_HISTORY,)):
        try:
            messages = json.loads(row["details"] or "[]")
        except ValueError:
            break
        out.append({m.split(" in ", 1)[-1].split(",", 1)[0]: m
                    for m in messages if isinstance(m, str) and m.startswith(prefix)})
    return out


def _starved(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Bands pitv_content ended a slice before looking at, and for how many runs running.

    This is starvation and nothing else. Work it looked at and deliberately held for a later
    slice is a different fact and is reported apart, because the two were once one and either
    was then impossible to judge: a deferral is a rule working, and a band never reached means
    nothing about it will change on its own. That is what left 148 requests untouched for a day
    while every report read as an ordinary busy night.

    The streak is kept because it is worth knowing how long, not because it decides anything:
    one is already worth saying now that a deferral is no longer counted here."""
    per_run = _run_details(conn, UNREACHED_PREFIX)
    if not per_run:
        return []
    out = []
    for band, message in per_run[0].items():
        streak = 0
        for run in per_run:
            if band not in run:
                break
            streak += 1
        out.append({"band": band, "runs": streak, "capped": streak == len(per_run), "message": message})
    return sorted(out, key=lambda s: (-s["runs"], s["band"]))


def _held_too_long(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Work deliberately held over whose wait has outlived the bound meant to end it.

    Holding a second re-encode for the next slice is correct: one long one can take an evening.
    pitv_content bounds how long that may go on and takes the request regardless after so many
    slices, which is the part worth checking, because a rule that can hold work back with no
    bound is the shape of fault both applications keep finding in each other. An ordinary wait
    says nothing here; only one that has passed the bound it reports does."""
    runs = _run_details(conn, HELD_PREFIX)
    if not runs:
        return []
    out = []
    for band, message in runs[0].items():
        waited, limit = _slices_of(message)
        # One over is the slice that takes it; further means the bound did not fire.
        if limit and waited > limit + 1:
            out.append({"band": band, "slices": waited, "limit": limit, "message": message})
    return sorted(out, key=lambda h: -h["slices"])


def _slices_of(message: str) -> tuple[int, int]:
    """The "waiting N slice(s) of M" a held-over message ends with, as (N, M); (0, 0) if absent."""
    found = re.search(r"waiting (\d+) slice\(s\) of (\d+)", message)
    return (int(found.group(1)), int(found.group(2))) if found else (0, 0)


def _content(settings: dict[str, Any]) -> dict[str, Any]:
    status, body = tool_client.request(tool_client.base_url(settings), "GET", "status", timeout=5)
    if status != 200 or not isinstance(body, dict):
        return {"reachable": False, "detail": body.get("error") if isinstance(body, dict) else f"HTTP {status}"}
    active = body.get("active_job") or {}
    queued: dict[str, int] = {}
    for job in body.get("queued_jobs") or []:
        queued[job.get("mode") or "?"] = queued.get(job.get("mode") or "?", 0) + 1
    return {"reachable": True, "version": body.get("version"), "state": body.get("state"), "phase": body.get("phase"),
            "active_job": {k: active.get(k) for k in ("job_id", "mode", "kind", "started_ts")} if active else None,
            "queued_by_mode": queued, "counts": body.get("counts"), "errors": (body.get("errors") or [])[:10],
            # Why it is idle, in its own words: "nothing to fetch", "resting until HH:MM", "waiting for room".
            "idle_reason": body.get("idle_reason"),
            # A job pitv_content ran ahead of its turn because a rule had held it too long, and
            # anything still waiting far longer than it should. Both are its own account of
            # itself; a queue that heals silently is only half of what was asked for.
            "healed": (body.get("healed") or [])[:10], "queue_warning": body.get("queue_warning")}


def _unreadable_sources(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Enabled library sources pitv_content cannot read from here, by its live check.

    With the NAS away nothing fails loudly: copies wait, the player falls back to a share that is
    not there, and an index publishes incomplete, which PiTV rightly imports as additions only.
    On 23 September the desktop session that held the NAS mounts was killed and every share was
    gone for a day and a half, while this report showed only a cache that seemed slow to fill. An
    unreachable pitv_content is its own finding, so it gives an empty list here."""
    status, body = tool_client.request(tool_client.base_url(settings), "GET", "sources", timeout=5)
    sources = body if isinstance(body, list) else (body or {}).get("sources") if isinstance(body, dict) else None
    if status != 200 or not isinstance(sources, list):
        return []
    out = []
    for source in (s for s in sources if isinstance(s, dict)):
        health = source.get("health") or {}
        if source.get("enabled") is False or health.get("readable") is not False:
            continue
        out.append({"id": source.get("id"), "name": source.get("name") or source.get("id"),
                    "error": health.get("mount_error") or health.get("error") or "not reachable"})
    return out


def _logs(cfg: Config) -> dict[str, list[dict[str, Any]]]:
    """Warnings and errors from the last day, per log. Older ones are history, not a symptom."""
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - DAY))
    out = {}
    for path in sorted(log_dir(cfg).glob("*.log")):
        recent = [e for e in tail(path, 400, min_level="WARNING") if e["ts"] >= since]
        if recent:
            out[path.name] = recent[-LOG_LINES:]
    return out


def _disks(cfg: Config, settings: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for label, folder in (("data", cfg.data_dir), ("cache", Path(settings.get("cache_dir") or ""))):
        if not str(folder) or str(folder) == ".":
            continue
        try:
            usage = shutil.disk_usage(folder)
            out.append({"what": label, "path": str(folder), "free_bytes": usage.free, "total_bytes": usage.total})
        except OSError as exc:
            out.append({"what": label, "path": str(folder), "error": str(exc)})
    return out


# --- findings -------------------------------------------------------------------------------

def _is_miss(message: str) -> bool:
    """Whether a message from pitv_content is a search that found nothing rather than a fault.

    It says so in the first words, and the same test decides whether the request keeps its
    attempts, so the rule is one constant both sides read. The message may be prefixed with the
    request it belongs to ("w:2104: not found yet: ..."), so the prefix is looked for after the
    last colon-space that precedes it rather than only at the start."""
    from .content import MISS_PREFIX
    return MISS_PREFIX in message.lower()


def _request_message(error: str) -> str:
    """A live error with its request id stripped, so it can be matched against the stored message.

    pitv_content reports a failure as "w:1917: candidates did not process" while the row itself
    holds only the part after the id; they are the same fault seen from either side."""
    found = re.match(r"^w:\d+:\s*(.*)$", error.strip())
    return found.group(1) if found else error.strip()


def _findings(doc: dict[str, Any]) -> list[str]:
    """What needs attention, most serious first. An empty list means nothing does."""
    out: list[str] = []
    for name, section in doc.items():
        if isinstance(section, dict) and set(section) == {"error"}:
            out.append(f"The {name} section could not be gathered: {section['error']}")
    for unit in doc.get("services") if isinstance(doc.get("services"), list) else []:
        if unit.get("loaded") == "loaded" and unit.get("active") == "failed":
            out.append(f"{unit['unit']} has failed ({unit.get('result')})")
    player = doc.get("player") or {}
    if player.get("online") is False:
        out.append("The player is not answering")
    elif player.get("error"):
        out.append(f"The player reports: {player['error']}")
    content = doc.get("content") or {}
    if content.get("reachable") is False:
        out.append(f"pitv_content is not reachable: {content.get('detail')}")
    if unreadable := doc.get("unreadable_sources") if isinstance(doc.get("unreadable_sources"), list) else []:
        named = "; ".join(f"{s['name']} ({s['error']})" for s in unreadable)
        out.append(f"{len(unreadable)} library source(s) cannot be read: {named}. Until they can, their programmes "
                   "play only from the cache, copies from them wait, and an index leaves them out. Reconnect the "
                   "share (Sources, Edit, Test connection says whether pitv_content can reach it), then Re-index "
                   "sources and import.")
    # A search that came back empty is an answer, not a fault: the title will be asked for again
    # and nobody need do anything. Listing ten of them one per line pushed the findings that do
    # need attention off the first screen, which is the same silence by another route.
    errors = [str(e) for e in content.get("errors") or []]
    misses = [e for e in errors if _is_miss(e)]
    # Nor is the same fault said twice. pitv_content's live list names one failing request;
    # PiTV's own list of faults names the same message with how many requests hold it and the
    # button that clears them, which is strictly the better sentence, so the bare one is dropped.
    counted = {str(f.get("message")) for f in (doc.get("wanted") or {}).get("faults") or []} \
        if isinstance(doc.get("wanted"), dict) else set()
    for error in (e for e in errors if e not in misses and _request_message(e) not in counted):
        out.append(f"pitv_content reports: {error}")
    if misses:
        out.append(f"{len(misses)} title(s) searched for and not found yet; they stay in the queue and are "
                   "asked for again. Wanted lists them with what each search said.")
    if warning := content.get("queue_warning"):
        out.append(f"pitv_content's queue: {warning}")
    for healed in content.get("healed") or []:
        # Worth a finding rather than a log line: the queue put itself right, and the rule that
        # held the job is still there to hold the next one.
        out.append(f"pitv_content healed its queue: {healed}")
    library = doc.get("library") or {}
    if isinstance(library, dict) and (held := library.get("adverts_unnamed") or 0):
        usable = library.get("adverts_usable") or 0
        out.append(f"{held} adverts name no product and are not put in a break, leaving {usable} that do. "
                   "They are chapters of a compilation nothing could identify; pitv_content is the one to name them.")
    providers = doc.get("providers") or {}
    if providers.get("reports_serves") and not providers.get("serving"):
        out.append(f"{providers['channel_entries']} catalogue entries name a creator's channel, and no enabled "
                   "provider serves the videos they fetch. Each entry carries a working address, so nothing "
                   "about it says why it has stopped: enable the provider in Content, Providers.")
    if fragile := doc.get("fragile_matches") or []:
        # Not broken, and not urgent: both forms fetch. But the day a creator renames themselves
        # the address stops resolving, and what anybody sees is a series that quietly stopped.
        out.append(f"{len(fragile)} catalogue entries are keyed on a handle or vanity name rather than the "
                   f"channel's own id, so each depends on its creator never renaming it "
                   f"(for example \"{fragile[0]['title']}\" on {fragile[0]['channel']})")
    for stranded in (doc.get("bands") or {}).get("no_band_can_air") or []:
        # Not a shortfall: this material is held and can never be shown, so nothing about it
        # will change until a band is lengthened or given to it.
        out.append(f"{stranded['channel']}: {stranded['items']} item(s) no band can air, the longest "
                   f"{stranded['longest_minutes']} min (\"{stranded['longest_title']}\"); its longest band "
                   f"runs {stranded['longest_band_minutes']} min")
    # A request that cannot be prepared is not waiting for a better day, and a queue of them
    # reads as ordinary work outstanding unless the message is put on screen. Each is named with
    # what it says and what to do about it, because a count nobody can act on is a better-worded
    # silence: these came to nothing for eight hours while the report said 1,653 queued.
    for run in doc.get("abandoned_runs") or []:
        # PiTV learns what a run did from the report it sends at the end, so one that dies first
        # leaves no entry at all and reads as a quiet night. Said here because nothing else does.
        out.append(f"A pitv_content {run.get('mode')} run ended without reporting to PiTV, so what it did or did "
                   f"not deliver is unknown here: {run['detail']}. The next run sees the truth on disk, so nothing "
                   "is lost; a run of these means something is stopping it part way through.")
    for starved in doc.get("starved") or []:
        # The work was ready and the slice ended before looking at it. Nothing failed, which is
        # why nothing else in this report says so. Work deliberately held over is a separate
        # fact and is not counted here, so this always means the slice stopped short.
        how_many = f"at least {starved['runs']}" if starved.get("capped") else str(starved["runs"])
        runs = "the last run" if starved["runs"] == 1 else f"{how_many} runs running"
        out.append(f"pitv_content ended its slice before reaching its {starved['band']} work, in {runs}. "
                   f"{starved['message'][len(UNREACHED_PREFIX):]}. Nothing failed and nothing will change on its "
                   "own; this is pitv_content's delivery order to answer for.")
    for held in doc.get("held_too_long") or []:
        # Holding a re-encode over is correct; holding it for ever is the fault that rule could
        # become, so pitv_content bounds it and this says when the bound has not held.
        out.append(f"pitv_content has held {held['band']} work for {held['slices']} slices running, past the "
                   f"{held['limit']} it takes them regardless after. The bound that should have ended the wait "
                   "has not, so that work may never be done.")
    requests = doc.get("wanted") if isinstance(doc.get("wanted"), dict) else {}
    for fault in requests.get("faults") or []:
        out.append(f"{fault['n']} request(s) are failing with the same error and will not come right on their own: "
                   f"\"{fault['message']}\". This is pitv_content's to fix; once it is, clear them with Retry on "
                   f"the Wanted page, which puts every request carrying this message back in the queue.")
    if given_up := requests.get("given_up"):
        out.append(f"{given_up} request(s) have been given up on after {MAX_ATTEMPTS} attempts and will not be asked "
                   f"for again. Wanted, Retry asks once more; anything genuinely unavailable is better deleted.")
    # pitv_content is never to be idle while anything is left to fetch.
    waiting = sum(r["n"] for r in requests.get("by_status") or [] if r.get("status") == "queued")
    short = sum(1 for b in doc.get("bands") or [] if isinstance(b, dict) and (b.get("have") or 0) < (b.get("want") or 0))
    if content.get("reachable") and not content.get("active_job") and not content.get("queued_by_mode") and (waiting or short):
        why = f" (it says: {content['idle_reason']})" if content.get("idle_reason") else ""
        out.append(f"pitv_content is idle with work outstanding: {waiting} request(s) queued and {short} band(s) under stock{why}")
    elif content.get("idle_reason") == "waiting for room":
        out.append("pitv_content is idle waiting for room in the cache")
    schedule = doc.get("schedule") or {}
    if schedule.get("days_ahead", 0) < 2:
        out.append(f"The schedule runs only {schedule.get('days_ahead', 0)} days ahead")
    holes = [d for d in schedule.get("channel_days") or [] if (d.get("hole_minutes") or 0) > 0]
    if holes:
        worst = max(holes, key=lambda d: d["hole_minutes"])
        out.append(f"{len(holes)} channel-days have holding cards outside any band; the worst is {worst['name']}"
                   f" on {worst['day']} with {worst['hole_minutes']} minutes")
    cache = doc.get("cache") or {}
    if cache.get("next_day_files") and cache.get("cached_percent", 100) < CACHED_TARGET:
        out.append(f"Only {cache['cached_percent']}% of the next day's {cache['next_day_files']} files are in the cache"
                   f" (waiting: {cache.get('waiting_by_action')})")
    holds = cache.get("cap_holds_days")
    if holds is not None:
        usage = cache.get("usage") or {}
        gib = 1024 ** 3
        free = usage.get("free")
        drive = f"; the drive has {free // gib} GiB spare" if isinstance(free, int) else ""
        if holds < CAP_DAYS_TARGET:
            want = int(CAP_DAYS_TARGET * cache["schedule_day_bytes"] / gib)
            out.append(f"The cache holds only {holds} days of the schedule ({usage.get('max', 0) // gib} GiB against"
                       f" {cache['schedule_day_bytes'] // gib} GiB a day), so copies are evicted before their slot"
                       f" comes round and those programmes play from the NAS. It wants about {want} GiB{drive}")
        # Fetched episodes are kept so a later airing is free; they are what the cache grows by.
        spare, kept = cache.get("room_for_kept_bytes", 0), cache.get("kept_bytes", 0)
        if kept and spare < kept:
            out.append(f"After a day of schedule the cache has {max(spare, 0) // gib} GiB left for fetched episodes,"
                       f" which already hold {kept // gib} GiB, so what is kept will start being evicted{drive}")
    cards = (doc.get("bands") or {}).get("holding_cards_next_two_days") or []
    if cards:
        minutes = sum(c["card_minutes"] for c in cards)
        out.append(f"{len(cards)} bands are short of material in the next two days, {minutes} minutes of holding card in all")
    failed: dict[str, list[dict[str, Any]]] = {}
    for run in doc.get("runs") if isinstance(doc.get("runs"), list) else []:
        if run.get("recent_error"):
            failed.setdefault(run["kind"], []).append(run)
    for kind, runs in failed.items():        # newest first, as gathered
        out.append(f"{len(runs)} {kind} run(s) ended in error in the last two days; the latest: {runs[0]['summary']}")
    for disk in doc.get("disks") if isinstance(doc.get("disks"), list) else []:
        if disk.get("error"):
            out.append(f"The {disk['what']} folder {disk['path']} cannot be read: {disk['error']}")
        elif disk.get("free_bytes", LOW_DISK_BYTES) < LOW_DISK_BYTES:
            out.append(f"The {disk['what']} drive has {disk['free_bytes'] // 1024 ** 2} MB free")
    return out


# --- text ------------------------------------------------------------------------------------

def render(doc: dict[str, Any]) -> str:
    """The report as text for a terminal: findings, then a few lines per section."""
    lines = [f"PiTV {doc['version']} doctor"]
    lines += ["", "Findings"] + ([f"  ! {f}" for f in doc["findings"]] or ["  nothing needs attention"])
    player, cache, content, schedule = (doc.get(k) or {} for k in ("player", "cache", "content", "schedule"))
    lines += ["", "Services"] + [f"  {u['unit']}: {u.get('active') or 'not installed'} ({u.get('sub') or '-'})"
                                 for u in (doc.get("services") if isinstance(doc.get("services"), list) else [])]
    lines += ["", f"Player: {'online' if player.get('online') else 'offline'}, {player.get('channel') or '-'},"
                  f" showing {player.get('showing') or '-'} {('(' + player['error'] + ')') if player.get('error') else ''}".rstrip()]
    lines += [f"Schedule: {schedule.get('days_ahead', 0)} days ahead"]
    lines.append(f"Cache: {cache.get('cached', 0)} of {cache.get('next_day_files', 0)} of the next day's files"
                 f" ({cache.get('cached_percent', 0)}%), waiting {cache.get('waiting_by_action') or 'nothing'}")
    if content.get("reachable"):
        active = content.get("active_job") or {}
        lines.append(f"pitv_content {content.get('version')}: {content.get('state')},"
                     f" active {active.get('mode') or 'nothing'}, queued {content.get('queued_by_mode') or 'nothing'}")
    lines += ["", "Bands short of material (next two days)"]
    lines += [f"  {c['day']} {c['channel']} / {c['band']}: {c['card_minutes']} min of card"
              for c in (doc.get("bands") or {}).get("holding_cards_next_two_days") or []] or ["  none"]
    lines += ["", "Latest warnings"]
    for name, entries in (doc.get("logs") or {}).items():
        lines += [f"  {name}: {e['ts']} {e['level']} {e['msg'].splitlines()[0][:150]}" for e in entries[-3:]]
    return "\n".join(lines)
