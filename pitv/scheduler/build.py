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
    DEFAULT_SETTINGS,
    all_settings,
    enabled_channels,
    now_ts,
    tx,
)
from ..lineup import nas_only_for
from . import bands
from .library import Library, Rebuild
from .policy import SchedulerPolicy
from .rules import (
    EraSpans,
    allowed_at,
    day_bounds,
    daypart_end_minutes,
    daypart_for,
    dayparts_for_weekday,
    era_spans,
    era_weight_spans,
    hhmm_to_minutes,
    in_decades,
    local_ts,
    minutes_of_day,
    parse_pattern,
    tz_of,
)
from .slots import Show, Slot, json_field, seconds, slot_titles

Progress = Callable[[str], None] | None
# Upper bound on walk steps for one channel-day. Only a pattern that can never place a
# programme (adverts or idents alone) gets near it; the rest of such a day becomes filler.
MAX_STEPS_PER_DAY = 3000
FREE_SHORT_ITEMS = 24          # fewer short items than this and a channel leans on its long ones
FREE_FEATURE_SECONDS = 30 * 60  # ... in any free stretch at least this long
FILLER_TITLE = "Programmes will continue shortly"
STAND_IN_IDENT_SECONDS = 10     # the shipped test signal's length (pitv/assets)
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
        # Parsed once: the candidate loops run tens of thousands of times per build.
        self._global_spans = era_spans(self.policy.value("era_weights"))
        self._advert_spans = era_spans(self.policy.value("advert_era_weights")
                                       or DEFAULT_SETTINGS["advert_era_weights"])
        self._advert_window = self.policy.integer("advert_year_window")
        self._advert_penalty = self.policy.advert_repeat_seconds
        self._advert_pools: dict[int, list[tuple[dict[str, Any], float, float]]] = {}
        self._channel_cols: dict[tuple[int, str], Any] = {}
        self._decades: dict[int, tuple[int, ...]] = {}
        self.library = Library(conn, self.policy, now=self.now, rebuild=rebuild,
                               exclude_media_ids=exclude_media_ids, only_media_ids=only_media_ids,
                               allow_external=allow_external)
        self.ad_last: dict[tuple[int, int], int] = {}  # (channel, media) -> ts
        # (channel, day) -> time from which save() replaces unlocked slots; None adds only.
        self._cuts: dict[tuple[int, str], int | None] = {}
        # (lineup_id, episode) -> wanted id, so every placement of one request in this build
        # shares a row (a film's later airings, a placeholder's overnight replay).
        self._requests: dict[tuple[int, int | None], int] = {}
        # A short series is scheduled as a bundle. Cadence repeats replay that bundle instead
        # of collapsing back to one five-minute episode followed by an advert break.
        self._short_runs: dict[int, list[dict[str, Any]]] = {}
        self._external_short_runs: dict[int, list[dict[str, Any]]] = {}

    def _bday(self, minute: int) -> int:
        """A local minute of day on the broadcast day's clock: +1440 after midnight."""
        return minute if minute >= self.day_start_min else minute + 1440

    def _bday_minutes(self, ts: int) -> int:
        return self._bday(minutes_of_day(ts, self.tz))

    # --- helpers -----------------------------------------------------------------------

    def _rng(self, channel_id: int, day: date) -> random.Random:
        key = f"{self.seed}:{channel_id}:{day.isoformat()}".encode()
        return random.Random(int.from_bytes(hashlib.sha256(key).digest()[:8], "big"))

    def _channel_json(self, channel: dict[str, Any], key: str) -> Any:
        """A channel's JSON column, parsed once per build rather than once per candidate."""
        ck = (channel["id"], key)
        if ck not in self._channel_cols:
            self._channel_cols[ck] = json_field(channel.get(key))
        return self._channel_cols[ck]

    def _channel_setting(self, channel: dict[str, Any], key: str) -> Any:
        """A channel's own era/kind weights, falling back to the global setting."""
        return self._channel_json(channel, key) or self.policy.value(key)

    def _channel_spans(self, channel: dict[str, Any]) -> EraSpans:
        own = self._channel_json(channel, "era_weights")
        if not own:
            return self._global_spans
        ck = (channel["id"], "era_spans")
        if ck not in self._channel_cols:
            self._channel_cols[ck] = era_spans(own)
        return self._channel_cols[ck]

    def _genre_weight(self, channel: dict[str, Any], genres: list[str]) -> float:
        gw = self._channel_json(channel, "genre_weights")
        if not gw or not genres:
            return 1.0
        return max(float(gw.get(g, gw.get(g.lower(), 1.0))) for g in genres)

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

    # --- choosing ----------------------------------------------------------------------

    def _choose_programme(self, channel: dict[str, Any], rng: random.Random, t: int, gap: int,
                          token: str, placed_today: dict[int, int], prev: Slot | None,
                          barred: set[int], relax: int = 0,
                          slack: int = 0) -> tuple[dict[str, Any], Show | None] | None:
        """Pick a programme for a gap. Selection is two-stage so the TV/movie balance follows the
        channel's kind weights rather than the size of each pool: choose the kind, then the item.

        relax=0 normal rules; relax=1 ignore daypart preferences;
        relax=2 additionally allow movies that aired recently. Certificates are never relaxed.
        `barred` holds the series either side of the gap (never two episodes back to back).
        `slack` is how far past the gap a programme may run: the duration tolerance at closedown,
        zero when a fixed slot follows (it must not be overlapped)."""
        start_min = minutes_of_day(t, self.tz)
        bday_min = self._bday_minutes(t)
        weekday_n = datetime.fromtimestamp(t, self.tz).weekday()
        dayparts = dayparts_for_weekday(weekday_n, self.settings, self._channel_json(channel, "daypart_profile"))
        dp = daypart_for(bday_min, dayparts)
        sport_block = float(dp.get("sport", 1.0)) >= 3.0   # docs/PLAN.md section 4.6: 3 and above forms a block
        dp_end = daypart_end_minutes(bday_min, dayparts, 1440)
        spans = self._channel_spans(channel)
        kind_weights = self._channel_setting(channel, "kind_weights")
        movie_repeat = self.policy.movie_repeat_seconds
        genre_penalty = self.policy.number("genre_repeat_penalty")
        # A channel may take only what the index has labelled: no genre or no year, no airing.
        strict_matching = bool(channel.get("strict_matching"))
        daily_limit = self.policy.integer("show_daily_limit")
        repeat_penalty = self.policy.number("show_repeat_penalty")
        max_minutes = dp.get("max_minutes")
        weekend = weekday_n >= 5
        relaxed = relax >= 1
        kids_rule = not channel.get("kids_any_time")
        kids_breakfast = weekend and self.policy.enabled("weekend_kids_breakfast") and dp.get("name") == "Breakfast"
        unknown_w = self.policy.number("unknown_year_weight")
        prev_genres = ({g.lower() for g in prev.genres}
                       if prev is not None and prev.kind == "programme" and prev.genres else set())

        decades = self._channel_decades(channel)

        def common_weight(item: dict[str, Any], kind: str, end_year: int | None = None) -> float:
            # A pinned external line-up entry was put on this exact channel by the user. Its
            # year must not be rejected by the automatic era-routing defaults; those defaults
            # decide where unassigned library material belongs.
            explicit = bool(item.get("lineup_pinned") or (item.get("lineup_id") and item.get("pinned")))
            if strict_matching and not explicit and not (item.get("genres") and item.get("year")):
                return 0.0     # this channel takes only what the index has labelled
            if not explicit and not in_decades(item.get("year"), decades, end_year, unknown_ok=not strict_matching):
                return 0.0
            w = era_weight_spans(item.get("year"), spans, end_year, unknown_w)
            if explicit and w <= 0:
                w = max(unknown_w, 0.2)
            if w <= 0:
                return 0.0
            duration = float(item["duration"])
            if duration > gap + slack:
                return 0.0
            short_threshold, short_target = self.policy.short_episode_seconds(channel)
            if kind == "tv" and short_threshold and duration < short_threshold and short_target > gap + slack:
                return 0.0     # never strand one short episode before a fixed boundary
            if not allowed_at(item, start_min, self.settings, kids_rule=kids_rule):
                return 0.0
            if not relaxed:
                w *= float(dp.get(kind, 1.0))
            if item.get("kids"):
                kw = float(dp.get("kids", 1.0))
                if kids_breakfast:
                    kw = max(kw, 4.0)
                w *= kw if not relaxed else max(kw, 0.2)
            w *= self._genre_weight(channel, item.get("genres") or [])
            is_sport = (item.get("category") == "sport")
            if is_sport:
                sport_w = float(dp.get("sport", 1.0))
                # Outside sport-friendly dayparts sport is admitted only at the final relaxation
                # step, after recently aired films: it is a last resort, not a filler.
                if sport_w < 0.5 and relax < 2:
                    return 0.0
                w *= sport_w if not relaxed else sport_w * 0.5
            w *= self.library.pool_factor(kind, item.get("year"), end_year)
            if max_minutes and duration > max_minutes * 60 and not relaxed and not (is_sport and sport_block):
                w *= 0.15
            # Running well past the end of the daypart changes the feel of the next one: a sport
            # block must not swallow the evening, and a long film should not start at teatime.
            overrun_min = (bday_min + duration / 60) - dp_end
            if overrun_min > 30 and not relaxed:
                w *= 0.02 if is_sport else (0.5 if kind == "movie" and overrun_min < 75 else 0.25)
            # Sharing a genre with the previous programme is penalised, except within a sport
            # block, which is meant to run together.
            if (prev_genres and item.get("genres") and not (is_sport and sport_block)
                    and prev_genres & {g.lower() for g in item["genres"]}):
                w *= genre_penalty
            if gap - duration < 600:
                w *= 1.5  # fills the gap neatly
            return w

        tv_cands: list[tuple[float, dict[str, Any], Show | None]] = []
        movie_cands: list[tuple[float, dict[str, Any], Show | None]] = []

        if token in ("show", "tv"):
            for show in self.library.free_shows.get(channel["id"], ()):
                if show.id in barred:
                    sport_ok = (show.category == "sport" and weekend
                                and self.policy.enabled("sport_back_to_back_weekends"))
                    if not sport_ok:
                        continue  # never two episodes of the same series back to back
                resting = bool(show.resting_until and t < show.resting_until)
                if resting and relax < 2:
                    continue
                times_today = placed_today.get(show.id, 0)
                # Relaxing daypart preferences must never turn a thin channel into the same
                # episode all day.  The daily series cap is a variety rule, not a preference.
                if times_today >= daily_limit:
                    continue
                last_new = self.library.show_last_placed.get(show.id)
                repeating = show.mode == "auto" and not self.policy.next_episode_due(last_new, t)
                prior_run = self._short_runs.get(show.id)
                ep = (prior_run[0] if repeating and prior_run else
                      show.previous_episode() if repeating else show.next_episode())
                if ep is None:
                    continue
                if repeating:
                    ep = {**ep, "_series_repeat": True}
                w = common_weight(ep, "tv", show.end_year)
                if w <= 0:
                    continue
                if times_today:
                    w *= repeat_penalty ** times_today
                if resting:
                    w *= 0.3
                if repeating:
                    w *= 0.12       # useful fallback, but fresh titles win decisively
                elif show.mode == "auto":
                    w *= self.policy.cadence_factor(self.library.show_last_placed.get(show.id), t, relaxed=relaxed)
                tv_cands.append((w, ep, show))
        if token in ("show", "movie"):
            for m in self.library.movies_on.get(channel["id"], ()):
                placements = self.library.movie_placements.get(m["id"], [])
                # Distance to the nearest airing in either direction: a film already placed later
                # today on another channel is just as "recent" as one shown yesterday.
                nearest = min((abs(t - x) for x in placements), default=None)
                recent = nearest is not None and nearest < movie_repeat
                if recent and (relax < 2 or nearest < 12 * 3600):
                    continue
                w = common_weight(m, "movie")
                if w <= 0:
                    continue
                if recent:
                    # Forced repeat (thin library): strongly prefer the one aired longest ago.
                    w *= 0.2 * (nearest / movie_repeat) ** 2
                elif nearest is not None:
                    w *= min(2.0, nearest / movie_repeat)  # prefer the least recently aired
                else:
                    w *= 2.0
                movie_cands.append((w, m, None))

        externals = self.library.externals_on.get(channel["id"])
        if externals and not nas_only_for(channel, self.settings):
            # Once there is enough preparation time, remote titles compete on programme variety,
            # rather than being permanently disadvantaged merely because they are not on the NAS.
            ext_w = self.policy.external_weight(t)
            for e in externals:
                times_today = placed_today.get(e["id"], 0)
                emergency_repeat = relax >= 2 and bool(e.get("last_spec"))
                if ((e["id"] in barred or (e.get("show_id") and e["show_id"] in barred))
                        and not emergency_repeat):
                    continue
                episode = e["kind"] == "episode"
                if token not in ("show", "tv" if episode else "movie"):
                    continue
                item_limit = daily_limit if episode else 1
                if times_today >= item_limit and not emergency_repeat:
                    continue
                due = not episode or self.policy.next_episode_due(self.library.external_last_placed.get(e["lineup_id"]), t)
                candidate = e
                if episode and (not due or times_today >= item_limit):
                    if not e.get("last_spec"):
                        continue
                    # Re-air the episode already requested rather than advancing the external
                    # series before its weekly next-episode slot.  The shared wanted row means
                    # one download serves both airings.
                    candidate = {**e, "_external_repeat": True}
                    prior_run = self._external_short_runs.get(e["lineup_id"])
                    if prior_run:
                        candidate["_repeat_spec"] = prior_run[0]
                w = common_weight(e, "tv" if episode else "movie") * ext_w
                if w <= 0:
                    continue
                if episode:
                    w *= (0.18 if not due else
                          self.policy.cadence_factor(self.library.external_last_placed.get(e["lineup_id"]), t, relaxed=relaxed))
                (tv_cands if episode else movie_cands).append((w, candidate, None))

            # A configured remote title is part of the channel's catalogue, not an occasional
            # lottery ticket.  Once the preparation window has passed, give every as-yet unseen
            # remote title one turn before falling back to already represented local material.
            # This is deliberately per kind so the channel's TV/film balance still applies.
            unseen_tv = [c for c in tv_cands
                         if c[1]["id"] < 0 and c[1]["lineup_id"] not in self.library.external_last_placed]
            unseen_movies = [c for c in movie_cands
                             if c[1]["id"] < 0 and c[1]["lineup_id"] not in self.library.external_last_placed]
            if unseen_tv and self.policy.external_prepared(t):
                tv_cands = unseen_tv
            if unseen_movies and self.policy.external_prepared(t):
                movie_cands = unseen_movies

        kinds: list[tuple[float, list[tuple[float, dict[str, Any], Show | None]]]] = []
        for kind, cands in (("tv", tv_cands), ("movie", movie_cands)):
            kind_weight = float(kind_weights.get(kind, 1.0))
            if not cands or kind_weight <= 0:
                continue  # zero is an explicit prohibition, not a very small preference
            weight = kind_weight * (1.0 if relaxed else float(dp.get(kind, 1.0)))
            if weight > 0:
                kinds.append((weight, cands))
        if not kinds:
            return None
        pool = rng.choices(kinds, weights=[k[0] for k in kinds], k=1)[0][1]
        pick = rng.choices(pool, weights=[c[0] for c in pool], k=1)[0]
        return pick[1], pick[2]

    def _advert_pool(self, channel: dict[str, Any]) -> list[tuple[dict[str, Any], float, float]]:
        """(advert, duration, era weight) for every advert the channel may carry, in library
        order. Built once per channel: `_choose_advert` runs for every break of every day."""
        pool = self._advert_pools.get(channel["id"])
        if pool is None:
            family_only = bool(channel.get("family_safe_ads"))
            pool = []
            for ad in self.library.adverts:
                if family_only and not ad.get("family_safe", 1):
                    continue  # no alcohol, tobacco or adult adverts on a family channel
                w = era_weight_spans(ad.get("year"), self._advert_spans)
                if w > 0:     # adverts must be from the configured decades
                    pool.append((ad, float(ad["duration"]), w))
            self._advert_pools[channel["id"]] = pool
        return pool

    def _choose_advert(self, channel: dict[str, Any], rng: random.Random, t: int, gap: int,
                       near_year: int | None) -> dict[str, Any] | None:
        cands: list[tuple[float, dict[str, Any]]] = []
        fresh: list[tuple[float, dict[str, Any]]] = []
        for ad, duration, w in self._advert_pool(channel):
            if duration > gap:
                continue
            year = ad.get("year")
            if near_year and year is not None and abs(year - near_year) <= self._advert_window:
                w *= 3.0
            last = self.ad_last.get((channel["id"], ad["id"]))
            if last is not None and t - last < self._advert_penalty:
                w *= 0.1
            cands.append((w, ad))
            if last is None or t - last > 900:
                fresh.append((w, ad))  # not shown in the last quarter of an hour
        pool = fresh or cands  # never repeat an advert within a break if any other will fit
        if not pool:
            return None
        return rng.choices(pool, weights=[c[0] for c in pool], k=1)[0][1]

    def _choose_ident(self, channel: dict[str, Any], rng: random.Random, gap: int) -> dict[str, Any] | None:
        if not channel.get("idents_enabled", 1):
            return None
        fits = [i for i in self.library.idents if float(i["duration"]) <= gap]
        # The channel's own idents, else generic ones; never another channel's, which name it.
        pool = [i for i in fits if i.get("home_channel_id") == channel["id"]] or \
            [i for i in fits if not i.get("home_channel_id")]
        return rng.choice(pool) if pool else None

    def _stand_in_ident(self, channel: dict[str, Any], gap: int) -> dict[str, Any] | None:
        """For a channel whose pattern asks for idents but which has none, of its own or generic:
        a slot with no file, which the player fills with the test signal under the channel's
        badge. Only where the pattern asks; gaps are never padded with it."""
        if not channel.get("idents_enabled", 1) or gap < STAND_IN_IDENT_SECONDS:
            return None
        if any(i.get("home_channel_id") in (None, channel["id"]) for i in self.library.idents):
            return None   # it has idents; none fitted this gap
        return {"id": None, "title": channel.get("short_name") or channel["name"], "duration": STAND_IN_IDENT_SECONDS}

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
        day_bands = self._bands_for(channel, day, day_start, day_end, next_day_start)
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
                        self.ad_last[(channel["id"], payload.media_id)] = payload.start_ts
                    continue
                if payload[0] == "band":
                    # A band that runs to closedown, or past it, may let its final item finish;
                    # the overnight starts when the band does end.
                    last_band = not fixed_queue
                    band = payload[1]
                    t = self._fill_band(channel, day_str, band, max(t, fs), fe, filler, emit,
                                        hard_end=next_day_start if last_band else None)
                    # A feature band is introduced by one film/concert. If that finishes before
                    # the next band's advertised start, hand the spare time to that next band;
                    # unrelated free material here both breaks the guide block and defeats the
                    # next band's genre/year rules.
                    if band.feature and t < fe and fixed_queue:
                        nfs, nfe, next_payload = fixed_queue[0]
                        if (not isinstance(next_payload, Slot) and next_payload[0] == "band"
                                and nfs == fe):
                            fixed_queue.popleft()
                            t = self._fill_band(channel, day_str, next_payload[1], t, nfe, filler, emit,
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
                    ad = self._choose_advert(channel, rng, t, room, last_programme_year) if room > 0 else None
                    if ad is None:
                        break
                    slot = self._media_slot(channel, day_str, t, ad, "advert")
                    emit(slot)
                    self.ad_last[(channel["id"], ad["id"])] = t
                    t = slot.end_ts
                continue
            if token == "ident":
                ident = self._choose_ident(channel, rng, boundary - t) or self._stand_in_ident(channel, boundary - t)
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
                choice = self._choose_programme(channel, rng, t, gap, tok, placed_today, prev,
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
                    self._channel_json(channel, "daypart_profile"))
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
        # Overnight is a replay on every channel, including one whose daytime is entirely made
        # from bands. Rebuilding it from the band's combined media pool loses every genre/year
        # boundary and turns the guide into scores of unrelated, unlabelled short clips.
        overnight = self._overnight(
            channel, day, day_end, next_day_start, all_slots,
            labelled_only=bool(not pattern and day_bands),
        )
        return new_slots + overnight

    # --- bands ---------------------------------------------------------------------------

    def _channel_decades(self, channel: dict[str, Any]) -> tuple[int, ...]:
        """The decades a channel plays; empty means any. Held as JSON on the channel row."""
        cached = self._decades.get(channel["id"])
        if cached is None:
            raw = json_field(channel.get("decades")) or []
            cached = self._decades[channel["id"]] = tuple(int(d) for d in raw if isinstance(d, (int, float, str))
                                                          and str(d).isdigit())
        return cached

    def _bands_for(self, channel: dict[str, Any], day: date, day_start: int, day_end: int,
                   last_end: int | None = None) -> list[tuple[int, int, bands.Band]]:
        """This channel's bands for this day as (start, end, band). A band without a length runs
        to the next one, or to the end of the day.

        A band that starts before closedown keeps the length it was given: a two hour band at
        23:30 runs two hours, into the small hours, rather than being cut to thirty minutes
        because midnight arrived. What follows it is the overnight, which simply starts later."""
        todays = [b for b in self.library.bands.get(channel["id"], ()) if b.on(day.weekday())]
        out: list[tuple[int, int, bands.Band]] = []
        starts = [self._band_start(day, b.start) for b in todays]
        for i, band in enumerate(todays):
            start = starts[i]
            later = [s for s in starts[i + 1:] if s > start]
            end = start + band.minutes * 60 if band.minutes else (later[0] if later else day_end)
            if later:
                end = min(end, later[0])
            start, end = max(start, day_start), min(end, last_end or day_end)
            if start < day_end and end - start >= 60:
                out.append((start, end, band))
        return sorted(out, key=lambda x: x[0])

    def _band_start(self, day: date, hhmm: str) -> int:
        """A band's start time on this broadcast day. A time earlier than the day's own start
        belongs to the small hours at its end, so "00:30" on a day that opens at 08:00 is
        tomorrow morning, not twenty-four hours ago."""
        minute = hhmm_to_minutes(hhmm)
        at = day + timedelta(days=1) if minute < self.day_start_min else day
        return local_ts(at, hhmm, self.tz)

    def _band_filler(self, channel: dict[str, Any], day: date, todays: list[bands.Band],
                     rng: random.Random) -> bands.Filler:
        kinds = {k for b in todays for k in b.kinds}
        decades = self._channel_decades(channel)
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
        """Fill one band with items under its name; the guide shows them as one programme.

        A band that opens with a feature and finds none, or whose feature ends early, fills the
        rest with its own short items, as any band does: a "Concert" stretch with no concert to
        show is still a music stretch. It runs to its timetabled end; the next band starts when
        the timetable says, not when this one runs out."""
        t = start
        if filler is None:
            emit(self._filler(channel, day_str, t, end, title=band.name, block=band.name))
            return end
        want_feature = band.feature
        steps = 0
        while t < end and steps < MAX_STEPS_PER_DAY:
            steps += 1
            # A feature may run a little past its band rather than be dropped for being long.
            opening_feature = want_feature
            gap = ((hard_end - t) if hard_end is not None
                   else (end - t + 20 * 60) if opening_feature else (end - t))
            item = filler.pick(band, t, gap, feature=opening_feature)
            want_feature = False
            if item is None:
                # Nothing suitable remains. Keep the band intact with its own caption: handing
                # this time back to a bands-only channel would let unrelated/untagged clips leak
                # into a strict genre or decade band and would make the guide lose the band.
                emit(self._filler(channel, day_str, t, end, title=band.name, block=band.name))
                return end
            slot = self._programme_slot(channel, day_str, t, item, None, block=band.name)
            emit(slot)
            filler.note(item, t)
            t = slot.end_ts
            if opening_feature:
                # "Concert" is one concert, not a concert and then whatever fits: what is left of
                # the stretch is the channel's own time, and the guide names each item in it.
                return t
        return t

    def _fill_free(self, channel: dict[str, Any], day_str: str, start: int, end: int,
                   filler: bands.Filler | None, day_bands: list[tuple[int, int, bands.Band]],
                   emit: Callable[[Slot], None]) -> int:
        """Fill time between bands on a channel that has no pattern, with no band name on it.

        A band that ends early (a concert shorter than its stretch) or a gap the timetable leaves
        is ordinary airtime for that channel: the same kinds of item, held to the channel's own
        decades, chosen the same way, but belonging to no band."""
        if filler is None or start >= end:
            return start
        kinds = tuple(sorted({k for _, _, b in day_bands for k in b.kinds})) or ("music",)
        free = bands.Band(id=0, channel_id=channel["id"], name="", start="", minutes=None, days=(),
                          kinds=kinds, genres=(), decades=self._channel_decades(channel), feature=False)
        # A channel with only a handful of short items would play those same few over and over,
        # so where it also holds long ones (a share of concert films, say) those carry the time
        # and the short items fill around them. A well stocked channel never reaches for them.
        thin = filler.short_items(free) < FREE_SHORT_ITEMS
        t = start
        steps = 0
        while t < end and steps < MAX_STEPS_PER_DAY:
            steps += 1
            item = None
            if thin and end - t >= FREE_FEATURE_SECONDS:
                item = filler.pick(free, t, end - t, feature=True)
            if item is None:
                item = filler.pick(free, t, end - t, feature=False)
            if item is None:
                break
            slot = self._programme_slot(channel, day_str, t, item, None)
            emit(slot)
            filler.note(item, t)
            t = slot.end_ts
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
                item = self._choose_advert(channel, rng, t, min(gap, break_end - t), near_year)
            if item is None and idents < 2:
                item = self._choose_ident(channel, rng, gap)
                kind = "ident"
                idents += 1
            if item is None:
                break
            slot = self._media_slot(channel, day_str, t, item, kind)
            emit(slot)
            if kind == "advert":
                adverts += 1
                self.ad_last[(channel["id"], item["id"])] = t
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
        cached = self._external_short_runs.get(entry["lineup_id"]) if repeating else None
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
            self._external_short_runs[entry["lineup_id"]] = run
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
        cached = self._short_runs.get(show.id) if repeating else None
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
            self._short_runs[show.id] = run
        return t

    def _media_slot(self, channel: dict[str, Any], day_str: str, start: int, item: dict[str, Any], kind: str) -> Slot:
        """An advert or ident slot; the subtitle is just the year."""
        return Slot(channel_id=channel["id"], day=day_str, start_ts=start, end_ts=start + seconds(item),
                    media_id=item["id"], offset=0, kind=kind, title=item["title"],
                    subtitle=str(item.get("year") or ""), year=item.get("year"))

    def _overnight(self, channel: dict[str, Any], day: date, day_end: int, next_day_start: int,
                   day_slots: list[Slot], *, labelled_only: bool = False) -> list[Slot]:
        """00:00 to 08:00: replay the day from the channel's `overnight_replay_from`, looping a
        short day, without butting the same series against the day's end or tomorrow's start."""
        day_str = day.isoformat()
        replay_from = channel.get("overnight_replay_from") or self.policy.day_start
        from_day = day + timedelta(days=1) if hhmm_to_minutes(replay_from) < self.day_start_min else day
        from_ts = local_ts(from_day, replay_from, self.tz)
        ordered = sorted(day_slots, key=lambda s: s.start_ts)
        source = [s for s in ordered if s.start_ts >= from_ts and s.kind != "filler"
                  and (not labelled_only or bool(s.block))]
        if not source:
            # A day with nothing to replay is a day built before the library had anything in it.
            # Showing a caption until morning is worse than opening tomorrow early, so the
            # overnight takes the next day's programmes when they are already built.
            source = self._next_day_slots(channel["id"], next_day_start)
            if labelled_only:
                source = [s for s in source if s.block]
        # A break only makes sense attached to a programme.  Starting a replay part-way through
        # the source day must not begin with the adverts that preceded its first programme.
        first_programme = next((i for i, s in enumerate(source) if s.kind == "programme"), len(source))
        source = source[first_programme:]
        # The replay follows straight on from the day's last programme: never start it with
        # another episode of that same series.
        last_prog = next((s for s in reversed(ordered) if s.kind == "programme"), None)
        if last_prog is not None and last_prog.show_id is not None:
            first = next((i for i, s in enumerate(source)
                          if s.kind == "programme" and s.show_id != last_prog.show_id), len(source))
            source = source[first:]
        # If tomorrow is already built, the replay must not end with tomorrow's opening series.
        tomorrow_first = self._adjacent_show(channel["id"], next_day_start, before=False)
        out: list[Slot] = []
        t = max(day_end, max((s.end_ts for s in day_slots), default=day_end))
        queue = deque(source)
        loops = 0
        suppress_break = False
        while t < next_day_start:
            if not queue:
                # A short day (thin library) is replayed again until 08:00.
                loops += 1
                if not source or loops > 12:
                    break
                queue.extend(source)
            s = queue.popleft()
            if (s.kind == "programme" and tomorrow_first is not None and s.show_id == tomorrow_first
                    and t + s.duration >= next_day_start):
                # The adverts which followed this programme in the source belong to it too.
                # Suppress the whole unit, otherwise a thin channel can end with every advert
                # from the source day and no programme between them.
                suppress_break = True
                continue  # would run straight into the same series at 08:00
            if s.kind != "programme" and suppress_break:
                continue
            if s.kind == "programme":
                suppress_break = False
            end = min(t + s.duration, next_day_start)
            # A replayed placeholder keeps its request (wanted_id, or wanted_spec until save)
            # so the delivered file binds to the replay too.
            out.append(replace(s, day=day_str, start_ts=t, end_ts=end, replay=1, locked=0))
            t = end
        if t < next_day_start:
            out.append(self._filler(channel, day_str, t, next_day_start,
                                    title=f"Programmes will resume at {self.policy.day_start}",
                                    replay=1))
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
