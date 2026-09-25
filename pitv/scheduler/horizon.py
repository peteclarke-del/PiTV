"""The schedule horizon: building days ahead, rebuilding a day from a point, and starting over.

The `Builder` builds one channel-day; everything here decides which days to build, when, and
what to throw away first. These are the entry points the rest of PiTV uses: maintenance
extends the horizon nightly and refills thin days after an import, the admin rebuilds a day or
the lot, readiness and delivery reports rebuild a channel from a point in time."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

from ..db import all_settings, enabled_channels, now_ts, run_log_finish, run_log_start, tx
from .build import Builder, Progress
from .rules import broadcast_day_for, day_bounds, tz_of
from .slots import episode_name

log = logging.getLogger("pitv.scheduler")


def seed_for(text: str) -> int:
    """Selection is seeded from the day being built, so rebuilding the same week reproduces it."""
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)


BUILD_STALE_SECONDS = 1800   # a build still unfinished after this long is taken as dead


def claim_build(conn: sqlite3.Connection, now: int) -> int | None:
    """Take the schedule build for this caller, or None when another already holds it.

    Two builders writing the same channel-days leave every programme in the guide twice. The web
    service and the player's maintenance thread are separate processes, and emptying the schedule
    is exactly what makes maintenance decide to build, so a fresh rebuild raced it by
    construction: one produced 46 channel-days and the other 39 three seconds later, and the
    week held 5063 overlapping slots.

    The run log is the lock. The claim is made under `BEGIN IMMEDIATE`, so only one process can
    take it, and a run left unfinished for `BUILD_STALE_SECONDS` is treated as dead rather than
    blocking every build after it: a killed process cannot close its own run."""
    with tx(conn):
        row = conn.execute("SELECT id, started_at FROM run_log WHERE kind = 'schedule'"
                           " AND finished_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        if row is not None and now - int(row["started_at"] or 0) < BUILD_STALE_SECONDS:
            return None
        return run_log_start(conn, "schedule")


def build_horizon(conn: sqlite3.Connection, *, start_day: date | None = None,
                  days: int | None = None, force: bool = False,
                  channel_numbers: list[int] | None = None, seed: int | None = None,
                  progress: Progress = None, now: int | None = None,
                  run_id: int | None = None) -> dict[str, Any]:
    """Build `days` broadcast days from `start_day` for the enabled channels (or those
    numbered in `channel_numbers`). Without `force` only incomplete days are extended; with it
    everything unlocked from now on is rebuilt. Logged as a `schedule` run.

    One build at a time (`claim_build`); another already running means this one has nothing to
    add, so it says so and leaves. A caller that has cleared state first takes the claim itself
    and passes `run_id`, so it never clears a schedule it then declines to rebuild."""
    settings = all_settings(conn)
    tz = tz_of(conn)
    now = now or now_ts()
    if start_day is None:
        start_day = broadcast_day_for(now, settings, tz)
    days = days or int(settings.get("horizon_days", 7))
    if seed is None:
        seed = seed_for(start_day.isoformat())
    if run_id is None:
        run_id = claim_build(conn, now)
    if run_id is None:
        log.info("a schedule build is already running; leaving this one to it")
        return {"status": "skipped", "summary": "another schedule build is already running",
                "notes": [], "run_id": None, "start_day": start_day.isoformat(), "days": days, "built": 0}
    channels = enabled_channels(conn)
    if channel_numbers:
        channels = [c for c in channels if c["number"] in channel_numbers]
    built = 0
    programmes = 0
    notes: list[str] = []
    builder: Builder | None = None
    try:
        bring_requests_forward(conn)      # before the library reads them: a build reuses what it finds
        builder = Builder(conn, now=now, seed=seed,
                          rebuild={c["id"]: (now, None) for c in channels} if force else None)
        builder.days_built = days
        for i in range(days):
            day = start_day + timedelta(days=i)
            for channel in channels:
                slots = builder.build_channel_day(channel, day, force)
                if not slots:
                    continue
                builder.save(channel["id"], day, slots)
                n = sum(1 for s in slots if s.kind == "programme" and not s.replay)
                builder.programmes_built[channel["id"]] = builder.programmes_built.get(channel["id"], 0) + n
                programmes += n
                built += 1
                if progress:
                    progress(f"{day} {channel['name']}: {n} programmes")
        withdraw_orphaned_requests(conn)
        notes = builder.notes()
    except Exception as exc:
        # Days saved before the failure stand (each is its own transaction); the run log must
        # not be left showing a build still running.
        notes = builder.notes() if builder is not None else []
        run_log_finish(conn, run_id, "error", f"failed after {built} channel-days: {exc}", [*notes, repr(exc)])
        raise
    status = "ok" if not notes else "warning"
    summary = f"{built} channel-days built, {programmes} programmes; {len(notes)} notes"
    run_log_finish(conn, run_id, status, summary, notes)
    log.info("build from %s for %d days (force=%s): %s", start_day, days, force, summary)
    for note in notes:
        log.warning("note: %s", note)
    return {"status": status, "summary": summary, "notes": notes, "run_id": run_id,
            "start_day": start_day.isoformat(), "days": days, "built": built}


# A request the scheduler raised for itself, as opposed to one a person made in the admin.
DERIVED_WANTED = "auto = 1 OR lineup_id IS NOT NULL"


def fresh_rebuild_horizon(conn: sqlite3.Connection, *, start_day: date | None = None,
                          days: int | None = None, seed: int | None = None,
                          progress: Progress = None, now: int | None = None) -> dict[str, Any]:
    """Throw away everything PiTV worked out for itself and work it out again.

    What stays is what a person put there: channels and their bands, settings, sources, the
    line-up including titles added by hand, episode cursors set in the admin, and every request
    for material somebody asked for. What goes is everything derived from those: the whole
    generated schedule, past and locked slots included, the viewing history, the run log, the
    placeholder requests the scheduler raised for slots that no longer exist (raised again for
    the new schedule), and the record of when each band last asked for material.

    No file is forgotten, here or in pitv_content: cache copies and fetched material stay
    exactly as they are, and the caller has pitv_content publish a fresh index which is imported
    before this runs, so material fetched earlier is scheduled again like anything else.

    The build is claimed before anything is cleared, so a rebuild that cannot have the builder
    leaves the schedule alone rather than emptying it and declining to fill it again."""
    now = now or now_ts()
    run_id = claim_build(conn, now)
    if run_id is None:
        return {"status": "skipped", "summary": "another schedule build is already running;"
                                                " nothing was cleared", "notes": [], "built": 0,
                "cleared_slots": 0, "cleared_history": 0, "cleared_wanted": 0, "kept_wanted": 0,
                "withdrawn_requests": 0}
    with tx(conn):
        cleared_slots = conn.execute("SELECT COUNT(*) FROM schedule").fetchone()[0]
        cleared_history = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        cleared_wanted = conn.execute(f"SELECT COUNT(*) FROM wanted WHERE {DERIVED_WANTED}").fetchone()[0]
        kept_wanted = conn.execute(f"SELECT COUNT(*) FROM wanted WHERE NOT ({DERIVED_WANTED})").fetchone()[0]
        conn.execute("DELETE FROM schedule")
        conn.execute("DELETE FROM history")
        # Every run but this one: the claim on the build is a row in here, and deleting it
        # would hand the builder to anyone asking, in the one path where the race actually bites.
        conn.execute("DELETE FROM run_log WHERE id != ?", (run_id,))
        conn.execute(f"DELETE FROM wanted WHERE {DERIVED_WANTED}")
        conn.execute("UPDATE band SET last_fetch_at = NULL")
    result = build_horizon(conn, start_day=start_day, days=days, seed=seed, progress=progress,
                           now=now, run_id=run_id)
    return {**result, "cleared_slots": cleared_slots, "cleared_history": cleared_history,
            "cleared_wanted": cleared_wanted, "kept_wanted": kept_wanted,
            "withdrawn_requests": cleared_wanted}


def bring_requests_forward(conn: sqlite3.Connection) -> int:
    """Open requests for a remote series take the lowest episode numbers still unasked, in order,
    and the slots that use them are retitled. A request can be left far into a run (by numbering
    that once continued from the highest ever raised, or by earlier ones being withdrawn), and
    every way a build reuses it, as a spare or as the last resort's repeat, would keep it there:
    the series would open on episode 8. Settled requests (delivered or given up) keep their
    numbers. Returns how many moved."""
    moved = 0
    with tx(conn):
        for entry in conn.execute("SELECT id, next_episode FROM lineup WHERE kind = 'show' AND source != 'library'").fetchall():
            requests = conn.execute("SELECT id, episode, status FROM wanted WHERE lineup_id = ? AND kind = 'episode'"
                                    " ORDER BY episode, id", (entry["id"],)).fetchall()
            settled = {int(w["episode"] or 0) for w in requests if w["status"] != "queued"}
            number = int(entry["next_episode"] or 1)
            for w in (w for w in requests if w["status"] == "queued"):
                while number in settled:
                    number += 1
                asked = int(w["episode"] or 0)
                if number < asked:
                    conn.execute("UPDATE wanted SET episode = ?, title = ?, updated_at = ? WHERE id = ?",
                                 (number, episode_name(number), now_ts(), w["id"]))
                    conn.execute("UPDATE schedule SET subtitle = ? WHERE wanted_id = ? AND media_id IS NULL",
                                 (episode_name(number), w["id"]))
                    moved += 1
                    asked = number
                number = max(number, asked + 1)
    return moved


def withdraw_orphaned_requests(conn: sqlite3.Connection) -> int:
    """Line-up requests that no slot uses any more (their slots were rebuilt away) are withdrawn,
    so pitv_content is never asked for material nothing will air and numbering cannot drift.

    A request a slot raised is the only kind that can be orphaned, which is what `auto = 0`
    says. One raised by a standing rule has no slot by design: a band asks its line-up for stock
    before anything is scheduled to air it, and sweeping those away as orphans undid the asking
    on the very next build, so the channel asked for the same material for ever and never kept
    any of it."""
    with tx(conn):
        cur = conn.execute("DELETE FROM wanted WHERE lineup_id IS NOT NULL AND auto = 0"
                           " AND status IN ('queued', 'failed')"
                           " AND id NOT IN (SELECT wanted_id FROM schedule WHERE wanted_id IS NOT NULL)")
    return cur.rowcount


def horizon_end(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(end_ts) AS e FROM schedule").fetchone()
    return row["e"] if row and row["e"] else None


def needs_rebuild(conn: sqlite3.Connection, now: int | None = None) -> bool:
    """Whether the schedule wants extending: when the nearest channel runs out, not the furthest.

    A channel switched on after the last build has nothing at all, and judging the horizon by
    its furthest edge made that invisible: the other channels were built a week ahead, so
    nothing was due, and the new one stayed blank until the whole horizon happened to run down.
    Overnight is a long time for a channel somebody has just turned on to show nothing."""
    now = now or now_ts()
    threshold = int(all_settings(conn).get("rebuild_when_days_left", 2)) * 86400
    ends = [r["e"] for r in conn.execute(
        "SELECT MAX(s.end_ts) AS e FROM channels c LEFT JOIN schedule s ON s.channel_id = c.id"
        " WHERE c.enabled = 1 GROUP BY c.id")]
    if not ends:
        return False        # no channels: nothing to build, and a build would say the same
    return any(end is None or end - now < threshold for end in ends)


def refill_empty_days(conn: sqlite3.Connection, now: int | None = None) -> dict[str, Any]:
    """Rebuild from each channel-day's first future holding-card gap.

    A filled day can still contain an isolated hole, so judging the whole day's percentage leaves
    visibly broken schedules behind. Start at the gap itself: earlier billing remains stable,
    while newly eligible local or remote entries get another chance to fill it. Every filler is
    retried: the configured advert and ident slots are their own kinds, never filler. A
    channel-day that cannot be rebuilt is logged and counted, never passed over in silence."""
    now = now or now_ts()
    rows = conn.execute(
        "SELECT channel_id, day, MIN(MAX(start_ts, ?)) AS first_ts"
        " FROM schedule WHERE end_ts > ? AND replay = 0 AND kind = 'filler'"
        " GROUP BY channel_id, day ORDER BY first_ts, channel_id",
        (now, now),
    ).fetchall()
    days = programmes = failed = 0
    for row in rows:
        result = rebuild_from(conn, row["channel_id"], int(row["first_ts"]), now=now)
        if result["status"] == "error":
            failed += 1
            log.error("refill: channel %s on %s could not be rebuilt: %s", row["channel_id"], row["day"],
                      result["summary"])
            continue
        days += 1
        programmes += int(result.get("programmes", 0))
    summary = f"{days} gap-bearing channel-days rebuilt, {programmes} programmes"
    if failed:
        summary += f"; {failed} could not be rebuilt"
    if days or failed:
        log.info("refill: %s", summary)
    return {"status": "ok" if not failed else "warning", "days": days, "programmes": programmes,
            "failed": failed, "summary": summary}


def _first_gap(conn: sqlite3.Connection, channel_id: int, now: int, from_ts: int) -> int:
    """Where a rebuild asked to start at `from_ts` must start: the first stretch with no slot
    between now and then, if there is one.

    Everything before the start is kept as it stands, so a gap inside it is walled in by kept
    slots, and a walk that cannot move them fills it with a holding card. That aired on PiTV Toons
    from 21:33 on 25 September, where a gap of minutes should have been closed by bringing what
    followed forward. Starting at the gap rebuilds it with everything after it."""
    # Every row counts, the overnight replay included, and only the day being rebuilt is looked
    # at: without both the night before read as one long gap and the wrong day was rebuilt.
    day = broadcast_day_for(from_ts, all_settings(conn), tz_of(conn)).isoformat()
    rows = conn.execute("SELECT start_ts, end_ts FROM schedule WHERE channel_id = ? AND day = ? AND end_ts > ?"
                        " AND start_ts < ? ORDER BY start_ts", (channel_id, day, now, from_ts)).fetchall()
    for a, b in pairwise(rows):
        if b["start_ts"] - a["end_ts"] > 1 and a["end_ts"] >= now:
            return int(a["end_ts"])
    return from_ts


def rebuild_from(conn: sqlite3.Connection, channel_id: int, from_ts: int, *,
                 now: int | None = None, seed: int | None = None,
                 exclude_media_ids: set[int] | None = None, allow_external: bool = True,
                 only_media_ids: set[int] | None = None) -> dict[str, Any]:
    """Rebuild one channel from a point in time to the end of that broadcast day.

    Used by the admin schedule editor after a slot is removed, replaced or inserted, and by
    the readiness check to substitute programmes whose files are not available."""
    settings = all_settings(conn)
    tz = tz_of(conn)
    now = now or now_ts()
    from_ts = _first_gap(conn, channel_id, now, max(from_ts, now))
    day = broadcast_day_for(from_ts, settings, tz)
    if seed is None:
        seed = seed_for(f"{day.isoformat()}:{from_ts}")
    builder = Builder(conn, now=now, seed=seed, exclude_media_ids=exclude_media_ids, allow_external=allow_external,
                      only_media_ids=only_media_ids, rebuild={channel_id: (from_ts, day_bounds(day, settings, tz)[2])})
    channel = next((c for c in builder.channels if c["id"] == channel_id), None)
    if channel is None:
        return {"status": "error", "summary": "channel not found or disabled", "programmes": 0, "notes": []}
    # One transaction: losing power between dropping the unavailable slots and saving their
    # replacements would leave a hole that a later build takes for a complete day.
    with tx(conn):
        if exclude_media_ids:
            # Unavailable files must not survive as kept future slots either.
            ids = sorted(exclude_media_ids)
            conn.execute("DELETE FROM schedule WHERE channel_id = ? AND start_ts >= ? AND replay = 0"
                         f" AND media_id IN ({','.join('?' * len(ids))})", (channel_id, from_ts, *ids))
        slots = builder.build_channel_day(channel, day, force=True, from_ts=from_ts)
        builder.save(channel_id, day, slots)
    withdraw_orphaned_requests(conn)
    programmes = sum(1 for s in slots if s.kind == "programme" and not s.replay)
    notes = builder.notes()
    return {"status": "ok" if not notes else "warning", "programmes": programmes,
            "summary": f"{programmes} programmes", "notes": notes}
