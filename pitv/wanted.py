"""The wanted list: what PiTV asks pitv_content to find. PiTV never downloads or encodes;
it publishes wanted items in the content manifest and records the results from reports.

Two kinds of request. A wanted row names one title (a missing episode, a song someone asked
for) and travels in the manifest. A band request names no title at all: it says that a stretch
of a channel's day wants short items of certain genres and decades, and asks pitv_content to go
and find some (contract section 2). Bands are how a channel of music videos is built, and a
library of concert films cannot fill one.

A band short of material is met either way round. Where the channel says what to go and look
for, pitv_content searches. Where the channel's material is named rather than searched for, as
a channel built from particular creators is, the band asks for the next episode of each line-up
entry it would take, which is the first kind of request again. Without the second, such a
channel could never fill: its bands draw only on what is on disk, and nothing was putting
anything there."""

from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta
from typing import Any

from . import genres as genre_rules
from . import tool_client
from .db import DEFAULT_SETTINGS, LIVE, genre_list, get_setting, now_ts, rows_to_dicts, tx
from .lineup import carries_programmes
from .scheduler import bands
from .scheduler.library import USABLE
from .scheduler.rules import tz_of
from .scheduler.slots import episode_name

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

BAND_ITEM_MINUTES = 4                  # assumed length of a band item until the band holds some
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
    music), and a band may name its own instead. A channel that names no kind is still reported
    where its line-up holds entries the band would take, because those are what it fills from
    (`request_band_lineup`). One with neither is left alone, however thin its bands: nothing
    could act on the shortfall, and a finding nobody can answer is noise."""
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
        entries = _lineup_entries(conn, channel_id)
        for band in band_list:
            kind = band.fetch or (channel.get("fetch_kind") or "")
            item_kinds = sorted({k for b in band.kinds for k in BAND_ITEM_KINDS.get(b, ())})
            mine = airings.get(band.id, [])
            mine_entries = [e for e in entries if band.wants(e)]
            if (not kind and not mine_entries) or not item_kinds or not mine:
                continue
            minutes = band.max_minutes or int(channel.get("band_item_max_minutes") or default_minutes)
            if band.feature:
                # A band billed as a concert wants one long item an airing, not a run of short
                # ones, and may not repeat it within the feature repeat gap.
                repeat_days = channel.get("band_feature_repeat_days")
                if repeat_days is None:
                    repeat_days = settings.get("band_feature_repeat_days", 14)
                want = _airings_within(int(repeat_days) * 24, band)
                have, _ = _matching_items(conn, band, item_kinds, minutes * 60, feature=True)
            else:
                longest = max(end - start for start, end in mine) // 60
                have, typical = _matching_items(conn, band, item_kinds, minutes * 60)
                # What the band already holds says how long its items run; until it holds any,
                # the assumed length stands. Never longer than the band, or a band shorter than
                # one of its own items would decide it needs none.
                each = min(typical or BAND_ITEM_MINUTES, longest) or BAND_ITEM_MINUTES
                items_per_airing = max(1, int((longest + each - 1) // each))
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
            if have < want and now - (band.last_fetch_at or 0) >= int(settings.get("band_fetch_gap_hours", 1)) * 3600:
                out.append({"band": band, "channel": channel, "kind": kind, "have": have, "want": want,
                            "minutes": minutes, "next_ts": min(start for start, _ in mine),
                            "entries": mine_entries})
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


def _lineup_entries(conn: sqlite3.Connection, channel_id: int) -> list[dict[str, Any]]:
    """A channel's line-up entries that name material rather than hold it: a series somebody
    added whose episodes are fetched one at a time. Shaped like an item so a band's own test
    can be used on it unchanged, which is what stops this module having a second opinion about
    what a band wants."""
    rows = rows_to_dicts(conn.execute(
        "SELECT id, title, year, genres, next_episode, episode_count, episode_minutes, transient"
        " FROM lineup WHERE channel_id = ? AND enabled = 1 AND source != 'library'"
        " AND kind = 'show' AND show_id IS NULL ORDER BY id", (channel_id,)))
    for e in rows:
        e["genres"] = genre_list(e.get("genres"))
    return rows


def _asked_episodes(conn: sqlite3.Connection, lineup_ids: list[int]) -> tuple[dict[int, set[int]], dict[int, int]]:
    """Per entry: the episode numbers ever requested, so none is asked for twice, and how many
    of those are still outstanding. The second is what says whether a band has enough on order:
    a band is not short merely because its material has not arrived yet, and asking again for
    what is already queued is how a queue becomes a list nobody can work through."""
    if not lineup_ids:
        return {}, {}
    marks = ",".join("?" * len(lineup_ids))
    asked: dict[int, set[int]] = {i: set() for i in lineup_ids}
    open_now: dict[int, int] = dict.fromkeys(lineup_ids, 0)
    for w in conn.execute(f"SELECT lineup_id, episode, status FROM wanted WHERE lineup_id IN ({marks})", lineup_ids):
        lineup_id = int(w["lineup_id"])
        asked[lineup_id].add(int(w["episode"] or 0))
        if w["status"] not in ("done", "failed"):
            open_now[lineup_id] += 1
    return asked, open_now


def unairable(conn: sqlite3.Connection, settings: dict[str, Any]) -> list[dict[str, Any]]:
    """A channel's own material that no band of it could ever air, and why.

    A band takes an item that suits its genres, is short enough to be one of several (or is the
    one feature of a band billed that way), and fits inside the band's stretch. Material that
    fails every band on every count is not merely waiting its turn: it will sit in the cache for
    ever, and nothing else in the system would say so. A full concert among three-minute videos
    and a three hour podcast in a two hour band both land here, and both are answered by
    lengthening a band or giving the material one of its own.

    Only a channel built from bands is examined: where a pattern places programmes, anything the
    bands cannot use is aired by the pattern instead."""
    out = []
    default_minutes = int(settings.get("band_item_max_minutes", bands.ITEM_MINUTES))
    channels = {c["id"]: c for c in rows_to_dicts(conn.execute("SELECT * FROM channels WHERE enabled = 1"))}
    now, tz = now_ts(), tz_of(conn)
    for channel_id, band_list in bands.load(conn).items():
        channel = channels.get(channel_id)
        if channel is None or carries_programmes(channel):
            continue
        longest = {b.id: max((e - s for s, e in _airings(band_list, settings, now, tz).get(b.id, [])), default=0)
                   for b in band_list}
        rows = rows_to_dicts(conn.execute(
            f"SELECT id, title, genres, year, duration, concert, kind FROM media"
            f" WHERE home_channel_id = ? AND {USABLE} AND duration > 0", (channel_id,)))
        stranded = []
        for r in rows:
            item = {"genres": genre_list(r["genres"]), "year": r["year"], "duration": r["duration"],
                    "concert": r["concert"], "kind": r["kind"]}
            if any(b.wants(item) and float(r["duration"]) <= longest[b.id]
                   and bands.is_feature(item, b.max_minutes or int(channel.get("band_item_max_minutes") or default_minutes)) == b.feature
                   for b in band_list):
                continue
            stranded.append(r)
        if stranded:
            worst = max(stranded, key=lambda r: float(r["duration"]))
            out.append({"channel": channel["name"], "items": len(stranded),
                        "longest_minutes": round(float(worst["duration"]) / 60),
                        "longest_title": worst["title"],
                        "longest_band_minutes": round(max(longest.values(), default=0) / 60)})
    return out


def card_waiting_for(conn: sqlite3.Connection, now: int | None = None) -> dict[int, int]:
    """For each line-up entry, when the first holding card it could fill goes on air.

    A request with no slot has no air time of its own, so there is nothing to judge it by and
    everything sits in the order it was written. Ranking by what raised it does not help for
    long: as more channels are built from line-ups, "a line-up raised it" becomes true of
    everything and says nothing. What does not go stale is the gap on screen. A band showing a
    card at eight tomorrow morning is waiting; one whose card is on Sunday can wait; a series
    gap behind material that already plays is not waiting at all.

    So an entry is worth as much as the soonest card it could fill, taken band by band rather
    than channel by channel: a channel of nine bands has nine different answers and giving them
    all the earliest is the same as giving them nothing."""
    now = now or now_ts()
    cards = conn.execute(
        "SELECT channel_id, block, MIN(start_ts) AS at FROM schedule"
        " WHERE kind = 'filler' AND block IS NOT NULL AND replay = 0 AND start_ts > ?"
        " GROUP BY channel_id, block", (now,)).fetchall()
    if not cards:
        return {}
    by_channel: dict[int, dict[str, int]] = {}
    for r in cards:
        by_channel.setdefault(int(r["channel_id"]), {})[r["block"]] = int(r["at"])
    entries = rows_to_dicts(conn.execute(
        "SELECT id, channel_id, genres, year FROM lineup WHERE enabled = 1 AND source != 'library'"))
    for e in entries:
        e["genres"] = genre_list(e["genres"])
    out: dict[int, int] = {}
    for channel_id, band_list in bands.load(conn).items():
        waiting = by_channel.get(channel_id)
        if not waiting:
            continue
        mine = [e for e in entries if e["channel_id"] == channel_id]
        for band in band_list:
            at = waiting.get(band.name)
            if at is None:
                continue
            for e in mine:
                if band.wants(e) and at < out.get(int(e["id"]), at + 1):
                    out[int(e["id"])] = at
    return out


def request_band_lineup(conn: sqlite3.Connection, settings: dict[str, Any]) -> dict[str, Any]:
    """Ask for the next videos from the line-up entries a starved band draws on.

    A channel built from named sources has nothing to search for: the sources are the answer.
    So the shortfall is met by asking each entry the band would take for its next episode, in
    turn, until the band has enough on order. They are ordinary wanted rows, so pitv_content
    fetches them as it does any other and nothing at its end changes.

    Round robin rather than one entry at a time, or a band of twelve creators would air the
    first of them for a fortnight. An entry whose run is known to have ended is passed over."""
    needs = [n for n in band_needs(conn, settings) if n.get("entries")]
    if not needs:
        return {"status": "ok", "asked": 0, "summary": "no band is waiting on its line-up"}
    # One pass declares a night's work, not a year's. Eight bands asking for their whole
    # shortfall at once put four hundred requests in front of the episodes tonight's schedule is
    # waiting for; the passes come round every few minutes and the bands fill over days.
    budget = max(1, int(settings.get("band_fetch_max", 60)))
    now = now_ts()
    every_id = sorted({int(e["id"]) for n in needs for e in n["entries"]})
    asked, on_order = _asked_episodes(conn, every_id)   # shared: a creator in two bands is asked once
    rows: list[tuple[Any, ...]] = []
    by_band: dict[int, int] = {}
    # Round robin over the bands as well as within them, so the neediest band by the timetable
    # gets its turn first but no band takes the whole budget.
    pending = [(n, list(n["entries"])) for n in needs]
    while len(rows) < budget and pending:
        before = len(rows)
        for need, entries in pending:
            if len(rows) >= budget:
                break
            # A band keeps about `band_fetch_min` videos on order and no more, topping up as
            # they arrive. Its shortfall can be hundreds: asking for all of them at once buries
            # the episodes tonight's schedule is waiting for under a list nobody can work
            # through, and a request queued for a week is a promise of work already overtaken.
            standing = sum(on_order.get(int(e["id"]), 0) for e in need["entries"])
            depth = max(1, int(settings.get("band_fetch_min", 20)))
            want = min(need["want"] - need["have"], depth) - standing
            if by_band.get(need["band"].id, 0) >= want:
                continue
            entry = entries.pop(0) if entries else None
            if entry is None:
                continue
            entries.append(entry)
            lineup_id = int(entry["id"])
            number = max(1, int(entry.get("next_episode") or 1))
            while number in asked[lineup_id]:
                number += 1
            count = entry.get("episode_count")
            if count and number > int(count):
                continue     # the run is known to end before this
            asked[lineup_id].add(number)
            by_band[need["band"].id] = by_band.get(need["band"].id, 0) + 1
            rows.append(("episode", episode_name(number), entry.get("year"), 1, number,
                         lineup_id, int(entry.get("transient") or 0), now))
        if len(rows) == before:
            break            # every entry has been asked for everything it has
    if not rows:
        return {"status": "ok", "asked": 0, "summary": "every band's line-up has been asked for"}
    with tx(conn):
        conn.executemany(
            "INSERT INTO wanted(kind, title, year, season, episode, lineup_id, transient,"
            " created_at, provider, auto) VALUES (?,?,?,?,?,?,?,?,'auto',1)", rows)
    # `last_fetch_at` is deliberately not stamped: it records a helping asked of pitv_content and
    # is what stops a band asking for another while one is queued there. These are wanted rows,
    # which the shortfall above already accounts for, and stamping it silenced the doctor and
    # the catalogue remedy for an hour over work that was not pitv_content's to do.
    summaries = [f"{n['band'].name}: {by_band[n['band'].id]}" for n in needs if by_band.get(n["band"].id)]
    summary = f"asked for {len(rows)} from the line-up ({', '.join(summaries)})"
    log.info("band line-up: %s", summary)
    return {"status": "ok", "asked": len(rows), "summary": summary}


def _matching_items(conn: sqlite3.Connection, band: bands.Band, kinds: list[str], limit_seconds: int,
                    feature: bool = False) -> tuple[int, float]:
    """How many items in the library the band could use, and how long the middle one runs.

    Usable means: of its kinds, a feature or one of several as the band wants, and what the band
    itself would take at its first, exact step (its genres, and a known year in its decades).

    The length matters because it says how many the band needs. A music band runs through videos
    of three or four minutes; a band of talks or podcasts gets through two an afternoon. Judging
    both by one assumed length asked for thirty items where two would do, and the same request
    came back every hour because the shortfall it was answering was imaginary."""
    rows = conn.execute(
        f"SELECT id, genres, year, duration, concert FROM media WHERE kind IN ({','.join('?' * len(kinds))})"
        f" AND {USABLE} AND duration > 0", tuple(kinds)).fetchall()
    minutes = max(1, limit_seconds // 60)
    usable = [float(r["duration"]) for r in rows
              if bands.is_feature({"duration": r["duration"], "concert": r["concert"]}, minutes) == feature
              and band.wants({"genres": genre_list(r["genres"]), "year": r["year"]})
              and (not band.decades or band.dated({"year": r["year"]}) is True)]
    if not usable:
        return 0, 0.0
    usable.sort()
    return len(usable), usable[len(usable) // 2] / 60


def request_band_material(conn: sqlite3.Connection, settings: dict[str, Any],
                          outstanding: dict[int, str] | None = None) -> dict[str, Any]:
    """Ask pitv_content for material for the band that needs it most.

    pitv_content queues the request and runs one job at a time (contract section 9), so
    `request_all_band_material` calls this once per starved band and the whole night's work is
    declared up front. A band is stamped once its request is accepted, so one whose genre
    nothing can satisfy does not block the rest night after night."""
    needs = [n for n in band_needs(conn, settings) if n["kind"]]
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
        # What else satisfies each of them, so pitv_content judges what it finds by the same rule
        # the band will judge it by here. Without it an open search has only the bare name, which
        # is reliable at the coarse end and not at fine distinctions: Black Sabbath comes back
        # Hard Rock, which is the right answer and satisfies a Metal band only if this says so.
        body["genre_families"] = {g: sorted(genre_rules.satisfied_by(g, band.families) - {g.lower()})
                                  for g in band.genres}
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
    # The line-up first: it names exactly what is wanted, costs pitv_content no search, and a
    # band it satisfies is one the searcher does not have to guess at.
    from_lineup = request_band_lineup(conn, settings)
    remaining = len([n for n in band_needs(conn, settings) if n["kind"]])
    if not remaining:
        return {"status": "ok", "asked": from_lineup["asked"],
                "summary": from_lineup["summary"] if from_lineup["asked"] else "every band has material",
                "jobs": [], "withdrawn": settled["cancelled"]}
    jobs: list[Any] = []
    summaries: list[str] = []
    status = "ok"
    covered = 0
    for _ in range(remaining):
        needs = [n for n in band_needs(conn, settings) if n["kind"]]
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
