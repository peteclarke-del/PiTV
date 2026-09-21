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
from datetime import timedelta
from typing import Any

from . import tool_client
from .db import DEFAULT_SETTINGS, LIVE, genre_list, get_setting, now_ts, rows_to_dicts, tx
from .scheduler import bands
from .scheduler.library import USABLE
from .scheduler.rules import tz_of

log = logging.getLogger("pitv.wanted")


def queue_gaps(conn: sqlite3.Connection) -> int:
    """Queue episodes missing between the first and last episode of each season on disk.
    Three queries in all, however large the library: this runs on every maintenance pass."""
    shows = {r["id"]: r for r in conn.execute(f"SELECT id, title, year FROM shows WHERE {LIVE}")}
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
BAND_ITEM_KINDS = {"music": ("music",), "episode": ("episode",), "movie": ("movie",)}


def withdraw_gaps(conn: sqlite3.Connection) -> int:
    """With "Request missing episodes" off, the requests it raised and nobody has answered are
    withdrawn, so pitv_content is not left working through a list the owner has switched off.
    They are derived from the library and come back the moment the setting does."""
    with tx(conn):
        cur = conn.execute("DELETE FROM wanted WHERE auto = 1 AND lineup_id IS NULL AND status IN ('queued', 'failed')"
                           " AND id NOT IN (SELECT wanted_id FROM schedule WHERE wanted_id IS NOT NULL)")
    return cur.rowcount


