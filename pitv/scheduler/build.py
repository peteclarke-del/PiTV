"""Build the schedule horizon: N broadcast days for every enabled channel.

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
import json
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

from ..db import (all_settings, effective, now_ts, row_to_dict, rows_to_dicts,
                  run_log_finish, run_log_start, tx)
from .rules import (allowed_at, broadcast_day_for, day_bounds, daypart_for, era_weight,
                    hhmm_to_minutes, is_kids, local_ts, minutes_of_day, parse_pattern, tz_of)

Progress = Callable[[str], None] | None
MAX_ITERATIONS_PER_DAY = 2000


@dataclass
class Slot:
    channel_id: int
    day: str
    start_ts: int
    end_ts: int
    media_id: int | None
    offset: float
    kind: str
    title: str
    subtitle: str = ""
    part: int = 1
    replay: int = 0
    locked: int = 0
    id: int | None = None
    # not persisted
    show_id: int | None = None
    genres: list[str] = field(default_factory=list)
    year: int | None = None

    @property
    def duration(self) -> int:
        return self.end_ts - self.start_ts


@dataclass
class Show:
    id: int
    title: str
    year: int | None
    certificate: str | None
    genres: list[str]
    kids: bool
    home_channel_id: int | None
    mode: str
    anchor_time: str | None
    anchor_days: list[int]
    rest_weeks: int
    episodes: list[dict[str, Any]]      # sorted by season, episode
    end_year: int | None = None          # premiere year + seasons - 1 (approximation)
    next_index: int = 0
    last_placed_ts: int | None = None
    resting_until: int | None = None

    @property
    def anchored(self) -> bool:
        return self.mode in ("strip", "weekly") and bool(self.anchor_time)

    def next_episode(self) -> dict[str, Any] | None:
        if not self.episodes:
            return None
        return self.episodes[self.next_index % len(self.episodes)]

    def advance(self, ts: int, rest_seconds: int) -> None:
        self.next_index += 1
        self.last_placed_ts = ts
        if self.next_index >= len(self.episodes):
            self.next_index = 0
            self.resting_until = ts + rest_seconds


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


class Builder:
    def __init__(self, conn: sqlite3.Connection, *, now: int | None = None,
                 seed: int | None = None, progress: Progress = None) -> None:
        self.conn = conn
        self.settings = all_settings(conn)
        self.tz = tz_of(conn)
        self.now = now or now_ts()
        self.seed = seed if seed is not None else 0
        self.progress = progress or (lambda _m: None)
        self.log: list[str] = []
        self.channels = [row_to_dict(r) for r in conn.execute(
            "SELECT * FROM channels WHERE enabled = 1 ORDER BY number")]
        self._load_library()
        self._load_history()
        self.placed_movies: dict[int, int] = {}        # media_id -> ts placed in this build
        self.ad_last: dict[tuple[int, int], int] = {}  # (channel, media) -> ts
        self.day_programmes: dict[tuple[int, str], list[Slot]] = {}

    # --- loading -------------------------------------------------------------------

    def _load_library(self) -> None:
        conn = self.conn
        self.shows: dict[int, Show] = {}
        show_rows = rows_to_dicts(conn.execute(
            "SELECT * FROM shows WHERE excluded = 0 AND missing = 0"))
        eps = rows_to_dicts(conn.execute(
            "SELECT * FROM media WHERE kind = 'episode' AND excluded = 0 AND missing = 0"
            " AND duration IS NOT NULL AND show_id IS NOT NULL"
            " ORDER BY show_id, COALESCE(season, 999), COALESCE(episode, 999), path"))
        by_show: dict[int, list[dict[str, Any]]] = {}
        for e in eps:
            e = effective(e)
            by_show.setdefault(e["show_id"], []).append(e)
        for row in show_rows:
            s = effective(row)
            episodes = by_show.get(s["id"], [])
            if not episodes:
                continue
            for e in episodes:  # episodes inherit show-level metadata when they lack it
                e.setdefault("certificate", None)
                e["certificate"] = e.get("certificate") or s.get("certificate")
                e["year"] = e.get("year") or s.get("year")
                e["genres"] = e.get("genres") or s.get("genres") or []
                e["kids"] = bool(s.get("kids")) or is_kids(e)
                e["show_title"] = s["title"]
            days = s.get("anchor_days")
            if isinstance(days, str):
                try:
                    days = json.loads(days)
                except ValueError:
                    days = None
            if not days:
                days = [0, 1, 2, 3, 4] if s.get("mode") == "strip" else [0]
            self.shows[s["id"]] = Show(
                id=s["id"], title=s["title"], year=s.get("year"), certificate=s.get("certificate"),
                genres=s.get("genres") or [], kids=bool(s.get("kids")),
                home_channel_id=s.get("home_channel_id"), mode=s.get("mode") or "auto",
                anchor_time=s.get("anchor_time"), anchor_days=[int(d) for d in days],
                rest_weeks=int(s.get("rest_weeks") or self.settings.get("series_rest_weeks", 4)),
                episodes=episodes,
                end_year=(s.get("year") + max((int(e.get("season") or 1) for e in episodes), default=1) - 1)
                if s.get("year") else None)
        self.movies: list[dict[str, Any]] = [effective(m) for m in rows_to_dicts(conn.execute(
            "SELECT * FROM media WHERE kind = 'movie' AND excluded = 0 AND missing = 0"
            " AND duration IS NOT NULL"))]
        self.adverts: list[dict[str, Any]] = [effective(m) for m in rows_to_dicts(conn.execute(
            "SELECT * FROM media WHERE kind = 'advert' AND excluded = 0 AND missing = 0"
            " AND duration IS NOT NULL"))]
        self.idents: list[dict[str, Any]] = [effective(m) for m in rows_to_dicts(conn.execute(
            "SELECT * FROM media WHERE kind = 'ident' AND excluded = 0 AND missing = 0"
            " AND duration IS NOT NULL"))]

    def _load_history(self) -> None:
        """Last time each media item was placed (history or already-scheduled), and each
        show's latest placed episode so the cursor can continue from it."""
        conn = self.conn
        self.last_placed: dict[int, int] = {}
        for r in conn.execute("SELECT media_id, MAX(started_at) AS ts FROM history"
                              " WHERE media_id IS NOT NULL GROUP BY media_id"):
            self.last_placed[r["media_id"]] = r["ts"]
        for r in conn.execute("SELECT media_id, MAX(start_ts) AS ts FROM schedule"
                              " WHERE media_id IS NOT NULL AND replay = 0 GROUP BY media_id"):
            self.last_placed[r["media_id"]] = max(self.last_placed.get(r["media_id"], 0), r["ts"])

        cursors = {r["show_id"]: r for r in conn.execute("SELECT * FROM show_cursor")}
        for show in self.shows.values():
            latest_ts, latest_idx = None, None
            for idx, ep in enumerate(show.episodes):
                ts = self.last_placed.get(ep["id"])
                if ts is not None and (latest_ts is None or ts > latest_ts):
                    latest_ts, latest_idx = ts, idx
            if latest_idx is not None:
                show.last_placed_ts = latest_ts
                show.next_index = latest_idx + 1
                if show.next_index >= len(show.episodes):
                    show.next_index = 0
                    show.resting_until = latest_ts + show.rest_weeks * 7 * 86400
            cur = cursors.get(show.id)
            if cur and (latest_ts is None or cur["set_at"] > latest_ts):
                for idx, ep in enumerate(show.episodes):
                    if (ep.get("season"), ep.get("episode")) >= (cur["next_season"], cur["next_episode"]):
                        show.next_index = idx
                        show.resting_until = None
                        break

    # --- helpers -----------------------------------------------------------------------

    def _rng(self, channel_id: int, day: date) -> random.Random:
        key = f"{self.seed}:{channel_id}:{day.isoformat()}".encode()
        return random.Random(int.from_bytes(hashlib.sha256(key).digest()[:8], "big"))

    def _channel_setting(self, channel: dict[str, Any], key: str) -> Any:
        value = channel.get(key)
        if isinstance(value, str) and value:
            try:
                value = json.loads(value)
            except ValueError:
                value = None
        if value:
            return value
        return self.settings.get({"era_weights": "era_weights", "kind_weights": "kind_weights",
                                  "daypart_profile": "dayparts"}[key])

    def _genre_weight(self, channel: dict[str, Any], genres: list[str]) -> float:
        gw = channel.get("genre_weights")
        if isinstance(gw, str):
            try:
                gw = json.loads(gw)
            except ValueError:
                gw = None
        if not gw or not genres:
            return 1.0
        weights = [float(gw.get(g, gw.get(g.lower(), 1.0))) for g in genres]
        return max(weights) if weights else 1.0

    def _existing_slots(self, channel_id: int, day: str) -> list[Slot]:
        rows = self.conn.execute(
            "SELECT s.*, m.show_id, m.genres AS mgenres, m.year AS myear FROM schedule s"
            " LEFT JOIN media m ON m.id = s.media_id"
            " WHERE s.channel_id = ? AND s.day = ? AND s.replay = 0 ORDER BY s.start_ts",
            (channel_id, day)).fetchall()
        out = []
        for r in rows:
            genres = []
            if r["mgenres"]:
                try:
                    genres = json.loads(r["mgenres"])
                except ValueError:
                    genres = []
            out.append(Slot(channel_id=r["channel_id"], day=r["day"], start_ts=r["start_ts"],
                            end_ts=r["end_ts"], media_id=r["media_id"], offset=r["offset"],
                            kind=r["kind"], title=r["title"], subtitle=r["subtitle"],
                            part=r["part"], replay=r["replay"], locked=r["locked"], id=r["id"],
                            show_id=r["show_id"], genres=genres, year=r["myear"]))
        return out

    def _yesterday_programmes(self, channel_id: int, day: date) -> list[Slot]:
        key = (channel_id, (day - timedelta(days=1)).isoformat())
        if key in self.day_programmes:
            return self.day_programmes[key]
        slots = [s for s in self._existing_slots(channel_id, key[1]) if s.kind == "programme"]
        self.day_programmes[key] = slots
        return slots

    # --- choosing ----------------------------------------------------------------------

    def _choose_programme(self, channel: dict[str, Any], rng: random.Random, t: int, gap: int,
                          token: str, placed_today: dict[int, int], prev: Slot | None,
                          yesterday: list[Slot], relax: int = 0) -> tuple[dict[str, Any], Show | None] | None:
        """Pick a programme for a gap. Selection is two-stage so the TV/movie balance follows the
        channel's kind weights rather than the size of each pool: choose the kind, then the item.

        relax=0 normal rules; relax=1 ignore daypart preferences and daily show limits;
        relax=2 additionally allow movies that aired recently. Certificates are never relaxed."""
        start_min = minutes_of_day(t, self.tz)
        dayparts = self._channel_setting(channel, "daypart_profile")
        dp = daypart_for(start_min, dayparts)
        era_weights = self._channel_setting(channel, "era_weights")
        kind_weights = self._channel_setting(channel, "kind_weights")
        tol = int(self.settings.get("duration_tolerance_minutes", 5)) * 60
        movie_repeat = int(self.settings.get("movie_repeat_days", 21)) * 86400
        same_slot_bonus = float(self.settings.get("same_slot_bonus", 3.0))
        genre_penalty = float(self.settings.get("genre_repeat_penalty", 0.4))
        daily_limit = int(self.settings.get("show_daily_limit", 2))
        repeat_penalty = float(self.settings.get("show_repeat_penalty", 0.3))
        max_minutes = dp.get("max_minutes")
        weekend = datetime.fromtimestamp(t, self.tz).weekday() >= 5
        relaxed = relax >= 1

        def common_weight(item: dict[str, Any], kind: str, end_year: int | None = None) -> float:
            w = era_weight(item.get("year"), era_weights, end_year)
            if w <= 0:
                return 0.0
            if not allowed_at(item, start_min, self.settings):
                return 0.0
            duration = float(item["duration"])
            if duration > gap + tol:
                return 0.0
            if not relaxed:
                w *= float(dp.get(kind, 1.0))
            kids = is_kids(item) or bool(item.get("kids"))
            if kids:
                kw = float(dp.get("kids", 1.0))
                if weekend and self.settings.get("weekend_kids_breakfast") and dp.get("name") == "Breakfast":
                    kw = max(kw, 4.0)
                w *= kw if not relaxed else max(kw, 0.2)
            w *= self._genre_weight(channel, item.get("genres") or [])
            if max_minutes and duration > max_minutes * 60 and not relaxed:
                w *= 0.15
            if prev is not None and prev.kind == "programme" and prev.genres and item.get("genres"):
                if set(g.lower() for g in prev.genres) & set(g.lower() for g in item["genres"]):
                    w *= genre_penalty
            if gap - duration < 600:
                w *= 1.5  # fills the gap neatly
            return w

        tv_cands: list[tuple[float, dict[str, Any], Show | None]] = []
        movie_cands: list[tuple[float, dict[str, Any], Show | None]] = []

        if token in ("show", "tv"):
            for show in self.shows.values():
                if show.anchored:
                    continue
                if show.home_channel_id not in (None, channel["id"]):
                    continue
                resting = bool(show.resting_until and t < show.resting_until)
                if resting and relax < 2:
                    continue
                times_today = placed_today.get(show.id, 0)
                if times_today >= daily_limit and not relaxed:
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
                for y in yesterday:
                    if y.show_id == show.id:
                        y_min = minutes_of_day(y.start_ts, self.tz)
                        w *= same_slot_bonus if abs(y_min - start_min) <= 30 else 0.6
                        break
                tv_cands.append((w, ep, show))
        if token in ("show", "movie"):
            for m in self.movies:
                last = self.last_placed.get(m["id"])
                recent = m["id"] in self.placed_movies or (last is not None and t - last < movie_repeat)
                if recent and relax < 2:
                    continue
                w = common_weight(m, "movie")
                if w <= 0:
                    continue
                if recent:
                    w *= 0.2
                elif last is not None:
                    w *= min(2.0, (t - last) / movie_repeat)  # prefer the least recently aired
                else:
                    w *= 2.0
                movie_cands.append((w, m, None))

        kinds: list[tuple[float, list]] = []
        if tv_cands:
            kinds.append((float(kind_weights.get("tv", 1.0)) * (1.0 if relaxed else float(dp.get("tv", 1.0))), tv_cands))
        if movie_cands:
            kinds.append((float(kind_weights.get("movie", 1.0)) * (1.0 if relaxed else float(dp.get("movie", 1.0))), movie_cands))
        if not kinds:
            return None
        pool = rng.choices(kinds, weights=[max(k[0], 1e-6) for k in kinds], k=1)[0][1]
        pick = rng.choices(pool, weights=[c[0] for c in pool], k=1)[0]
        return pick[1], pick[2]

    def _choose_advert(self, channel: dict[str, Any], rng: random.Random, t: int, gap: int,
                       near_year: int | None) -> dict[str, Any] | None:
        window = int(self.settings.get("advert_year_window", 3))
        penalty_s = int(self.settings.get("advert_repeat_penalty_hours", 6)) * 3600
        era_weights = self._channel_setting(channel, "era_weights")
        cands: list[tuple[float, dict[str, Any]]] = []
        for ad in self.adverts:
            if float(ad["duration"]) > gap:
                continue
            w = era_weight(ad.get("year"), era_weights) or 0.05
            if near_year and ad.get("year") is not None:
                w *= 3.0 if abs(ad["year"] - near_year) <= window else 1.0
            last = self.ad_last.get((channel["id"], ad["id"]))
            if last is not None and t - last < penalty_s:
                w *= 0.1
            cands.append((w, ad))
        if not cands:
            return None
        return rng.choices(cands, weights=[c[0] for c in cands], k=1)[0][1]

    def _choose_ident(self, channel: dict[str, Any], rng: random.Random, gap: int) -> dict[str, Any] | None:
        if not channel.get("idents_enabled", 1):
            return None
        mine = [i for i in self.idents if i.get("channel_hint") == channel["number"] and float(i["duration"]) <= gap]
        pool = mine or [i for i in self.idents if float(i["duration"]) <= gap]
        return rng.choice(pool) if pool else None

    # --- building ----------------------------------------------------------------------

    def _anchors_for(self, channel: dict[str, Any], day: date, day_start: int, day_end: int) -> list[tuple[int, Show]]:
        out = []
        weekday = day.weekday()
        for show in self.shows.values():
            if not show.anchored or show.home_channel_id != channel["id"]:
                continue
            if weekday not in show.anchor_days:
                continue
            if show.resting_until and day_start < show.resting_until:
                continue
            ts = local_ts(day, show.anchor_time or "00:00", self.tz)
            if ts < day_start:
                ts += 86400
            if day_start <= ts < day_end:
                out.append((ts, show))
        out.sort(key=lambda x: x[0])
        return out

    def build_channel_day(self, channel: dict[str, Any], day: date, force: bool,
                          from_ts: int | None = None) -> list[Slot]:
        """Build (or complete) one channel-day. Returns the new slots, or [] if nothing to do.

        force: rebuild everything that has not started and is not locked.
        from_ts: rebuild from this time onwards only (implies force for that part)."""
        day_str = day.isoformat()
        day_start, day_end, next_day_start = day_bounds(day, self.settings, self.tz)
        existing = self._existing_slots(channel["id"], day_str)
        rng = self._rng(channel["id"], day)
        rest_seconds = 7 * 86400

        # Keep what must stay; everything else is rebuilt.
        if force or from_ts is not None:
            cut = max(self.now, from_ts) if from_ts is not None else self.now
            keep = [s for s in existing if s.locked or s.start_ts < cut]
        else:
            if existing and max(s.end_ts for s in existing) >= day_end - 60:
                return []  # already complete
            keep = list(existing)
        self._cut = (cut if (force or from_ts is not None) else None)
        keep.sort(key=lambda s: s.start_ts)
        keep_ids = {s.id for s in keep}
        placed_today: dict[int, int] = {}
        for s in keep:
            if s.show_id:
                placed_today[s.show_id] = placed_today.get(s.show_id, 0) + 1
        for s in keep:
            if s.media_id:
                self.last_placed[s.media_id] = max(self.last_placed.get(s.media_id, 0), s.start_ts)

        fixed: list[tuple[int, int, Any]] = [(s.start_ts, s.end_ts, s) for s in keep]
        for ts, show in self._anchors_for(channel, day, day_start, day_end):
            if placed_today.get(show.id):
                continue
            ep = show.next_episode()
            if ep is None:
                continue
            end = ts + int(float(ep["duration"]))
            if any(not (end <= fs or ts >= fe) for fs, fe, _ in fixed):
                self.log.append(f"{channel['name']} {day_str}: anchor {show.title} at {show.anchor_time} clashes with a kept slot")
                continue
            fixed.append((ts, end, ("anchor", show, ep)))
            placed_today[show.id] = placed_today.get(show.id, 0) + 1
        fixed.sort(key=lambda x: x[0])

        pattern = parse_pattern(channel.get("pattern") or "show")
        ads_on = bool(channel.get("ads_enabled"))
        ads_per_break = int(channel.get("ads_per_break") or 2)
        if not ads_on:
            pattern = [tok for tok in pattern if tok not in ("ad", "break")] or ["show"]
        rounding = int(self.settings.get("start_rounding_minutes", 5)) * 60

        new_slots: list[Slot] = []
        all_slots: list[Slot] = list(keep)
        yesterday = self._yesterday_programmes(channel["id"], day)
        t = day_start
        pat_idx = 0
        overrun = int(self.settings.get("end_of_day_overrun_minutes", 30)) * 60
        prev: Slot | None = keep[-1] if keep else None
        last_programme_year: int | None = None
        iterations = 0
        fixed_queue = [f for f in fixed if f[0] >= t]

        def emit(slot: Slot) -> None:
            nonlocal prev
            new_slots.append(slot)
            all_slots.append(slot)
            prev = slot

        while t < day_end and iterations < MAX_ITERATIONS_PER_DAY:
            iterations += 1
            # A fixed item starting now (or that we have run into)?
            if fixed_queue and t >= fixed_queue[0][0] - 60:
                fs, fe, payload = fixed_queue.pop(0)
                if isinstance(payload, Slot):
                    t = max(t, fe)
                    prev = payload
                    continue
                _, show, ep = payload
                start = max(t, fs)
                slot = self._programme_slot(channel, day_str, start, ep, show)
                emit(slot)
                show.advance(start, show.rest_weeks * rest_seconds)
                last_programme_year = ep.get("year")
                t = slot.end_ts
                pat_idx += 1  # the anchor stands in for a 'show' token
                continue
            boundary = fixed_queue[0][0] if fixed_queue else day_end
            gap = boundary - t
            if gap <= 0:
                t = boundary
                continue
            token = pattern[pat_idx % len(pattern)]
            pat_idx += 1

            if token in ("show", "tv", "movie") and rounding and ads_on:
                # Tidy start time: pad with adverts up to the next 5-minute boundary.
                target = -(-t // rounding) * rounding
                if 0 < target - t <= 4 * 60 and target < boundary:
                    t = self._pad(channel, rng, day_str, t, target, last_programme_year, emit, limit=boundary)
                    gap = boundary - t
            if not fixed_queue and token in ("show", "tv", "movie"):
                gap += overrun  # the last programme of the day may run past closedown

            if token in ("ad", "break"):
                count = ads_per_break if token == "break" else 1
                for _ in range(count):
                    ad = self._choose_advert(channel, rng, t, boundary - t, last_programme_year)
                    if ad is None:
                        break
                    slot = self._media_slot(channel, day_str, t, ad, "advert")
                    emit(slot)
                    self.ad_last[(channel["id"], ad["id"])] = t
                    t = slot.end_ts
                continue
            if token == "ident":
                ident = self._choose_ident(channel, rng, boundary - t)
                if ident is not None:
                    slot = self._media_slot(channel, day_str, t, ident, "ident")
                    emit(slot)
                    t = slot.end_ts
                continue

            choice = self._choose_programme(channel, rng, t, gap, token, placed_today, prev, yesterday)
            if choice is None and token != "show":
                choice = self._choose_programme(channel, rng, t, gap, "show", placed_today, prev, yesterday)
            for relax in (1, 2):
                if choice is None:
                    choice = self._choose_programme(channel, rng, t, gap, "show", placed_today, prev, yesterday, relax=relax)
            if choice is None:
                # Nothing fits this gap: pad with adverts/idents, then filler up to the boundary.
                t = self._pad(channel, rng, day_str, t, boundary, last_programme_year, emit, limit=boundary)
                if t < boundary:
                    if boundary - t > 120:
                        self.log.append(f"{channel['name']} {day_str}: filler {(boundary - t)//60} min at {self._hhmm(t)}")
                    emit(Slot(channel_id=channel["id"], day=day_str, start_ts=t, end_ts=boundary,
                              media_id=None, offset=0, kind="filler",
                              title="Programmes will continue shortly"))
                    t = boundary
                continue
            item, show = choice
            slot = self._programme_slot(channel, day_str, t, item, show)
            emit(slot)
            if show is not None:
                show.advance(t, show.rest_weeks * rest_seconds)
                placed_today[show.id] = placed_today.get(show.id, 0) + 1
            else:
                self.placed_movies[item["id"]] = t
            self.last_placed[item["id"]] = t
            last_programme_year = item.get("year")
            t = slot.end_ts

        # Overnight replay.
        replay_slots = self._overnight(channel, day_str, day_end, next_day_start, all_slots)
        return new_slots + replay_slots

    def _pad(self, channel, rng, day_str, t, target, near_year, emit, limit) -> int:
        """Fill t..target with adverts (ad channels) or idents; returns the new t."""
        guard = 0
        while t < target and guard < 20:
            guard += 1
            gap = target - t
            item = None
            kind = "advert"
            if channel.get("ads_enabled"):
                item = self._choose_advert(channel, rng, t, gap, near_year)
            if item is None:
                item = self._choose_ident(channel, rng, gap)
                kind = "ident"
            if item is None:
                break
            slot = self._media_slot(channel, day_str, t, item, kind)
            emit(slot)
            if kind == "advert":
                self.ad_last[(channel["id"], item["id"])] = t
            t = slot.end_ts
        return t

    def _programme_slot(self, channel, day_str, start, item, show: Show | None) -> Slot:
        duration = int(round(float(item["duration"])))
        if show is not None:
            title = show.title
            se = ""
            if item.get("season") is not None and item.get("episode") is not None:
                se = f"S{int(item['season']):02d}E{int(item['episode']):02d} "
            subtitle = f"{se}{item.get('title') or ''}".strip()
        else:
            title = item["title"]
            year = item.get("year")
            cert = item.get("certificate") or ""
            subtitle = " ".join(x for x in (f"({year})" if year else "", cert) if x)
        return Slot(channel_id=channel["id"], day=day_str, start_ts=start, end_ts=start + duration,
                    media_id=item["id"], offset=0, kind="programme", title=title, subtitle=subtitle,
                    show_id=show.id if show else None, genres=item.get("genres") or [],
                    year=item.get("year"))

    def _media_slot(self, channel, day_str, start, item, kind) -> Slot:
        duration = max(1, int(round(float(item["duration"]))))
        return Slot(channel_id=channel["id"], day=day_str, start_ts=start, end_ts=start + duration,
                    media_id=item["id"], offset=0, kind=kind, title=item["title"],
                    subtitle=str(item.get("year") or ""), year=item.get("year"))

    def _overnight(self, channel, day_str, day_end, next_day_start, day_slots: list[Slot]) -> list[Slot]:
        replay_from = channel.get("overnight_replay_from") or self.settings.get("day_start", "08:00")
        day = parse_day(day_str)
        from_ts = local_ts(day, replay_from, self.tz)
        if from_ts < local_ts(day, self.settings.get("day_start", "08:00"), self.tz):
            from_ts += 86400
        source = [s for s in sorted(day_slots, key=lambda s: s.start_ts)
                  if s.start_ts >= from_ts and s.kind != "filler"]
        out: list[Slot] = []
        t = max(day_end, max((s.end_ts for s in day_slots), default=day_end))
        for s in source:
            if t >= next_day_start:
                break
            end = min(t + s.duration, next_day_start)
            out.append(Slot(channel_id=channel["id"], day=day_str, start_ts=t, end_ts=end,
                            media_id=s.media_id, offset=s.offset, kind=s.kind, title=s.title,
                            subtitle=s.subtitle, part=s.part, replay=1))
            t = end
        return out

    def _hhmm(self, ts: int) -> str:
        return datetime.fromtimestamp(ts, self.tz).strftime("%H:%M")

    # --- persistence ---------------------------------------------------------------------

    def save(self, channel_id: int, day: date, slots: list[Slot], force: bool) -> None:
        day_str = day.isoformat()
        conn = self.conn
        cut = getattr(self, "_cut", None)
        with tx(conn):
            # Remove replaced slots: unlocked, not started, plus all old replay slots for the day.
            conn.execute("DELETE FROM schedule WHERE channel_id = ? AND day = ? AND replay = 1",
                         (channel_id, day_str))
            if cut is not None:
                conn.execute("DELETE FROM schedule WHERE channel_id = ? AND day = ? AND locked = 0"
                             " AND start_ts >= ? AND replay = 0", (channel_id, day_str, cut))
            conn.executemany(
                "INSERT INTO schedule(channel_id, day, start_ts, end_ts, media_id, offset, kind, part,"
                " replay, locked, title, subtitle) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(s.channel_id, s.day, s.start_ts, s.end_ts, s.media_id, s.offset, s.kind, s.part,
                  s.replay, s.locked, s.title, s.subtitle) for s in slots])


