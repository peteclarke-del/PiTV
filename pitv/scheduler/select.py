"""Choosing what fills a gap: a programme, an advert, an ident.

The Builder walks a channel-day and asks this for one thing at a time: a programme for a gap
of so many seconds at such a time, an advert for a break, an ident to bridge. Every weighting
rule lives here (eras, dayparts, certificates, genres, kids, sport, cadence, repeats, the
TV and film balance) and nothing about the walk does: the Selector never emits a slot.

Selection is weighted random over everything eligible, seeded per channel-day by the caller,
so building the same week again reproduces it."""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..db import DEFAULT_SETTINGS
from ..genres import canonical, canonical_all
from ..lineup import nas_only_for
from .library import Library
from .policy import SchedulerPolicy
from .rules import (
    EraSpans,
    allowed_at,
    broadcast_day_for,
    daypart_end_minutes,
    daypart_for,
    dayparts_for_weekday,
    era_spans,
    era_weight_spans,
    in_decades,
    minutes_of_day,
)
from .runs import next_episode_number
from .slots import Show, Slot, json_field

STAND_IN_IDENT_SECONDS = 10     # the shipped test signal's length (pitv/assets)


# A channel borrows a type from another's shelf only where its daypart at least doubles that
# type's weight: a children's daypart, not a teatime that merely tolerates children's programmes.
BORROW_AT = 2.0