def band_needs(conn: sqlite3.Connection, settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Bands the library cannot fill, the next to air first.

    A band wants short items of its own genres and decades: a two hour "Disco Lunch" needs
    perhaps thirty of them. A share of full-length concert films satisfies none of that, so the
    band falls back to whatever fits and plays the same few films all day. This counts what the
    library actually holds for each band and reports the shortfall.

    When a band runs, and for how long, comes from the same timetable the scheduler places it
    by (`bands.timetable`), and whether an item is one the band could use is the band's own
    test (`Band.wants`), so this can never disagree with what the build does.

    What a band asks for is configuration, not something this module knows: the channel says
    what pitv_content should fetch for it (`fetch_kind`, for instance shows, cartoons, sport or
    music), and a band may name its own instead. A channel that asks for nothing is left alone,
    however thin its bands, because its material comes from somewhere else."""
    default_minutes = int(settings.get("band_item_max_minutes", bands.ITEM_MINUTES))
    channels = {c["id"]: c for c in rows_to_dicts(conn.execute("SELECT * FROM channels WHERE enabled = 1"))}
    now = now_ts()
    tz = tz_of(conn)
    out = []
    for channel_id, band_list in bands.load(conn).items():
        channel = channels.get(channel_id)
        if channel is None:
            continue
        airings = _airings(band_list, settings, now, tz)
        for band in band_list:
            kind = band.fetch or (channel.get("fetch_kind") or "")
            item_kinds = sorted({k for b in band.kinds for k in BAND_ITEM_KINDS.get(b, ())})
            mine = airings.get(band.id, [])
            if not kind or not item_kinds or not mine:
                continue
            minutes = band.max_minutes or int(channel.get("band_item_max_minutes") or default_minutes)
            if band.feature:
                # A band billed as a concert wants one long item an airing, not a run of short
                # ones, and may not repeat it within the feature repeat gap.
                repeat_days = channel.get("band_feature_repeat_days")
                if repeat_days is None:
                    repeat_days = settings.get("band_feature_repeat_days", 14)
                want = _airings_within(int(repeat_days) * 24, band)
                have = _matching_items(conn, band, item_kinds, minutes * 60, feature=True)
            else:
                longest = max(end - start for start, end in mine) // 60
                items_per_airing = max(1, (longest + BAND_ITEM_MINUTES - 1) // BAND_ITEM_MINUTES)
                repeat_hours = channel.get("band_item_repeat_hours")
                if repeat_hours is None:
                    repeat_hours = settings.get("band_item_repeat_hours", 36)
                # One airing's worth guarantees that a daily band repeats itself tomorrow even
                # though its configured repeat gap says it should not. Prepare enough distinct
                # material for every occurrence inside that gap, and then keep going until the band
                # could run `band_stock_days` without a repeat: the repeat gap is the least a band
                # can live on, not the point at which collecting for it should stop.
                stock_hours = int(settings.get("band_stock_days", 7)) * 24
                want = items_per_airing * _airings_within(max(int(repeat_hours), stock_hours), band)
                have = _matching_items(conn, band, item_kinds, minutes * 60)
            if have < want and now - (band.last_fetch_at or 0) >= int(settings.get("band_fetch_gap_hours", 1)) * 3600:
                out.append({"band": band, "channel": channel, "kind": kind, "have": have, "want": want,
                            "minutes": minutes, "next_ts": min(start for start, _ in mine)})
    # Preparation follows the timetable: the next band to air is more urgent than a larger
    # shortfall several hours later. The shortfall breaks ties between simultaneous bands.
    return sorted(out, key=lambda n: (n["next_ts"], n["have"] - n["want"]))


def _airings(band_list: list[bands.Band], settings: dict[str, Any], now: int, tz: Any
             ) -> dict[int, list[tuple[int, int]]]:
    """When each band next airs over the coming week, as (start, end) per band id, from the
    scheduler's own timetable, so a band that starts before the day does or runs past
    closedown is measured exactly as it will be placed."""
    from .scheduler.rules import broadcast_day_for, day_bounds, hhmm_to_minutes
    today = broadcast_day_for(now, settings, tz)
    day_start_min = hhmm_to_minutes(str(settings.get("day_start") or "08:00"))
    out: dict[int, list[tuple[int, int]]] = {}
    for offset in range(8):
        day = today + timedelta(days=offset)
        day_start, day_end, next_day_start = day_bounds(day, settings, tz)
        for start, end, band in bands.timetable(band_list, day, day_start, day_end, next_day_start, day_start_min, tz):
            if end > now:
                out.setdefault(band.id, []).append((max(start, now), end))
    return out


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


def _matching_items(conn: sqlite3.Connection, band: bands.Band, kinds: list[str], limit_seconds: int,
                    feature: bool = False) -> int:
    """How many items in the library the band could use: of its kinds, a feature or one of
    several as the band wants, and what the band itself would take at its first, exact step
    (its genres, and a known year in its decades)."""
    rows = conn.execute(
        f"SELECT id, genres, year, duration, concert FROM media WHERE kind IN ({','.join('?' * len(kinds))})"
        f" AND {USABLE} AND duration > 0", tuple(kinds)).fetchall()
    minutes = max(1, limit_seconds // 60)
    return sum(1 for r in rows
               if bands.is_feature({"duration": r["duration"], "concert": r["concert"]}, minutes) == feature
               and band.wants({"genres": genre_list(r["genres"]), "year": r["year"]})
               and (not band.decades or band.dated({"year": r["year"]}) is True))


def request_band_material(conn: sqlite3.Connection, settings: dict[str, Any],
                          outstanding: dict[int, str] | None = None) -> dict[str, Any]:
    """Ask pitv_content for material for the band that needs it most.

    pitv_content queues the request and runs one job at a time (contract section 9), so
    `request_all_band_material` calls this once per starved band and the whole night's work is
    declared up front. A band is stamped once its request is accepted, so one whose genre
    nothing can satisfy does not block the rest night after night."""
    needs = band_needs(conn, settings)
    if not needs:
        return {"status": "ok", "asked": 0, "summary": "every band has material"}
    need = needs[0]
    band = need["band"]
    decades = sorted(band.decades)
    # Ask for what the band is short of, and never for a trifle: a run costs a search and a
    # download either way, and a band wanting thirty videos should not be fed three a night.
    count = max(int(settings.get("band_fetch_min", 20)),
                min(int(settings.get("band_fetch_max", 60)), need["want"] - need["have"]))
    # Urgent goes ahead of pitv_content's cache work, so it is for a band with nothing at all;
    # a band that is merely short queues behind the copies tonight's schedule depends on.
    body: dict[str, Any] = {"mode": "catalogue", "kind": need["kind"], "count": count,
                            "urgent": need["have"] == 0, "max_minutes": need["minutes"]}
    if band.genres:
        body["genres"] = list(band.genres[:12])
    if decades:
        body["years"] = [decades[0], decades[-1] + 9]
    if band.id in (outstanding or {}):
        return {"status": "ok", "asked": 0,
                "summary": f"{band.name}: its last helping has not had its turn yet"}
    url = get_setting(conn, "content_tool_url") or DEFAULT_SETTINGS["content_tool_url"]
    status, payload = tool_client.request(url, "POST", "run", body=body, timeout=15)
    if status in (409, 503):
        # Busy or not running: nothing was asked, so the band keeps its turn rather than waiting
        # a night for another one.
        return {"status": "ok", "asked": 0, "summary": f"{band.name}: pitv_content is busy; will ask again"}
    with tx(conn):
        conn.execute("UPDATE band SET last_fetch_at = ?, fetch_job_id = ? WHERE id = ?",
                     (now_ts(), (payload or {}).get("job_id") if isinstance(payload, dict) else None, band.id))
    if status >= 400 or not isinstance(payload, dict) or not payload.get("ok"):
        reason = (payload or {}).get("error") or (payload or {}).get("errors") or f"HTTP {status}"
        log.warning("band %s: pitv_content refused the request: %s", band.name, reason)
        return {"status": "error", "asked": 0, "summary": f"{band.name}: {reason}"}
    summary = (f"{band.name} ({need['kind']}): asked for {count} "
               f"{', '.join(body.get('genres') or ['any'])} items"
               f"{' from ' + str(body['years'][0]) + ' to ' + str(body['years'][1]) if decades else ''}")
    log.info("band material: %s", summary)
    return {"status": "ok", "asked": 1, "summary": summary, "job_id": payload.get("job_id")}


def settle_band_requests(conn: sqlite3.Connection, settings: dict[str, Any]) -> dict[str, Any]:
    """Reconcile the helpings PiTV has asked for with what its bands still need.

    A helping can wait hours for its turn, and a band can fill from an earlier one meanwhile.
    Nothing used to take the ask back, so the request stayed in pitv_content's queue as a promise
    of work already done: eight were found waiting up to five hours for bands that were by then
    fully stocked, and they would have fetched music nobody was waiting for ahead of episodes
    that slots were waiting for. A band that no longer needs its helping has it cancelled, and
    one whose helping is still coming is not asked again.

    Returns the ids still outstanding, keyed by band, so the caller knows what not to ask for."""
    rows = conn.execute("SELECT id, name, fetch_job_id FROM band WHERE fetch_job_id IS NOT NULL").fetchall()
    if not rows:
        return {"outstanding": {}, "cancelled": [], "forgotten": []}
    url = get_setting(conn, "content_tool_url") or DEFAULT_SETTINGS["content_tool_url"]
    status, payload = tool_client.request(url, "GET", "jobs", timeout=10)
    if status != 200 or not isinstance(payload, list):
        # Unreachable: keep what is recorded rather than asking again for what may be coming.
        return {"outstanding": {int(r["id"]): r["fetch_job_id"] for r in rows}, "cancelled": [], "forgotten": []}
    alive = {j.get("job_id") for j in payload
             if isinstance(j, dict) and j.get("status") in ("queued", "running")}
    wanted_now = {n["band"].id for n in band_needs(conn, settings)}
    outstanding: dict[int, str] = {}
    cancelled: list[str] = []
    forgotten: list[str] = []
    for row in rows:
        band_id, job = int(row["id"]), row["fetch_job_id"]
        if job not in alive:
            forgotten.append(job)                    # run, given up or cancelled: nothing to hold
        elif band_id in wanted_now:
            outstanding[band_id] = job               # still coming, and still wanted
            continue
        else:
            tool_client.request(url, "POST", "cancel", body={"job_id": job}, timeout=10)
            cancelled.append(job)
            log.info("band %s is stocked; withdrew its helping %s", row["name"], job)
        with tx(conn):
            conn.execute("UPDATE band SET fetch_job_id = NULL WHERE id = ?", (band_id,))
    return {"outstanding": outstanding, "cancelled": cancelled, "forgotten": forgotten}


def request_all_band_material(conn: sqlite3.Connection, settings: dict[str, Any]) -> dict[str, Any]:
    """Queue every currently starved band with pitv_content's single coordinator.

    Fetch jobs still run one at a time, but they are all declared up front so a schedule built
    days ahead does not spend ten-minute maintenance intervals merely discovering its needs.
    Older pitv_content versions that reject work while busy stop the loop and are retried by the
    next maintenance pass.
    """
    settled = settle_band_requests(conn, settings)
    remaining = len(band_needs(conn, settings))
    if not remaining:
        return {"status": "ok", "asked": 0, "summary": "every band has material", "jobs": [],
                "withdrawn": settled["cancelled"]}
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
        result = request_band_material(conn, settings, settled["outstanding"])
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
