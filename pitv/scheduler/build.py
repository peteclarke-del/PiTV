"""Build one channel-day.

Overview of one channel-day:

1. Fixed items are collected: slots that already exist and must be kept (locked, or
   already started), plus anchored shows (strips and weekly slots) for that weekday.
2. The gaps between fixed items are walked from 08:00 to midnight, following the
   channel's pattern (e.g. ``show, ad, ad``). Programmes are chosen by weighted random
   selection over everything eligible at that time; adverts and idents are chosen to fit.
3. The overnight replay is copied from the day's own slots.

Episode order comes from the ``next_episode`` computation: the episode after the latest
one already placed (in history or the schedule), or a cursor set in the admin UI.
"""

from __future__ import annotations

import hashlib
import logging
import random
import sqlite3
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

from ..db import (
    all_settings,
    enabled_channels,
    now_ts,
    tx,
)
from . import bands, overnight
from .library import Library, Rebuild
from .policy import SchedulerPolicy
from .rules import (
    day_bounds,
    daypart_end_minutes,
    dayparts_for_weekday,
    in_decades,
    local_ts,
    minutes_of_day,
    parse_pattern,
    tz_of,
)
from .select import Selector
from .slots import Show, Slot, json_field, seconds, slot_titles

Progress = Callable[[str], None] | None
# Upper bound on walk steps for one channel-day. Only a pattern that can never place a
# programme (adverts or idents alone) gets near it; the rest of such a day becomes filler.
MAX_STEPS_PER_DAY = 3000
FILLER_TITLE = "Programmes will continue shortly"
log = logging.getLogger("pitv.scheduler")