class Selector:
    def __init__(self, policy: SchedulerPolicy, settings: dict[str, Any], tz: ZoneInfo, library: Library) -> None:
        self.policy = policy
        self.settings = settings
        self.tz = tz
        self.library = library
        self.day_start_min = policy.day_start_minutes
        # Parsed once: the candidate loops run tens of thousands of times per build.
        self._global_spans = era_spans(policy.value("era_weights"))
        self._advert_spans = era_spans(policy.value("advert_era_weights") or DEFAULT_SETTINGS["advert_era_weights"])
        self._advert_window = policy.integer("advert_year_window")
        self._advert_penalty = policy.advert_repeat_seconds
        self._advert_pools: dict[int, list[tuple[dict[str, Any], float, float]]] = {}
        self._channel_cols: dict[tuple[int, str], Any] = {}
        self._decades: dict[int, tuple[int, ...]] = {}

    def _bday_minutes(self, ts: int) -> int:
        """A local minute of day on the broadcast day's clock: +1440 after midnight."""
        minute = minutes_of_day(ts, self.tz)
        return minute if minute >= self.day_start_min else minute + 1440

    def channel_json(self, channel: dict[str, Any], key: str) -> Any:
        """A channel's JSON column, parsed once per build rather than once per candidate."""
        ck = (channel["id"], key)
        if ck not in self._channel_cols:
            self._channel_cols[ck] = json_field(channel.get(key))
        return self._channel_cols[ck]

    def _borrowed(self, channel: dict[str, Any], dp: dict[str, Any], relax: int) -> list[Show]:
        """Series from other channels' shelves that this channel may carry here and now. A type
        belongs to its themed channel; a channel that lists it under `also_carries` borrows it,
        but only in a daypart that asks for it by at least doubling its weight (children's
        television on a Saturday morning for cartoons, the sport dayparts for sport, a daypart
        weighted towards documentaries for those), and never as a way out when nothing else fits. The episode position is the
        series' own, so the home channel carries on from wherever the borrower left it."""
        if relax:
            return []
        out: list[Show] = []
        for ptype in self.channel_json(channel, "also_carries") or []:
            asks = (float(dp.get("kids", 1.0)) if ptype == "cartoon" else float(dp.get("sport", 1.0)) if ptype == "sport"
                    else self._daypart_genre_weight(dp, ["Documentary"]) if ptype == "documentary" else 0.0)
            if asks >= BORROW_AT:
                out += [s for s in self.library.shows_of_type.get(ptype, ()) if s.home_channel_id != channel["id"]]
        return out

    def _channel_setting(self, channel: dict[str, Any], key: str) -> Any:
        """A channel's own era/kind weights, falling back to the global setting."""
        return self.channel_json(channel, key) or self.policy.value(key)

    def _channel_spans(self, channel: dict[str, Any]) -> EraSpans:
        own = self.channel_json(channel, "era_weights")
        if not own:
            return self._global_spans
        ck = (channel["id"], "era_spans")
        if ck not in self._channel_cols:
            self._channel_cols[ck] = era_spans(own)
        return self._channel_cols[ck]

    def _genre_weight(self, channel: dict[str, Any], genres: list[str]) -> float:
        gw = self.channel_json(channel, "genre_weights")
        if not gw or not genres:
            return 1.0
        return max(float(gw.get(g, gw.get(g.lower(), 1.0))) for g in genres)

    @staticmethod
    def _daypart_genre_weight(daypart: dict[str, Any], item_genres: list[str]) -> float:
        """What a daypart makes of a programme's genres: the largest of the weights it gives to
        any of them, 1 when it names none of them, and 0 when it gives 0 to any of them, since a
        0 says this kind of programme does not belong at this hour (a game show by day) whatever
        else it is. This is how a channel keeps its quiz for the evening and its soap at half
        past seven; like every daypart weight it goes when the rules relax."""
        weights = daypart.get("genres")
        if not weights or not item_genres:
            return 1.0
        have = {g.casefold() for g in canonical_all(item_genres)}
        found = [float(w) for name, w in weights.items() if (canonical(name) or name).casefold() in have]
        return 0.0 if found and min(found) <= 0 else max(found, default=1.0)

    def channel_decades(self, channel: dict[str, Any]) -> tuple[int, ...]:
        """The decades a channel plays; empty means any. Held as JSON on the channel row."""
        cached = self._decades.get(channel["id"])
        if cached is None:
            raw = json_field(channel.get("decades")) or []
            cached = self._decades[channel["id"]] = tuple(int(d) for d in raw if isinstance(d, (int, float, str))
                                                          and str(d).isdigit())
        return cached


    def programme(self, channel: dict[str, Any], rng: random.Random, t: int, gap: int,
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
        dayparts = dayparts_for_weekday(weekday_n, self.settings, self.channel_json(channel, "daypart_profile"))
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

        decades = self.channel_decades(channel)

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
                w *= float(dp.get(kind, 1.0)) * self._daypart_genre_weight(dp, item.get("genres") or [])
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
            own = self.library.free_shows.get(channel["id"], ())
            for show in (*own, *self._borrowed(channel, dp, relax)):
                if show.id in barred:
                    sport_ok = (show.category == "sport" and weekend
                                and self.policy.enabled("sport_back_to_back_weekends"))
                    if not sport_ok:
                        continue  # never two episodes of the same series back to back
                resting = bool(show.resting_until and t < show.resting_until)
                if resting and relax < 2:
                    continue
                times_today = placed_today.get(show.id, 0)
                # The daily cap is the first rule to give when nothing else fits: a channel with
                # a few series airs one a third time, each extra airing penalised, rather than
                # going dark for the evening (docs/PLAN.md section 4.5).
                if times_today >= daily_limit and not relaxed:
                    continue
                # One episode per cadence: a week unless the channel sets its own. A series the owner made a strip or anchored keeps its own
                # arrangement, and sport runs in blocks by its dayparts. Only at the last step
                # before a holding card does a series come round early, and then with its next
                # episode, never the last one again: the small hours are where repeats live.
                weekly = show.mode == "auto" and show.category != "sport"
                if weekly and relax < 2 and not self.policy.next_episode_due(channel, self.library.show_last(channel["id"], show), t):
                    continue
                ep = show.next_episode()
                if ep is None:
                    continue
                w = common_weight(ep, "tv", show.end_year)
                if w <= 0:
                    continue
                if times_today:
                    w *= repeat_penalty ** times_today
                if resting:
                    w *= 0.3
                if show.mode == "auto":
                    # Episodes only ever advance. The weekly cadence says when the next one is
                    # wanted, never that the last one is shown again in the meantime.
                    w *= self.policy.cadence_factor(channel, self.library.show_last(channel["id"], show), t, relaxed=relaxed)
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
            # Inside that time a slot is filled from what is already here: the one exception is
            # the last resort, a repeat of an episode already requested, since the shared wanted
            # row means it costs no further download and beats a holding card.
            prepared = self.policy.external_prepared(t)
            # The schedule never promises more new remote material for a day than the ceiling:
            # what pitv_content cannot fetch in time would only be replaced on the morning.
            day_key = broadcast_day_for(t, self.settings, self.tz).isoformat()
            room = self.library.external_per_day.get(day_key, 0) < self.policy.integer("external_new_per_day")
            ext_w = self.policy.external_weight(t)
            for e in externals:
                episode = e["kind"] == "episode"
                if token not in ("show", "tv" if episode else "movie"):
                    continue
                times_today = placed_today.get(e["id"], 0)
                early = episode and not self.policy.next_episode_due(
                    channel, self.library.external_last_placed.get(e["lineup_id"]), t)
                # A series whose every episode has been asked for has nothing new to offer; what
                # has arrived of it airs as the library series it has become.
                finished = episode and not e["spare_wanted"] and next_episode_number(e) is None
                held_back = (finished or not prepared or not room or early or times_today >= (daily_limit if episode else 1)
                             or e["id"] in barred or (e.get("show_id") and e["show_id"] in barred))
                candidate = e
                if held_back:
                    # The last step before a holding card may bring a remote title round again,
                    # but never past the daily cap: without it one unfetched episode was booked
                    # eight times in a day while series on disk waited for their week to pass.
                    if relax < 2 or not e.get("last_spec") or times_today >= (daily_limit if episode else 1):
                        continue
                    candidate = {**e, "_external_repeat": True}
                    prior_run = self.library.external_short_runs.get(e["lineup_id"])
                    if prior_run:
                        candidate["_repeat_spec"] = prior_run[0]
                w = common_weight(e, "tv" if episode else "movie") * ext_w
                if w <= 0:
                    continue
                if episode and not held_back:
                    w *= self.policy.cadence_factor(channel, self.library.external_last_placed.get(e["lineup_id"]), t, relaxed=relaxed)
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
        order. Built once per channel: `advert` runs for every break of every day."""
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

    def advert(self, channel: dict[str, Any], rng: random.Random, t: int, gap: int,
                       near_year: int | None) -> dict[str, Any] | None:
        cands: list[tuple[float, dict[str, Any]]] = []
        fresh: list[tuple[float, dict[str, Any]]] = []
        for ad, duration, w in self._advert_pool(channel):
            if duration > gap:
                continue
            year = ad.get("year")
            if near_year and year is not None and abs(year - near_year) <= self._advert_window:
                w *= 3.0
            last = self.library.ad_last.get((channel["id"], ad["id"]))
            if last is not None and t - last < self._advert_penalty:
                w *= 0.1
            cands.append((w, ad))
            if last is None or t - last > 900:
                fresh.append((w, ad))  # not shown in the last quarter of an hour
        pool = fresh or cands  # never repeat an advert within a break if any other will fit
        if not pool:
            return None
        return rng.choices(pool, weights=[c[0] for c in pool], k=1)[0][1]

    def ident(self, channel: dict[str, Any], rng: random.Random, gap: int) -> dict[str, Any] | None:
        if not channel.get("idents_enabled", 1):
            return None
        fits = [i for i in self.library.idents if float(i["duration"]) <= gap]
        # The channel's own idents, else generic ones; never another channel's, which name it.
        pool = [i for i in fits if i.get("home_channel_id") == channel["id"]] or \
            [i for i in fits if not i.get("home_channel_id")]
        return rng.choice(pool) if pool else None

    def stand_in_ident(self, channel: dict[str, Any], gap: int) -> dict[str, Any] | None:
        """For a channel whose pattern asks for idents but which has none, of its own or generic:
        a slot with no file, which the player fills with the test signal under the channel's
        badge. Only where the pattern asks; gaps are never padded with it."""
        if not channel.get("idents_enabled", 1) or gap < STAND_IN_IDENT_SECONDS:
            return None
        if any(i.get("home_channel_id") in (None, channel["id"]) for i in self.library.idents):
            return None   # it has idents; none fitted this gap
        return {"id": None, "title": channel.get("short_name") or channel["name"], "duration": STAND_IN_IDENT_SECONDS}
