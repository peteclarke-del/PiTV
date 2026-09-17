"""The wanted list: what PiTV asks pitv_content to find. PiTV never downloads or encodes;
it publishes wanted items in the content manifest and records the results from reports.

Two kinds of request. A wanted row names one title (a missing episode, a song someone asked
for) and travels in the manifest. A band request names no title at all: it says that a stretch
of a channel's day wants short items of certain genres and decades, and asks pitv_content to go
and find some (contract section 2). Bands are how a channel of music videos is built, and a
library of concert films cannot fill one."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from . import tool_client
from .db import DEFAULT_SETTINGS, genre_list, get_setting, now_ts, rows_to_dicts, tx
from .scheduler import bands
from .scheduler.rules import local_ts, tz_of

log = logging.getLogger("pitv.wanted")


def queue_gaps(conn: sqlite3.Connection) -> int:
    """Queue episodes missing between the first and last episode of each season on disk.
    Three queries in all, however large the library: this runs on every maintenance pass."""
    shows = {r["id"]: r for r in conn.execute("SELECT id, title, year FROM shows WHERE missing = 0 AND excluded = 0")}
    seasons: dict[tuple[int, int], set[int]] = {}
    for e in conn.execute("SELECT show_id, season, episode FROM media WHERE show_id IS NOT NULL AND missing = 0"
                          " AND season IS NOT NULL AND episode IS NOT NULL ORDER BY show_id, season"):
        if e["show_id"] in shows:
            seasons.setdefault((e["show_id"], e["season"]), set()).add(e["episode"])
    asked = {(r["show_id"], r["season"], r["episode"]) for r in conn.execute(
        "SELECT show_id, season, episode FROM wanted WHERE show_id IS NOT NULL")}
    now = now_ts()
    rows = []
    for (show_id, season), have in seasons.items():
        if season == 0 or season > 1900:
            continue  # specials, and date-based seasons (year = season) have no fixed count
        show = shows[show_id]
        rows += [(show["title"], show["year"], season, ep, show_id, now)
                 for ep in range(min(have), max(have)) if ep not in have and (show_id, season, ep) not in asked]
    if rows:
        with tx(conn):
            conn.executemany("INSERT INTO wanted(kind, title, year, season, episode, show_id, provider, auto, created_at)"
                             " VALUES ('episode', ?, ?, ?, ?, ?, 'auto', 1, ?)", rows)
    return len(rows)

# --- material for bands ---------------------------------------------------------------------

BAND_ITEM_MINUTES = 4                  # rough length of a band item, for judging how many a band needs
BAND_FETCH_GAP = 6 * 3600              # leave this long before asking for the same band again
MIN_FETCH = 20                         # the fewest items a run is worth starting for
MAX_FETCH = 60                         # and the most to ask for at once, so one band cannot hog the night
BAND_ITEM_KINDS = {"music": ("music",), "episode": ("episode",), "movie": ("movie",)}


def band_needs(conn: sqlite3.Connection, settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Bands the library cannot fill, the worst short of material first.

    A band wants short items of its own genres and decades: a two hour "Disco Lunch" needs
    perhaps thirty of them. A share of full-length concert films satisfies none of that, so the
    band falls back to whatever fits and plays the same few films all day. This counts what the
    library actually holds for each band and reports the shortfall.

    What a band asks for is configuration, not something this module knows: the channel says
    what pitv_content should fetch for it (`fetch_kind`, for instance shows, cartoons, sport or
    music), and a band may name its own instead. A channel that asks for nothing is left alone,
    however thin its bands, because its material comes from somewhere else."""
    default_minutes = int(settings.get("band_item_max_minutes", 25))
    channels = {c["id"]: c for c in rows_to_dicts(conn.execute("SELECT * FROM channels WHERE enabled = 1"))}
    now = now_ts()
    out = []
    for channel_id, band_list in bands.load(conn).items():
        channel = channels.get(channel_id)
        if channel is None:
            continue
        for band in band_list:
            kind = band.fetch or (channel.get("fetch_kind") or "")
            item_kinds = sorted({k for b in band.kinds for k in BAND_ITEM_KINDS.get(b, ())})
            if not kind or not item_kinds:
                continue
            minutes = band.max_minutes or int(channel.get("band_item_max_minutes") or default_minutes)
            duration = _band_duration(band, band_list, str(settings.get("day_start") or "08:00"))
            items_per_airing = max(1, (duration + BAND_ITEM_MINUTES - 1) // BAND_ITEM_MINUTES)
            repeat_hours = channel.get("band_item_repeat_hours")
            if repeat_hours is None:
                repeat_hours = settings.get("band_item_repeat_hours", 36)
            # One airing's worth guarantees that a daily band repeats itself tomorrow even
            # though its configured repeat gap says it should not. Prepare enough distinct
            # material for every occurrence inside that gap. A weekly band still needs one set.
            want = items_per_airing * _airings_within(int(repeat_hours), band)
            have = _matching_items(conn, item_kinds, band.genres, band.decades, minutes * 60)
            if have < want and now - (band.last_fetch_at or 0) >= BAND_FETCH_GAP:
                out.append({"band": band, "channel": channel, "kind": kind, "have": have, "want": want,
                            "minutes": minutes})
    # Preparation follows the timetable: the next band to air is more urgent than a larger
    # shortfall several hours later.  The shortfall breaks ties between simultaneous bands.
    return sorted(out, key=lambda n: (_next_band_ts(n["band"], settings, now, tz_of(conn)),
                                     n["have"] - n["want"]))


def _next_band_ts(band: bands.Band, settings: dict[str, Any], now: int, tz) -> int:
    """Next wall-clock occurrence of a band, respecting broadcast-day weekday rules."""
    today = datetime.fromtimestamp(now, tz).date()
    boundary = _hhmm_minutes(str(settings.get("day_start") or "08:00"))
    for offset in range(-1, 8):
        broadcast_day = today + timedelta(days=offset)
        if not band.on(broadcast_day.weekday()):
            continue
        calendar_day = broadcast_day + timedelta(days=1) if _hhmm_minutes(band.start) < boundary else broadcast_day
        at = local_ts(calendar_day, band.start, tz)
        if at > now:
            return at
    return now + 8 * 86400


def _airings_within(repeat_hours: int, band: bands.Band) -> int:
    """Largest number of this band's airings inside its item repeat window."""
    window_days = max(1, (max(0, repeat_hours) + 23) // 24)
    active = [day for day in range(14 + window_days) if band.on(day % 7)]
    return max(
        (sum(start <= day < start + window_days for day in active) for start in active[:14]),
        default=1,
    )


def _need_key(need: dict[str, Any]) -> tuple[Any, ...]:
    """Acquisition requirements that one catalogue run can satisfy together."""
    band = need["band"]
    return (need["channel"]["id"], need["kind"], tuple(sorted(g.casefold() for g in band.genres)),
            tuple(sorted(band.decades)), need["minutes"])


def _band_duration(band: bands.Band, all_bands: list[bands.Band], day_start: str) -> int:
    """Longest daily window this band owns, including an implicit ``to next`` duration.

    Day-specific bands can have a different successor on different weekdays, so preparation
    uses the longest applicable window. Explicit lengths are capped at the next active band in
    the same way as the scheduler; the final implicit band runs to the broadcast-day boundary.
    """
    boundary = _hhmm_minutes(day_start)

    def logical(value: str) -> int:
        minute = _hhmm_minutes(value)
        return minute + (1440 if minute < boundary else 0)

    durations = []
    for weekday in range(7):
        active = sorted((b for b in all_bands if b.on(weekday)), key=lambda b: logical(b.start))
        if band not in active:
            continue
        index = active.index(band)
        start = logical(band.start)
        next_start = logical(active[index + 1].start) if index + 1 < len(active) else boundary + 1440
        end = start + band.minutes if band.minutes is not None else next_start
        durations.append(max(1, min(end, next_start) - start))
    return max(durations, default=band.minutes or 60)


def _hhmm_minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _matching_items(conn: sqlite3.Connection, kinds: list[str], genres: tuple[str, ...],
                    decades: tuple[int, ...], limit_seconds: int) -> int:
    """How many items in the library a band could actually use."""
    rows = conn.execute(
        f"SELECT genres, year FROM media WHERE kind IN ({','.join('?' * len(kinds))})"
        " AND missing = 0 AND excluded = 0 AND duration > 0 AND duration <= ?",
        (*kinds, limit_seconds)).fetchall()
    wanted = {g.lower() for g in genres}
    count = 0
    for r in rows:
        if decades and ((r["year"] or 0) // 10) * 10 not in decades:
            continue
        if wanted and wanted.isdisjoint(g.lower() for g in genre_list(r["genres"])):
            continue
        count += 1
    return count


def request_band_material(conn: sqlite3.Connection, settings: dict[str, Any]) -> dict[str, Any]:
    """Ask pitv_content for material for the band that needs it most.

    One band at a time: pitv_content runs one job at a time, and these runs fetch from YouTube,
    which objects to a crowd. The band is stamped either way, so a band whose genre nothing can
    satisfy does not block the rest night after night."""
    needs = band_needs(conn, settings)
    if not needs:
        return {"status": "ok", "asked": 0, "summary": "every band has material"}
    need = needs[0]
    band = need["band"]
    decades = sorted(band.decades)
    # Ask for what the band is short of, and never for a trifle: a run costs a search and a
    # download either way, and a band wanting thirty videos should not be fed three a night.
    count = max(MIN_FETCH, min(MAX_FETCH, need["want"] - need["have"]))
    body: dict[str, Any] = {"mode": "catalogue", "kind": need["kind"], "count": count, "urgent": True,
                            "max_minutes": need["minutes"]}
    if band.genres:
        body["genres"] = list(band.genres[:12])
    if decades:
        body["years"] = [decades[0], decades[-1] + 9]
    url = get_setting(conn, "content_tool_url") or DEFAULT_SETTINGS["content_tool_url"]
    status, payload = tool_client.request(url, "POST", "run", body=body, timeout=15)
    if status in (409, 503):
        # Busy or not running: nothing was asked, so the band keeps its turn rather than waiting
        # a night for another one.
        return {"status": "ok", "asked": 0, "summary": f"{band.name}: pitv_content is busy; will ask again"}
    with tx(conn):
        conn.execute("UPDATE band SET last_fetch_at = ? WHERE id = ?", (now_ts(), band.id))
    if status >= 400 or not isinstance(payload, dict) or not payload.get("ok"):
        reason = (payload or {}).get("error") or (payload or {}).get("errors") or f"HTTP {status}"
        log.warning("band %s: pitv_content refused the request: %s", band.name, reason)
        return {"status": "error", "asked": 0, "summary": f"{band.name}: {reason}"}
    summary = (f"{band.name} ({need['kind']}): asked for {count} "
               f"{', '.join(body.get('genres') or ['any'])} items"
               f"{' from ' + str(body['years'][0]) + ' to ' + str(body['years'][1]) if decades else ''}")
    log.info("band material: %s", summary)
    return {"status": "ok", "asked": 1, "summary": summary, "job_id": payload.get("job_id")}


def request_all_band_material(conn: sqlite3.Connection, settings: dict[str, Any]) -> dict[str, Any]:
    """Queue every currently starved band with pitv_content's single coordinator.

    Fetch jobs still run one at a time, but they are all declared up front so a schedule built
    days ahead does not spend ten-minute maintenance intervals merely discovering its needs.
    Older pitv_content versions that reject work while busy stop the loop and are retried by the
    next maintenance pass.
    """
    remaining = len(band_needs(conn, settings))
    if not remaining:
        return {"status": "ok", "asked": 0, "summary": "every band has material", "jobs": []}
    jobs: list[Any] = []
    summaries: list[str] = []
    status = "ok"
    covered = 0
    for _ in range(remaining):
        needs = band_needs(conn, settings)
        if not needs:
            break
        first_key = _need_key(needs[0])
        equivalent = [n for n in needs if _need_key(n) == first_key]
        result = request_band_material(conn, settings)
        if not result.get("asked"):
            status = result.get("status", "error")
            if result.get("summary"):
                summaries.append(result["summary"])
            break
        jobs.append(result.get("job_id"))
        summaries.append(result["summary"])
        covered += len(equivalent)
        if len(equivalent) > 1:
            # One fetched pool fills every identical band. Stamp the siblings so the loop does
            # not submit duplicate downloads merely because the timetable names the block twice.
            with tx(conn):
                conn.executemany("UPDATE band SET last_fetch_at=? WHERE id=?",
                                 [(now_ts(), n["band"].id) for n in equivalent[1:]])
    asked = len(jobs)
    summary = f"queued material for {asked} band{'s' if asked != 1 else ''}"
    if covered < remaining:
        summary += f"; {remaining - covered} will retry"
    log.info("band material: %s", summary)
    return {"status": status, "asked": asked, "summary": summary, "jobs": jobs, "details": summaries}