class Builder:
    def __init__(self, conn: sqlite3.Connection, *, now: int | None = None,
                 seed: int | None = None, exclude_media_ids: set[int] | None = None,
                 rebuild: Rebuild | None = None, allow_external: bool = True,
                 only_media_ids: set[int] | None = None) -> None:
        self.conn = conn
        self.settings = all_settings(conn)
        self.tz = tz_of(conn)
        self.now = now or now_ts()
        self.policy = SchedulerPolicy(self.settings, self.now)
        self.seed = seed if seed is not None else 0
        self.log: list[str] = []
        self.channels = enabled_channels(conn)
        # Minute of day the broadcast day starts (08:00 = 480); minutes before it belong to the
        # previous day and are counted past 1440 so comparisons stay monotonic.
        self.day_start_min = self.policy.day_start_minutes
        self.library = Library(conn, self.policy, now=self.now, rebuild=rebuild,
                               exclude_media_ids=exclude_media_ids, only_media_ids=only_media_ids,
                               allow_external=allow_external)
        self.select = Selector(self.policy, self.settings, self.tz, self.library)
        # (channel, day) -> time from which save() replaces unlocked slots; None adds only.
        self._cuts: dict[tuple[int, str], int | None] = {}
        # (lineup_id, episode) -> wanted id, so every placement of one request in this build
        # shares a row (a film's later airings, a placeholder's overnight replay).
        self._requests: dict[tuple[int, int | None], int] = {}

    def _bday(self, minute: int) -> int:
        """A local minute of day on the broadcast day's clock: +1440 after midnight."""
        return minute if minute >= self.day_start_min else minute + 1440

    def _bday_minutes(self, ts: int) -> int:
        return self._bday(minutes_of_day(ts, self.tz))

    # --- helpers -----------------------------------------------------------------------

    def _rng(self, channel_id: int, day: date) -> random.Random:
        key = f"{self.seed}:{channel_id}:{day.isoformat()}".encode()
        return random.Random(int.from_bytes(hashlib.sha256(key).digest()[:8], "big"))

    def _existing_slots(self, channel_id: int, day: str) -> list[Slot]:
        """The day's persisted slots (overnight replay excluded), in time order."""
        rows = self.conn.execute(
            "SELECT s.*, m.show_id, m.genres AS mgenres, m.year AS myear FROM schedule s"
            " LEFT JOIN media m ON m.id = s.media_id"
            " WHERE s.channel_id = ? AND s.day = ? AND s.replay = 0 ORDER BY s.start_ts",
            (channel_id, day)).fetchall()
        return [Slot(channel_id=r["channel_id"], day=r["day"], start_ts=r["start_ts"], end_ts=r["end_ts"],
                     media_id=r["media_id"], offset=r["offset"], kind=r["kind"], title=r["title"],
                     subtitle=r["subtitle"], replay=r["replay"], locked=r["locked"],
                     block=r["block"], wanted_id=r["wanted_id"], show_id=r["show_id"],
                     genres=json_field(r["mgenres"]) or [], year=r["myear"])
                for r in rows]

    # --- building ----------------------------------------------------------------------

    def _anchors_for(self, channel: dict[str, Any], day: date, day_start: int, day_end: int) -> list[tuple[int, Show]]:
        out = []
        weekday = day.weekday()
        for show in self.library.anchored.get(channel["id"], ()):
            if weekday not in show.anchor_days:
                continue
            if show.resting_until and day_start < show.resting_until:
                continue
            ts = local_ts(day, show.anchor_time, self.tz)
            if ts < day_start:   # after midnight, so on the next calendar day
                ts = local_ts(day + timedelta(days=1), show.anchor_time, self.tz)
            if day_start <= ts < day_end:
                out.append((ts, show))
        out.sort(key=lambda x: x[0])
        return out

    def _keep_slots(self, channel_id: int, day_str: str, day_end: int, force: bool,
                    from_ts: int | None) -> list[Slot] | None:
        """The day's slots that survive this build, in time order, or None when the day is
        already complete and nothing was asked to be rebuilt. Records where `save()` cuts.

        force: rebuild everything that has not started and is not locked.
        from_ts: rebuild from this time onwards only (implies force for that part)."""
        existing = self._existing_slots(channel_id, day_str)
        if force or from_ts is not None:
            cut = max(self.now, from_ts) if from_ts is not None else self.now
            self._cuts[(channel_id, day_str)] = cut
            # A holding card is not content already in progress. Rebuild its remaining time;
            # save() truncates the persisted slot at the cut so the past stays truthful.
            return [s for s in existing if s.locked or (s.start_ts < cut and not (
                    s.kind == "filler" and not s.locked and s.end_ts > cut))]
        if existing and max(s.end_ts for s in existing) >= day_end - 60:
            return None
        self._cuts[(channel_id, day_str)] = None
        return existing

    def _adjacent_show(self, channel_id: int, ts: int, before: bool) -> int | None:
        """show_id of the nearest programme across a day boundary: the last one starting before
        ts (the previous day's tail) or the first starting at or after it (the next day's
        opening), looking no further than 12 hours. Adverts, idents and a closedown filler
        between the two do not break the no-same-series-back-to-back rule."""
        if before:
            where, params, order = "s.start_ts < ? AND s.start_ts > ?", (channel_id, ts, ts - 12 * 3600), "DESC"
        else:
            where, params, order = "s.start_ts >= ? AND s.start_ts < ?", (channel_id, ts, ts + 12 * 3600), "ASC"
        row = self.conn.execute(
            "SELECT m.show_id FROM schedule s JOIN media m ON m.id = s.media_id WHERE s.channel_id = ?"
            f" AND {where} AND s.kind = 'programme' ORDER BY s.start_ts {order} LIMIT 1", params).fetchone()
        return row["show_id"] if row else None

    def _filler(self, channel: dict[str, Any], day_str: str, start: int, end: int, title: str = FILLER_TITLE,
                **extra: Any) -> Slot:
        return Slot(channel_id=channel["id"], day=day_str, start_ts=start, end_ts=end, media_id=None,
                    offset=0, kind="filler", title=title, **extra)

    def build_channel_day(self, channel: dict[str, Any], day: date, force: bool,
                          from_ts: int | None = None) -> list[Slot]:
        """Build (or complete) one channel-day. Returns the new slots, or [] if nothing to do
        (see `_keep_slots` for `force` and `from_ts`)."""
        day_str = day.isoformat()
        day_start, day_end, next_day_start = day_bounds(day, self.settings, self.tz)
        rng = self._rng(channel["id"], day)

        keep = self._keep_slots(channel["id"], day_str, day_end, force, from_ts)
        if keep is None:
            return []
        placed_today: dict[int, int] = {}
        for s in keep:
            if s.show_id:
                placed_today[s.show_id] = placed_today.get(s.show_id, 0) + 1

        fixed: list[tuple[int, int, Any]] = [(s.start_ts, s.end_ts, s) for s in keep]
        for ts, show in self._anchors_for(channel, day, day_start, day_end):
            if placed_today.get(show.id):
                continue
            ep = show.next_episode()
            if ep is None:
                continue
            if channel.get("strict_matching") and not show.pinned and not (ep.get("genres") and ep.get("year")):
                continue     # this channel takes only what the index has labelled, anchors included
            end = ts + seconds(ep)
            if any(not (end <= fs or ts >= fe) for fs, fe, _ in fixed):
                self.log.append(f"{channel['name']} {day_str}: anchor {show.title} at {show.anchor_time} clashes with a kept slot")
                continue
            fixed.append((ts, end, ("anchor", show, ep)))
            placed_today[show.id] = placed_today.get(show.id, 0) + 1
        day_bands = bands.timetable(self.library.bands.get(channel["id"], []), day, day_start, day_end,
                                    next_day_start, self.day_start_min, self.tz)
        filler = self._band_filler(channel, day, [b for _, _, b in day_bands], rng) if day_bands else None
        for ts, end, band in day_bands:
            if any(not (end <= fs or ts >= fe) for fs, fe, _ in fixed):
                self.log.append(f"{channel['name']} {day_str}: band {band.name} at {band.start} clashes with a kept slot")
                continue
            fixed.append((ts, end, ("band", band)))
        fixed.sort(key=lambda x: x[0])

        # An empty pattern means the channel places no programmes of its own: its day is its
        # bands, and anything they leave is padded.
        pattern_text = (channel.get("pattern") or "").strip()
        pattern = parse_pattern(pattern_text) if pattern_text else []
        ads_on = bool(channel.get("ads_enabled"))
        ads_per_break = int(channel.get("ads_per_break") or 2)
        if not ads_on and pattern:
            pattern = [tok for tok in pattern if tok not in ("ad", "break")] or ["show"]
        rounding = self.policy.start_rounding_seconds
        tol = self.policy.duration_tolerance_seconds

        new_slots: list[Slot] = []
        all_slots: list[Slot] = list(keep)
        t = day_start
        pat_idx = 0
        # Kept slots update these (and the advert repeat times) as the walk passes them, so
        # nothing is seeded from `keep`: its last entry may be a locked slot late in the day.
        # What precedes 08:00 is the tail of yesterday's overnight replay.
        prev: Slot | None = None
        last_programme_year: int | None = None
        last_show_id = self._adjacent_show(channel["id"], day_start, before=True)
        steps = 0
        fixed_queue = deque(fixed)

        def emit(slot: Slot) -> None:
            nonlocal prev
            new_slots.append(slot)
            all_slots.append(slot)
            prev = slot

        break_cap = self.policy.advert_break_seconds

        def break_state() -> tuple[int, int]:
            """Seconds and number of adverts in the consecutive break currently under way.

            The pattern's own breaks, the tidy-up to a round start time and the padding of a
            gap all draw on the same limits, so they cannot silently turn two configured adverts
            into a much longer break."""
            seconds = count = 0
            for s in reversed(all_slots):
                if s.kind != "advert":
                    break
                seconds += s.duration
                count += 1
            return seconds, count

        def break_room() -> int:
            return max(0, break_cap - break_state()[0])

        def advert_count_room() -> int:
            return max(0, ads_per_break - break_state()[1])

        def fill_to(target: int, note: bool = False) -> None:
            """Close the gap up to `target` with adverts or idents, then filler, so the
            channel-day stays contiguous (the guide and the player both rely on that)."""
            nonlocal t
            t = self._pad(channel, rng, day_str, t, target, last_programme_year, emit,
                          advert_room=break_room(), advert_limit=advert_count_room())
            if t < target:
                if note and target - t > 120:
                    self.log.append(f"{channel['name']} {day_str}: filler {(target - t) // 60} min at {self._hhmm(t)}")
                emit(self._filler(channel, day_str, t, target))
                t = target

        # A band or programme that began before closedown finishes; the walk stops looking for
        # anything new at closedown.
        while t < day_end and steps < MAX_STEPS_PER_DAY:
            steps += 1
            # A fixed item starting now (or that we have run into)?
            if fixed_queue and t >= fixed_queue[0][0] - 60:
                fs, fe, payload = fixed_queue.popleft()
                if t < fs:
                    fill_to(fs)
                if isinstance(payload, Slot):
                    t = max(t, fe)
                    prev = payload
                    if payload.kind == "programme":
                        last_show_id = payload.show_id
                        last_programme_year = payload.year
                    elif payload.kind == "advert" and payload.media_id:
                        self.library.ad_last[(channel["id"], payload.media_id)] = payload.start_ts
                    continue
                if payload[0] == "band":
                    # A band that runs to closedown, or past it, may let its final item finish;
                    # the overnight starts when the band does end. A band that ends early gives
                    # its time back to the channel; the next band starts when the timetable says.
                    t = self._fill_band(channel, day_str, payload[1], max(t, fs), fe, filler, emit,
                                        hard_end=next_day_start if not fixed_queue else None)
                    continue
                _, show, ep = payload
                start = max(t, fs)
                slot = self._programme_slot(channel, day_str, start, ep, show)
                emit(slot)
                show.advance(start)
                self.library.show_last_placed[show.id] = start
                last_programme_year = ep.get("year")
                last_show_id = show.id
                t = slot.end_ts
                pat_idx += 1  # the anchor stands in for a 'show' token
                continue
            boundary = fixed_queue[0][0] if fixed_queue else day_end
            gap = boundary - t
            if gap <= 0:
                t = boundary
                continue
            if not pattern:
                # A bands-only channel: what the bands leave is still its own airtime, so it is
                # filled from the same material with no band name, and the guide lists each item
                # by its own title rather than under a band it does not belong to.
                t = self._fill_free(channel, day_str, t, boundary, filler, day_bands, emit)
                if t < boundary:
                    fill_to(boundary)
                continue
            token = pattern[pat_idx % len(pattern)]
            pat_idx += 1

            if token in ("ad", "break"):
                requested = ads_per_break if token == "break" else 1
                for _ in range(min(requested, advert_count_room())):
                    room = min(boundary - t, break_room())
                    ad = self.select.advert(channel, rng, t, room, last_programme_year) if room > 0 else None
                    if ad is None:
                        break
                    slot = self._media_slot(channel, day_str, t, ad, "advert")
                    emit(slot)
                    self.library.ad_last[(channel["id"], ad["id"])] = t
                    t = slot.end_ts
                continue
            if token == "ident":
                ident = self.select.ident(channel, rng, boundary - t) or self.select.stand_in_ident(channel, boundary - t)
                if ident is not None:
                    slot = self._media_slot(channel, day_str, t, ident, "ident")
                    emit(slot)
                    t = slot.end_ts
                continue

            if rounding and ads_on:
                # Tidy start time: pad with adverts up to the next rounding boundary, as far as
                # the break allowance goes; a programme may start off the five minutes rather
                # than sit behind a longer break.
                target = -(-t // rounding) * rounding
                room = break_room()
                if 0 < target - t <= break_cap and target < boundary and room > 0:
                    t = self._pad(channel, rng, day_str, t, min(target, t + room), last_programme_year, emit,
                                  advert_room=room, advert_limit=advert_count_room())
                    gap = boundary - t
            slack = 0
            if not fixed_queue:
                # Closedown is when the final programme may start, not when it is cut off. It may
                # finish before tomorrow's broadcast day; overnight replay simply starts later.
                slack = max(tol, next_day_start - day_end)

            next_fixed = fixed_queue[0][2] if fixed_queue else None
            next_show_id = (next_fixed.show_id if isinstance(next_fixed, Slot)
                            else next_fixed[1].id if next_fixed else None)
            barred = {x for x in (last_show_id, next_show_id) if x}
            attempts = [(token, 0)] + ([("show", 0)] if token != "show" else []) + [("show", 1), ("show", 2)]
            choice = None
            for tok, relax in attempts:
                choice = self.select.programme(channel, rng, t, gap, tok, placed_today, prev,
                                                barred, relax=relax, slack=slack)
                if choice is not None:
                    break
            if choice is None:
                # Eligibility changes later in the day: certificate watersheds and daypart
                # weights can make a sparse channel viable even though nothing fits now.  Do
                # not turn the first miss into one holding card through closedown; retry at the
                # next daypart boundary.
                start_min = self._bday_minutes(t)
                dayparts = dayparts_for_weekday(
                    datetime.fromtimestamp(t, self.tz).weekday(), self.settings,
                    self.select.channel_json(channel, "daypart_profile"))
                retry_min = daypart_end_minutes(start_min, dayparts, 1440)
                retry = min(boundary, t + max(60, retry_min - start_min) * 60)
                fill_to(retry, note=True)
                continue
            item, show = choice
            if item["id"] < 0:   # external line-up entry: placeholder slot plus a wanted request
                slot = self._external_slot(channel, day_str, t, item)
                emit(slot)
                placed_today[item["id"]] = placed_today.get(item["id"], 0) + 1
                if not item.get("_external_repeat"):
                    self.library.external_last_placed[item["lineup_id"]] = t
                last_show_id = item["id"] if item["kind"] == "episode" else None
                last_programme_year = item.get("year")
                t = slot.end_ts
                if item["kind"] == "episode":
                    t = self._external_short_episode_run(
                        channel, day_str, item, slot, gap - slot.duration, emit,
                        repeating=bool(item.get("_external_repeat")),
                    )
                continue
            slot = self._programme_slot(channel, day_str, t, item, show)
            emit(slot)
            if show is not None:
                series_repeat = bool(item.get("_series_repeat"))
                if not series_repeat:
                    show.advance(t)
                    self.library.show_last_placed[show.id] = t
                placed_today[show.id] = placed_today.get(show.id, 0) + 1
                last_show_id = show.id
            else:
                last_show_id = None
                self.library.movie_placements.setdefault(item["id"], []).append(t)
            self.library.last_placed[item["id"]] = t
            last_programme_year = item.get("year")
            t = slot.end_ts
            if show is not None:
                t = self._short_episode_run(
                    channel, day_str, show, item, slot, gap - slot.duration, emit,
                    repeating=bool(item.get("_series_repeat")),
                )

        if t < day_end:
            self.log.append(f"{channel['name']} {day_str}: gave up after {MAX_STEPS_PER_DAY} steps at"
                            f" {self._hhmm(t)}; check the channel pattern")
            for fs, fe, payload in fixed_queue:
                if isinstance(payload, Slot):   # kept slots stay; only the gaps become filler
                    if t < fs:
                        emit(self._filler(channel, day_str, t, fs))
                    t = max(t, fe)
            if t < day_end:
                emit(self._filler(channel, day_str, t, day_end))
        if not pattern and filler is not None:
            # A channel of bands carries on through the night from its own material, with the
            # repeat gaps holding, rather than replaying a thin day.
            return new_slots + self._overnight_from_pool(channel, day, day_end, next_day_start, all_slots,
                                                         filler, day_bands)
        return new_slots + self._overnight(channel, day, day_end, next_day_start, all_slots)

    # --- bands ---------------------------------------------------------------------------

    def _band_filler(self, channel: dict[str, Any], day: date, todays: list[bands.Band],
                     rng: random.Random) -> bands.Filler:
        kinds = {k for b in todays for k in b.kinds}
        decades = self.select.channel_decades(channel)
        pool = [m for kind in sorted(kinds) for m in self.library.band_pool(kind)
                if in_decades(m.get("year"), decades, unknown_ok=not channel.get("strict_matching"))]
        if not pool:
            self.log.append(f"{channel['name']} {day.isoformat()}: nothing in the library for its bands ({', '.join(sorted(kinds))})")
        item_minutes, item_repeat, feature_repeat = self.policy.band_limits(channel)
        return bands.Filler(todays, pool, rng=rng, last_placed=self.library.last_placed,
                            item_minutes=item_minutes,
                            strict=bool(channel.get("strict_matching")),
                            item_repeat=item_repeat, feature_repeat=feature_repeat)

    def _fill_band(self, channel: dict[str, Any], day_str: str, band: bands.Band, start: int, end: int,
                   filler: bands.Filler | None, emit: Callable[[Slot], None],
                   hard_end: int | None = None) -> int:
        """Emit one band's items under its name (the guide shows them as one programme) and
        return where it ended; see `bands.fill_band` for what goes in and why it may end early."""
        if filler is None:
            emit(self._filler(channel, day_str, start, end, title=band.name, block=band.name))
            return end
        placed, t = bands.fill_band(band, start, end, filler, hard_end)
        for at, item in placed:
            emit(self._programme_slot(channel, day_str, at, item, None, block=band.name))
        return t

    def _fill_free(self, channel: dict[str, Any], day_str: str, start: int, end: int,
                   filler: bands.Filler | None, day_bands: list[tuple[int, int, bands.Band]],
                   emit: Callable[[Slot], None]) -> int:
        """Emit the channel's own items over time its bands leave; see `bands.fill_free`."""
        if filler is None or start >= end:
            return start
        kinds = tuple(sorted({k for _, _, b in day_bands for k in b.kinds}))
        placed, t = bands.fill_free(channel["id"], kinds, self.select.channel_decades(channel), start, end, filler)
        for at, item in placed:
            emit(self._programme_slot(channel, day_str, at, item, None))
        return t

    def _pad(self, channel: dict[str, Any], rng: random.Random, day_str: str, t: int, target: int,
             near_year: int | None, emit: Callable[[Slot], None], advert_room: int | None = None,
             advert_limit: int | None = None) -> int:
        """Fill t..target with adverts (ad channels) or idents; returns the new t.

        A break is a break, not a filibuster: adverts stop when `advert_room` (what is left of
        `max_break_minutes` after the adverts already running) is used up, however wide the
        gap, because a channel that fills twenty minutes with adverts is unwatchable and no
        station ever did it. Idents are continuity clips, so at most two run together; whatever
        is left after both becomes filler, which the caller adds."""
        guard = 0
        idents = 0
        adverts = 0
        if advert_room is None:
            advert_room = self.policy.advert_break_seconds
        break_end = t + advert_room
        while t < target and guard < 20:
            guard += 1
            gap = target - t
            item = None
            kind = "advert"
            if (channel.get("ads_enabled") and t < break_end
                    and (advert_limit is None or adverts < advert_limit)):
                item = self.select.advert(channel, rng, t, min(gap, break_end - t), near_year)
            if item is None and idents < 2:
                item = self.select.ident(channel, rng, gap)
                kind = "ident"
                idents += 1
            if item is None:
                break
            slot = self._media_slot(channel, day_str, t, item, kind)
            emit(slot)
            if kind == "advert":
                adverts += 1
                self.library.ad_last[(channel["id"], item["id"])] = t
            t = slot.end_ts
        return t

    def _programme_slot(self, channel: dict[str, Any], day_str: str, start: int, item: dict[str, Any],
                        show: Show | None, block: str | None = None) -> Slot:
        title, subtitle = slot_titles(item, show.title if show else None)
        return Slot(channel_id=channel["id"], day=day_str, start_ts=start, end_ts=start + seconds(item),
                    media_id=item["id"], offset=0, kind="programme", title=title, subtitle=subtitle,
                    show_id=show.id if show else None, genres=item.get("genres") or [],
                    year=item.get("year"), block=block)

    def _external_slot(self, channel: dict[str, Any], day_str: str, start: int, e: dict[str, Any]) -> Slot:
        """A programme that is not on disk yet. Episodes number on from the entry's counter; a
        wanted row raised by an earlier build for a slot since replaced is reused first."""
        duration = int(e["duration"])
        if e["kind"] == "episode":
            if e.get("_repeat_spec"):
                spec = dict(e["_repeat_spec"])
                number = int(spec["episode"] or 1)
            elif e.get("_external_repeat"):
                spec = dict(e["last_spec"])
                number = int(spec["episode"] or 1)
            elif e["spare_wanted"]:
                spare = e["spare_wanted"].pop(0)
                number, reuse = int(spare["episode"] or 1), int(spare["id"])
                spec = {"kind": "episode", "lineup_id": e["lineup_id"], "title": f"Episode {number}", "season": 1,
                        "episode": number, "year": e.get("year"), "reuse": reuse}
            else:
                number, reuse = e["next_number"], None
                e["next_number"] += 1
                spec = {"kind": "episode", "lineup_id": e["lineup_id"], "title": f"Episode {number}", "season": 1,
                        "episode": number, "year": e.get("year"), "reuse": reuse}
            subtitle = f"Episode {number}"
            if not e.get("_external_repeat"):
                e["last_spec"] = dict(spec)
        else:
            # One request serves every airing of a film, so a spare is shared rather than used up.
            spare = e["spare_wanted"][0] if e["spare_wanted"] else None
            subtitle = f"({e['year']})" if e.get("year") else ""
            spec = {"kind": "movie", "lineup_id": e["lineup_id"], "title": e["title"], "season": None,
                    "episode": None, "year": e.get("year"), "reuse": int(spare["id"]) if spare else None}
        return Slot(channel_id=channel["id"], day=day_str, start_ts=start, end_ts=start + duration, media_id=None,
                    offset=0, kind="programme", title=e["title"], subtitle=subtitle, show_id=None,
                    genres=e.get("genres") or [], year=e.get("year"), wanted_spec=spec,
                    replay=1 if e.get("_external_repeat") else 0)

    def _external_short_episode_run(self, channel: dict[str, Any], day_str: str,
                                    entry: dict[str, Any], first: Slot, room: int,
                                    emit: Callable[[Slot], None], *, repeating: bool) -> int:
        """Bundle short remote episodes exactly as local episodes are bundled.

        Each component gets its own wanted request so pitv_content can prepare the complete
        programme. A later cadence repeat reuses those requests in the same order.
        """
        threshold, target = self.policy.short_episode_seconds(channel)
        if not threshold or not target or first.duration >= threshold:
            return first.end_ts
        first.block = first.block or entry["title"]
        t = first.end_ts
        cached = self.library.external_short_runs.get(entry["lineup_id"]) if repeating else None
        specs = list(cached[1:]) if cached else []
        run = list(cached) if cached else [dict(first.wanted_spec or {})]
        fresh = dict(entry)
        fresh.pop("_external_repeat", None)
        fresh.pop("_repeat_spec", None)
        while t - first.start_ts < target and room >= int(entry["duration"]):
            if specs:
                component = {**entry, "_external_repeat": True, "_repeat_spec": specs.pop(0)}
            elif cached:
                break
            else:
                component = fresh
            slot = self._external_slot(channel, day_str, t, component)
            slot.block = first.block
            emit(slot)
            if not cached:
                run.append(dict(slot.wanted_spec or {}))
                self.library.external_last_placed[entry["lineup_id"]] = t
            room -= slot.duration
            t = slot.end_ts
        if not cached:
            self.library.external_short_runs[entry["lineup_id"]] = run
        return t

    def _next_day_slots(self, channel_id: int, next_day_start: int) -> list[Slot]:
        """Tomorrow's own slots, if tomorrow has been built: the overnight's last resort."""
        row = self.conn.execute("SELECT day FROM schedule WHERE channel_id = ? AND start_ts >= ? AND replay = 0"
                                " AND kind = 'programme' ORDER BY start_ts LIMIT 1",
                                (channel_id, next_day_start)).fetchone()
        return self._existing_slots(channel_id, row["day"]) if row else []

    def _short_episode_run(self, channel: dict[str, Any], day_str: str, show: Show,
                           first_item: dict[str, Any], first: Slot, room: int,
                           emit: Callable[[Slot], None], *, repeating: bool = False) -> int:
        """Run several short episodes of one series together, and return where the run ends.

        A five minute cartoon on its own leaves the day in scraps and the guide unreadable, so
        episodes under `short_episode_minutes` are followed straight away by the next ones, in
        order, until the run reaches `short_episode_run_minutes`. They share the series title as
        their block, so the guide shows one entry, as it does for a band."""
        threshold, target = self.policy.short_episode_seconds(channel)
        if not threshold or not target or first.duration >= threshold:
            return first.end_ts
        first.block = first.block or show.title
        t = first.end_ts
        cached = self.library.short_runs.get(show.id) if repeating else None
        episodes = list(cached[1:]) if cached else []
        run = list(cached) if cached else [first_item]
        while t - first.start_ts < target:
            if episodes:
                episode = episodes.pop(0)
                if seconds(episode) > room:
                    break
            else:
                episode = show.take_short(min(room, threshold))
                if episode is None:
                    break
                run.append(episode)
            slot = self._programme_slot(channel, day_str, t, episode, show, block=first.block)
            emit(slot)
            self.library.last_placed[episode["id"]] = t
            room -= seconds(episode)
            t = slot.end_ts
        # Component episodes are one programme for variety and daily-limit purposes.
        if not cached:
            self.library.short_runs[show.id] = run
        return t

    def _media_slot(self, channel: dict[str, Any], day_str: str, start: int, item: dict[str, Any], kind: str) -> Slot:
        """An advert or ident slot; the subtitle is just the year."""
        return Slot(channel_id=channel["id"], day=day_str, start_ts=start, end_ts=start + seconds(item),
                    media_id=item["id"], offset=0, kind=kind, title=item["title"],
                    subtitle=str(item.get("year") or ""), year=item.get("year"))

    def _overnight(self, channel: dict[str, Any], day: date, day_end: int, next_day_start: int,
                   day_slots: list[Slot]) -> list[Slot]:
        """The replay of the day; see `overnight.replay`."""
        day_str = day.isoformat()
        return overnight.replay(
            channel, day, day_end, next_day_start, day_slots, policy=self.policy, tz=self.tz,
            day_start_min=self.day_start_min,
            next_day_slots=lambda: self._next_day_slots(channel["id"], next_day_start),
            tomorrow_first=self._adjacent_show(channel["id"], next_day_start, before=False),
            caption=lambda start, end: self._filler(channel, day_str, start, end,
                                                    title=f"Programmes will resume at {self.policy.day_start}",
                                                    replay=1))

    def _overnight_from_pool(self, channel: dict[str, Any], day: date, day_end: int, next_day_start: int,
                             day_slots: list[Slot], filler: bands.Filler,
                             day_bands: list[tuple[int, int, bands.Band]]) -> list[Slot]:
        """The small hours on a channel of bands; see `overnight.from_pool`."""
        day_str = day.isoformat()
        kinds = tuple(sorted({k for _, _, b in day_bands for k in b.kinds}))
        placed, t = overnight.from_pool(channel["id"], kinds, self.select.channel_decades(channel),
                                        day_end, next_day_start, day_slots, filler)
        out = [replace(self._programme_slot(channel, day_str, at, item, None), replay=1) for at, item in placed]
        if t < next_day_start:
            out.append(self._filler(channel, day_str, t, next_day_start,
                                    title=f"Programmes will resume at {self.policy.day_start}", replay=1))
        return out

    def _hhmm(self, ts: int) -> str:
        return datetime.fromtimestamp(ts, self.tz).strftime("%H:%M")

    # --- persistence ---------------------------------------------------------------------

    def save(self, channel_id: int, day: date, slots: list[Slot]) -> None:
        """Persist a channel-day built by `build_channel_day`: replace the old overnight replay
        and every unlocked slot from the build's cut point, then insert the new slots."""
        day_str = day.isoformat()
        cut = self._cuts.pop((channel_id, day_str), None)
        conn = self.conn
        with tx(conn):
            conn.execute("DELETE FROM schedule WHERE channel_id = ? AND day = ? AND replay = 1",
                         (channel_id, day_str))
            if cut is not None:
                conn.execute("UPDATE schedule SET end_ts = ? WHERE channel_id = ? AND day = ? AND locked = 0"
                             " AND kind = 'filler' AND start_ts < ? AND end_ts > ? AND replay = 0",
                             (cut, channel_id, day_str, cut, cut))
                conn.execute("DELETE FROM schedule WHERE channel_id = ? AND day = ? AND locked = 0"
                             " AND start_ts >= ? AND replay = 0", (channel_id, day_str, cut))
            self._raise_wanted(slots)
            conn.executemany(
                "INSERT INTO schedule(channel_id, day, start_ts, end_ts, media_id, offset, kind,"
                " replay, locked, title, subtitle, block, wanted_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(s.channel_id, s.day, s.start_ts, s.end_ts, s.media_id, s.offset, s.kind,
                  s.replay, s.locked, s.title, s.subtitle, s.block, s.wanted_id) for s in slots])

    def _raise_wanted(self, slots: list[Slot]) -> None:
        """Turn placeholder slots into wanted rows for pitv_content, one per episode or film;
        every other placement of the same episode or film in this build shares the row."""
        for sl in slots:
            spec = sl.wanted_spec
            if not spec:
                continue
            key = (spec["lineup_id"], spec["episode"])
            wid = self._requests.get(key)
            if wid is None:
                if spec.get("reuse"):
                    wid = int(spec["reuse"])
                else:
                    cur = self.conn.execute(
                        "INSERT INTO wanted(kind, title, year, season, episode, provider, lineup_id, transient, created_at)"
                        " VALUES (?,?,?,?,?,'auto',?,1,?)",
                        (spec["kind"], spec["title"], spec["year"], spec["season"], spec["episode"],
                         spec["lineup_id"], now_ts()))
                    wid = int(cur.lastrowid)
                self._requests[key] = wid
            sl.wanted_id = wid
