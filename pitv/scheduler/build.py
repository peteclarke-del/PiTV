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
from dataclasses import dataclass, field, replace
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
from .runs import Runs
from .select import Selector
from .slots import (
    FILLER_TITLE,
    Show,
    Slot,
    filler_slot,
    json_field,
    media_slot,
    programme_slot,
    seconds,
)

Progress = Callable[[str], None] | None
# Upper bound on walk steps for one channel-day. Only a pattern that can never place a
# programme (adverts or idents alone) gets near it; the rest of such a day becomes filler.
MAX_STEPS_PER_DAY = 3000
log = logging.getLogger("pitv.scheduler")


@dataclass
class Walk:
    """Where the walk through one channel-day has got to, and what it has placed so far.

    Kept slots update these as the walk passes them, so nothing is seeded from what is kept:
    its last entry may be a locked slot late in the day. What precedes the day's start is the
    tail of yesterday's overnight replay."""
    channel: dict[str, Any]
    day_str: str
    rng: random.Random
    t: int
    all_slots: list[Slot]                                  # kept and new, in the order they air
    last_show_id: int | None
    ads_per_break: int
    break_cap: int
    new_slots: list[Slot] = field(default_factory=list)
    prev: Slot | None = None
    last_programme_year: int | None = None
    placed_today: dict[int, int] = field(default_factory=dict)

    def emit(self, slot: Slot) -> None:
        self.new_slots.append(slot)
        self.all_slots.append(slot)
        self.prev = slot

    def break_state(self) -> tuple[int, int]:
        """Seconds and number of adverts in the consecutive break currently under way.

        The pattern's own breaks, the tidy-up to a round start time and the padding of a gap
        all draw on the same limits, so they cannot silently turn two configured adverts into a
        much longer break."""
        length = count = 0
        for s in reversed(self.all_slots):
            if s.kind != "advert":
                break
            length += s.duration
            count += 1
        return length, count

    def break_room(self) -> int:
        return max(0, self.break_cap - self.break_state()[0])

    def advert_count_room(self) -> int:
        return max(0, self.ads_per_break - self.break_state()[1])


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
        # (channel, band) -> minutes of holding card on each day built, reported as one note
        self.band_short: dict[tuple[str, str], list[int]] = {}
        self.channels = enabled_channels(conn)
        # Minute of day the broadcast day starts (08:00 = 480); minutes before it belong to the
        # previous day and are counted past 1440 so comparisons stay monotonic.
        self.day_start_min = self.policy.day_start_minutes
        self.library = Library(conn, self.policy, now=self.now, rebuild=rebuild,
                               exclude_media_ids=exclude_media_ids, only_media_ids=only_media_ids,
                               allow_external=allow_external)
        self.select = Selector(self.policy, self.settings, self.tz, self.library)
        self.runs = Runs(self.policy, self.library)
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
            # A holding card is not content already in progress: its remaining time is rebuilt,
            # and save() truncates the persisted slot at the cut so the past stays truthful. The
            # part before the cut is kept here at that length, or the walk would take the hole
            # it leaves for time to fill and place programmes in the past, over the card.
            return [replace(s, end_ts=cut) if s.kind == "filler" and not s.locked and s.end_ts > cut else s
                    for s in existing if s.locked or s.start_ts < cut]
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
        return filler_slot(channel["id"], day_str, start, end, title, **extra)

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
        w = Walk(channel=channel, day_str=day_str, rng=rng, t=day_start, all_slots=list(keep),
                 last_show_id=self._adjacent_show(channel["id"], day_start, before=True),
                 ads_per_break=int(channel.get("ads_per_break") or 2), break_cap=self.policy.advert_break_seconds)
        for s in keep:
            if s.show_id:
                w.placed_today[s.show_id] = w.placed_today.get(s.show_id, 0) + 1

        fixed: list[tuple[int, int, Any]] = [(s.start_ts, s.end_ts, s) for s in keep]
        for ts, show in self._anchors_for(channel, day, day_start, day_end):
            if w.placed_today.get(show.id):
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
            w.placed_today[show.id] = w.placed_today.get(show.id, 0) + 1
        day_bands = bands.timetable(self.library.bands.get(channel["id"], []), day, day_start, day_end,
                                    next_day_start, self.day_start_min, self.tz)
        filler = self._band_filler(channel, day, [b for _, _, b in day_bands], rng) if day_bands else None
        # A band holds its configured stretch except where something already stands in it: what
        # aired before a rebuild's cut, a locked slot, an anchored programme. It carries on
        # around those rather than being dropped for the day, and does not show again what it
        # showed before the cut.
        taken = sorted((fs, fe) for fs, fe, _ in fixed)
        band_shown: dict[tuple[int, int], set[int]] = {}
        for ts, end, band in day_bands:
            band_shown[(band.id, ts)] = {s.media_id for s in keep if s.block == band.name and s.media_id
                                               and ts <= s.start_ts < end}
            for seg_start, seg_end in self._segments(ts, end, taken):
                fixed.append((seg_start, seg_end, ("band", band, ts)))
        fixed.sort(key=lambda x: x[0])

        # An empty pattern means the channel places no programmes of its own: its day is its
        # bands, and anything they leave is its own free time.
        pattern_text = (channel.get("pattern") or "").strip()
        pattern = parse_pattern(pattern_text) if pattern_text else []
        ads_on = bool(channel.get("ads_enabled"))
        if not ads_on and pattern:
            pattern = [tok for tok in pattern if tok not in ("ad", "break")] or ["show"]
        rounding = self.policy.start_rounding_seconds
        tol = self.policy.duration_tolerance_seconds
        pat_idx = 0
        steps = 0
        fixed_queue = deque(fixed)

        # A band or programme that began before closedown finishes; the walk stops looking for
        # anything new at closedown.
        while w.t < day_end and steps < MAX_STEPS_PER_DAY:
            steps += 1
            # A fixed item starting now (or that we have run into)?
            if fixed_queue and w.t >= fixed_queue[0][0] - 60:
                fs, fe, payload = fixed_queue.popleft()
                if w.t < fs:
                    self._fill_to(w, fs)
                if isinstance(payload, Slot):
                    w.t = max(w.t, fe)
                    w.prev = payload
                    if payload.kind == "programme":
                        w.last_show_id = payload.show_id
                        w.last_programme_year = payload.year
                    elif payload.kind == "advert" and payload.media_id:
                        self.library.ad_last[(channel["id"], payload.media_id)] = payload.start_ts
                    continue
                if payload[0] == "band":
                    # A band that runs to closedown, or past it, may let its final item finish;
                    # the overnight starts when the band does end. A band that ends early gives
                    # its time back to the channel; the next band starts when the timetable says.
                    w.t = self._fill_band(channel, day_str, payload[1], max(w.t, fs), fe, filler, w.emit,
                                          hard_end=next_day_start if not fixed_queue else None,
                                          overrun=self._overrun(fixed_queue, tol),
                                          shown=band_shown[(payload[1].id, payload[2])])
                    continue
                _, show, ep = payload
                start = max(w.t, fs)
                slot = self._programme_slot(channel, day_str, start, ep, show)
                w.emit(slot)
                show.advance(start)
                self.library.show_last_placed[show.id] = start
                w.last_programme_year = ep.get("year")
                w.last_show_id = show.id
                w.t = slot.end_ts
                pat_idx += 1  # the anchor stands in for a 'show' token
                continue
            boundary = fixed_queue[0][0] if fixed_queue else day_end
            gap = boundary - w.t
            if gap <= 0:
                w.t = boundary
                continue
            if not pattern:
                # A bands-only channel: what the bands leave is still its own airtime, so it is
                # filled from the same material with no band name, and the guide lists each item
                # by its own title rather than under a band it does not belong to.
                w.t = self._fill_free(channel, day_str, w.t, boundary, filler, day_bands, w.emit,
                                      overrun=self._overrun(fixed_queue, tol))
                if w.t < boundary:
                    self._fill_to(w, boundary)
                continue
            token = pattern[pat_idx % len(pattern)]
            pat_idx += 1

            if token in ("ad", "break"):
                requested = w.ads_per_break if token == "break" else 1
                for _ in range(min(requested, w.advert_count_room())):
                    room = min(boundary - w.t, w.break_room())
                    ad = self.select.advert(channel, rng, w.t, room, w.last_programme_year) if room > 0 else None
                    if ad is None:
                        break
                    slot = self._media_slot(channel, day_str, w.t, ad, "advert")
                    w.emit(slot)
                    self.library.ad_last[(channel["id"], ad["id"])] = w.t
                    w.t = slot.end_ts
                continue
            if token == "ident":
                ident = self.select.ident(channel, rng, boundary - w.t) or self.select.stand_in_ident(channel, boundary - w.t)
                if ident is not None:
                    slot = self._media_slot(channel, day_str, w.t, ident, "ident")
                    w.emit(slot)
                    w.t = slot.end_ts
                continue

            if rounding and ads_on:
                # Tidy start time: pad with adverts up to the next rounding boundary, as far as
                # the break allowance goes; a programme may start off the five minutes rather
                # than sit behind a longer break.
                target = -(-w.t // rounding) * rounding
                room = w.break_room()
                if 0 < target - w.t <= w.break_cap and target < boundary and room > 0:
                    w.t = self._pad(channel, rng, day_str, w.t, min(target, w.t + room), w.last_programme_year,
                                    w.emit, advert_room=room, advert_limit=w.advert_count_room())
                    gap = boundary - w.t
            slack = 0
            if not fixed_queue:
                # Closedown is when the final programme may start, not when it is cut off. It may
                # finish before tomorrow's broadcast day; overnight replay simply starts later.
                slack = max(tol, next_day_start - day_end)

            next_fixed = fixed_queue[0][2] if fixed_queue else None
            next_show_id = (next_fixed.show_id if isinstance(next_fixed, Slot)
                            else next_fixed[1].id if next_fixed else None)
            barred = {x for x in (w.last_show_id, next_show_id) if x}
            attempts = [(token, 0)] + ([("show", 0)] if token != "show" else []) + [("show", 1), ("show", 2)]
            choice = None
            for tok, relax in attempts:
                choice = self.select.programme(channel, rng, w.t, gap, tok, w.placed_today, w.prev,
                                               barred, relax=relax, slack=slack)
                if choice is not None:
                    break
            if choice is None:
                # Eligibility changes later in the day: certificate watersheds and daypart
                # weights can make a sparse channel viable even though nothing fits now.  Do
                # not turn the first miss into one holding card through closedown; retry at the
                # next daypart boundary.
                start_min = self._bday_minutes(w.t)
                dayparts = dayparts_for_weekday(
                    datetime.fromtimestamp(w.t, self.tz).weekday(), self.settings,
                    self.select.channel_json(channel, "daypart_profile"))
                retry_min = daypart_end_minutes(start_min, dayparts, 1440)
                retry = min(boundary, w.t + max(60, retry_min - start_min) * 60)
                self._fill_to(w, retry, note=True)
                continue
            item, show = choice
            if item["id"] < 0:   # external line-up entry: placeholder slot plus a wanted request
                slot = self.runs.external_slot(channel["id"], day_str, w.t, item)
                w.emit(slot)
                w.placed_today[item["id"]] = w.placed_today.get(item["id"], 0) + 1
                if not item.get("_external_repeat"):
                    self.library.external_last_placed[item["lineup_id"]] = w.t
                    self.library.external_per_day[day_str] = self.library.external_per_day.get(day_str, 0) + 1
                w.last_show_id = item["id"] if item["kind"] == "episode" else None
                w.last_programme_year = item.get("year")
                w.t = slot.end_ts
                if item["kind"] == "episode":
                    more, w.t = self.runs.external_run(channel, day_str, item, slot, gap - slot.duration,
                                                       repeating=bool(item.get("_external_repeat")))
                    for extra in more:
                        w.emit(extra)
                continue
            slot = self._programme_slot(channel, day_str, w.t, item, show)
            w.emit(slot)
            if show is not None:
                show.advance(w.t)
                self.library.note_show_placed(channel["id"], show, w.t)
                w.placed_today[show.id] = w.placed_today.get(show.id, 0) + 1
                w.last_show_id = show.id
            else:
                w.last_show_id = None
                self.library.movie_placements.setdefault(item["id"], []).append(w.t)
            self.library.last_placed[item["id"]] = w.t
            w.last_programme_year = item.get("year")
            w.t = slot.end_ts
            if show is not None:
                more, w.t = self.runs.series_run(channel, day_str, show, slot, gap - slot.duration)
                for extra in more:
                    w.emit(extra)

        if w.t < day_end:
            self.log.append(f"{channel['name']} {day_str}: gave up after {MAX_STEPS_PER_DAY} steps at"
                            f" {self._hhmm(w.t)}; check the channel pattern")
            for fs, fe, payload in fixed_queue:
                if isinstance(payload, Slot):   # kept slots stay; only the gaps become filler
                    if w.t < fs:
                        w.emit(self._filler(channel, day_str, w.t, fs))
                    w.t = max(w.t, fe)
            if w.t < day_end:
                w.emit(self._filler(channel, day_str, w.t, day_end))
        if not pattern and filler is not None:
            # A channel of bands carries on through the night from its own material, with the
            # repeat gaps holding, rather than replaying a thin day.
            return w.new_slots + self._overnight_from_pool(channel, day, day_end, next_day_start, w.all_slots,
                                                           filler, day_bands)
        return w.new_slots + self._overnight(channel, day, day_end, next_day_start, w.all_slots)

    def _fill_to(self, w: Walk, target: int, note: bool = False) -> None:
        """Close the gap up to `target` with adverts or idents, then a caption, so the
        channel-day stays contiguous (the guide and the player both rely on that)."""
        w.t = self._pad(w.channel, w.rng, w.day_str, w.t, target, w.last_programme_year, w.emit,
                        advert_room=w.break_room(), advert_limit=w.advert_count_room())
        if w.t < target:
            if note and target - w.t > 120:
                self.log.append(f"{w.channel['name']} {w.day_str}: filler {(target - w.t) // 60} min at {self._hhmm(w.t)}")
            w.emit(self._filler(w.channel, w.day_str, w.t, target))
            w.t = target

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
                            item_repeat=item_repeat, feature_repeat=feature_repeat,
                            fit_seconds=self.policy.integer("band_fit_minutes") * 60,
                            feature_overrun=self.policy.integer("band_feature_overrun_minutes") * 60)

    @staticmethod
    def _segments(start: int, end: int, taken: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """The parts of start..end not covered by `taken` (sorted), each at least a minute."""
        out, t = [], start
        for fs, fe in taken:
            if fe <= t or fs >= end:
                continue
            if fs - t >= 60:
                out.append((t, fs))
            t = max(t, fe)
        if end - t >= 60:
            out.append((t, end))
        return out

    @staticmethod
    def _overrun(fixed_queue: deque[tuple[int, int, Any]], tolerance: int) -> int:
        """How far the last item before the next fixed thing may run over: the duration
        tolerance when a band follows (it starts when the item ends), nothing before a kept
        slot or an anchored programme, whose times are not the walk's to move."""
        following = fixed_queue[0][2] if fixed_queue else None
        return tolerance if isinstance(following, tuple) and following[0] == "band" else 0

    def notes(self) -> list[str]:
        """What the build has to say: its log, and one line for each band short of material
        however many days it was short on, so a week of thin bands is ten lines and not seventy."""
        short = [f"{channel}: {band} is short of material on {len(minutes)} day(s), "
                 f"{min(minutes)} to {max(minutes)} min of holding card" if len(minutes) > 1 else
                 f"{channel}: {band} is {minutes[0]} min short of material"
                 for (channel, band), minutes in self.band_short.items()]
        return self.log + short

    def _fill_band(self, channel: dict[str, Any], day_str: str, band: bands.Band, start: int, end: int,
                   filler: bands.Filler | None, emit: Callable[[Slot], None],
                   hard_end: int | None = None, overrun: int = 0, shown: set[int] | None = None) -> int:
        """Emit one band over the whole stretch it is configured for, under its name (the guide
        shows it as one programme), and return where it ended.

        The configuration is the authority: a band set to run ninety minutes occupies ninety
        minutes. What the library has for it plays (see `bands.fill_band`), and whatever is left
        of the stretch is the band's own holding card saying when service resumes, never other
        material under other names. The shortfall is what asks pitv_content for more, and each
        import rebuilds from the card, so it shrinks as material arrives."""
        placed, t = (bands.fill_band(band, start, end, filler, hard_end, overrun, shown)
                     if filler is not None else ([], start))
        for at, item in placed:
            emit(self._programme_slot(channel, day_str, at, item, None, block=band.name))
            if shown is not None:
                shown.add(item["id"])
        if end - t >= 60:
            resumes = f"Service resumes at {datetime.fromtimestamp(end, self.tz).strftime('%H:%M')}"
            # A concert that ends before its band does is not a shortfall, only an interval.
            short = not (band.feature and placed)
            emit(self._filler(channel, day_str, t, end, title=band.name, block=band.name,
                              subtitle=f"{self.settings.get('band_card_message') or ''} {resumes}".strip()
                              if short else resumes))
            if short:
                self.band_short.setdefault((channel["name"], band.name), []).append((end - t) // 60)
            return end
        return max(t, end) if placed else end

    def _fill_free(self, channel: dict[str, Any], day_str: str, start: int, end: int,
                   filler: bands.Filler | None, day_bands: list[tuple[int, int, bands.Band]],
                   emit: Callable[[Slot], None], overrun: int = 0) -> int:
        """Emit the channel's own items over time its bands leave; see `bands.fill_free`."""
        if filler is None or start >= end:
            return start
        kinds = tuple(sorted({k for _, _, b in day_bands for k in b.kinds}))
        placed, t = bands.fill_free(channel["id"], kinds, self.select.channel_decades(channel), start, end,
                                    filler, overrun)
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
        return programme_slot(channel["id"], day_str, start, item, show, block)

    def _media_slot(self, channel: dict[str, Any], day_str: str, start: int, item: dict[str, Any], kind: str) -> Slot:
        return media_slot(channel["id"], day_str, start, item, kind)

    def _next_day_slots(self, channel_id: int, next_day_start: int) -> list[Slot]:
        """Tomorrow's own slots, if tomorrow has been built: the overnight's last resort."""
        row = self.conn.execute("SELECT day FROM schedule WHERE channel_id = ? AND start_ts >= ? AND replay = 0"
                                " AND kind = 'programme' ORDER BY start_ts LIMIT 1",
                                (channel_id, next_day_start)).fetchone()
        return self._existing_slots(channel_id, row["day"]) if row else []

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
