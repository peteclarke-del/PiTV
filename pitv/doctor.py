"""One report on the state of the whole television, for whoever has to find out what is wrong.

`pitv doctor` on the machine, or `GET /api/doctor` from the admin, gathers what would otherwise
take a dozen commands over SSH: the services, what the player is doing, holes in the schedule,
how much of the next day is in the cache, which bands are short, what pitv_content is working
on, the last warnings from every log, and the disks. It opens with the findings, in plain
sentences, so the first screen says whether anything needs attention.

It only reads. Every section stands alone: one that cannot be gathered says why and the rest
still are, because the report matters most when something is broken. It deliberately offers no
way to change anything and nothing listens for it: remote support is SSH to the machine
(docs/PLAN.md, Remote support), and this is the first thing to run once there."""

from __future__ import annotations

import shutil
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__, tool_client
from .config import Config
from .db import all_settings, now_ts
from .hostinfo import host_info
from .logsetup import log_dir, tail
from .scheduler.rules import keyword_pattern, names_a_product
from .player.cache import MediaCache
from .player.hwdec import is_raspberry_pi
from .scheduler.rules import tz_of

DAY = 86400
CACHED_TARGET = 95      # per cent of the next day's files expected in the cache
CAP_DAYS_TARGET = 1.5   # days of built schedule the cache should hold; below this it thrashes
LOW_DISK_BYTES = 5 * 1024 ** 3
LOG_LINES = 12


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
        "wanted": lambda: _wanted(conn),
        "runs": lambda: _runs(conn),
        "content": lambda: _content(settings),
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
    from .wanted import band_needs
    cards = conn.execute(
        "SELECT c.name AS channel, s.block AS band, s.day, (s.end_ts - s.start_ts) / 60 AS card_minutes"
        " FROM schedule s JOIN channels c ON c.id = s.channel_id WHERE s.kind = 'filler' AND s.block IS NOT NULL"
        " AND s.replay = 0 AND s.end_ts > ? AND s.start_ts < ? ORDER BY s.start_ts", (now, now + 2 * DAY)).fetchall()
    needs = [{"channel": n["channel"]["name"], "band": n["band"].name, "kind": n["kind"], "have": n["have"], "want": n["want"]}
             for n in band_needs(conn, settings)]
    return {"holding_cards_next_two_days": [dict(r) for r in cards], "due_a_top_up": needs}


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


def _wanted(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT kind, status, auto, COUNT(*) AS n FROM wanted GROUP BY kind, status, auto ORDER BY n DESC")]


def _runs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The latest run of each kind, and any that ended in error in the last two days."""
    latest = conn.execute("SELECT kind, status, summary, started_at, finished_at FROM run_log WHERE id IN"
                          " (SELECT MAX(id) FROM run_log GROUP BY kind) ORDER BY kind").fetchall()
    errors = conn.execute("SELECT kind, status, summary, started_at, finished_at FROM run_log WHERE status = 'error'"
                          " AND started_at > ? ORDER BY id DESC LIMIT 10", (now_ts() - 2 * DAY,)).fetchall()
    return [dict(r) for r in latest] + [{**dict(r), "recent_error": True} for r in errors]


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
    for error in content.get("errors") or []:
        out.append(f"pitv_content reports: {error}")
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
    # pitv_content is never to be idle while anything is left to fetch.
    waiting = sum(r["n"] for r in doc.get("wanted") or [] if isinstance(r, dict) and r.get("status") == "queued")
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