def build_horizon(conn: sqlite3.Connection, *, start_day: date | None = None,
                  days: int | None = None, force: bool = False,
                  channel_numbers: list[int] | None = None, seed: int | None = None,
                  progress: Progress = None, now: int | None = None) -> dict[str, Any]:
    settings = all_settings(conn)
    tz = tz_of(conn)
    now = now or now_ts()
    if start_day is None:
        start_day = broadcast_day_for(now, settings, tz)
    days = days or int(settings.get("horizon_days", 7))
    if seed is None:
        seed = int(hashlib.sha256(start_day.isoformat().encode()).hexdigest()[:8], 16)
    run_id = run_log_start(conn, "schedule")
    builder = Builder(conn, now=now, seed=seed, progress=progress)
    channels = builder.channels
    if channel_numbers:
        channels = [c for c in channels if c["number"] in channel_numbers]
    built = 0
    programmes = 0
    for i in range(days):
        day = start_day + timedelta(days=i)
        for channel in channels:
            slots = builder.build_channel_day(channel, day, force)
            if not slots:
                continue
            builder.save(channel["id"], day, slots, force)
            builder.day_programmes[(channel["id"], day.isoformat())] = [
                s for s in slots if s.kind == "programme" and not s.replay]
            n = sum(1 for s in slots if s.kind == "programme" and not s.replay)
            programmes += n
            built += 1
            if progress:
                progress(f"{day} {channel['name']}: {n} programmes")
    status = "ok" if not builder.log else "warning"
    summary = f"{built} channel-days built, {programmes} programmes; {len(builder.log)} notes"
    run_log_finish(conn, run_id, status, summary, builder.log)
    return {"status": status, "summary": summary, "notes": builder.log, "run_id": run_id,
            "start_day": start_day.isoformat(), "days": days}


def horizon_end(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(end_ts) AS e FROM schedule").fetchone()
    return row["e"] if row and row["e"] else None


def needs_rebuild(conn: sqlite3.Connection, now: int | None = None) -> bool:
    now = now or now_ts()
    end = horizon_end(conn)
    threshold = int(all_settings(conn).get("rebuild_when_days_left", 2)) * 86400
    return end is None or end - now < threshold


def rebuild_from(conn: sqlite3.Connection, channel_id: int, from_ts: int, *,
                 now: int | None = None, seed: int | None = None) -> dict[str, Any]:
    """Rebuild one channel from a point in time to the end of that broadcast day.

    Used by the admin schedule editor after a slot is removed, replaced or inserted."""
    settings = all_settings(conn)
    tz = tz_of(conn)
    now = now or now_ts()
    from_ts = max(from_ts, now)
    day = broadcast_day_for(from_ts, settings, tz)
    if seed is None:
        seed = int(hashlib.sha256(day.isoformat().encode()).hexdigest()[:8], 16)
    builder = Builder(conn, now=now, seed=seed)
    channel = next((c for c in builder.channels if c["id"] == channel_id), None)
    if channel is None:
        return {"status": "error", "summary": "channel not found or disabled"}
    slots = builder.build_channel_day(channel, day, force=True, from_ts=from_ts)
    builder.save(channel_id, day, slots, True)
    return {"status": "ok" if not builder.log else "warning",
            "summary": f"{sum(1 for s in slots if s.kind == 'programme' and not s.replay)} programmes",
            "notes": builder.log}
