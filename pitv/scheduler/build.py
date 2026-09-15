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
import logging
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

from ..db import (all_settings, effective, enabled_channels, now_ts, rows_to_dicts,
                  run_log_finish, run_log_start, tx)
from .rules import (EraSpans, allowed_at, broadcast_day_for, day_bounds, daypart_end_minutes,
                    daypart_for, dayparts_for_weekday, era_spans, era_weight_spans, hhmm_to_minutes,
                    is_kids, local_ts, minutes_of_day, parse_pattern, tz_of)

Progress = Callable[[str], None] | None
MAX_ITERATIONS_PER_DAY = 2000
log = logging.getLogger("pitv.scheduler")


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
    block: str | None = None
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
    category: str = "general"
    next_index: int = 0
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
        if self.next_index >= len(self.episodes):
            self.next_index = 0
            self.resting_until = ts + rest_seconds


def episode_subtitle(item: dict[str, Any]) -> str:
    """The episode's own title; falls back to 'Episode N' when the file has no title."""
    title = (item.get("title") or "").strip()
    if title:
        return title
    if item.get("episode") is not None:
        return f"Episode {int(item['episode'])}"
    return ""


def slot_titles(item: dict[str, Any], show_title: str | None = None) -> tuple[str, str]:
    """Guide title and subtitle for a programme. Episodes: the series name over the episode
    title (never the SxxEyy code). Films: '(year) certificate'. Music videos: '(year) genres'."""
    year = f"({item['year']})" if item.get("year") else ""
    if item.get("kind") == "episode":
        return show_title or item["title"], episode_subtitle(item)
    if item.get("kind") == "music":
        detail = ", ".join(_json_field(item.get("genres")) or [])
    else:
        detail = item.get("certificate") or ""
    return item["title"], " ".join(x for x in (year, detail) if x)


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _json_field(value: Any) -> Any:
    """A column stored as JSON text (or already decoded by row_to_dict); unreadable -> None."""
    if isinstance(value, str):
        if not value:
            return None
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


# Per channel, the window of unlocked slots a build is about to replace: (from_ts, to_ts) with
# to_ts None for "everything from from_ts on" (a forced horizon build).
Rebuild = dict[int, tuple[int, int | None]]


class Builder:
    def __init__(self, conn: sqlite3.Connection, *, now: int | None = None,
                 seed: int | None = None, progress: Progress = None,
                 exclude_media_ids: set[int] | None = None, rebuild: Rebuild | None = None) -> None:
        self.conn = conn
        self.exclude_media_ids: set[int] = set(exclude_media_ids or ())
        self.rebuild: Rebuild = rebuild or {}
        self.settings = all_settings(conn)
        self.tz = tz_of(conn)
        self.now = now or now_ts()
        self.seed = seed if seed is not None else 0
        self.progress = progress or (lambda _m: None)
        self.log: list[str] = []
        self.channels = enabled_channels(conn)
        # Minute of day the broadcast day starts (08:00 = 480); minutes before it belong to the
        # previous day and are counted past 1440 so comparisons stay monotonic.
        self.day_start_min = hhmm_to_minutes(self.settings.get("day_start", "08:00"))
        self.movie_placements: dict[int, list[int]] = {}  # media_id -> every placement time known
        # Parsed once: the candidate loops run tens of thousands of times per build.
        self._global_spans = era_spans(self.settings.get("era_weights"))
        self._advert_spans = era_spans(self.settings.get("advert_era_weights") or {"1980-1989": 0.85, "1990-1999": 0.15})
        self._advert_era_w: dict[int | None, float] = {}
        self._era_band_cache: dict[tuple[int | None, int | None], str] = {}
        self._channel_cols: dict[tuple[int, str], Any] = {}
        self._load_library()
        self._load_history()
        self.ad_last: dict[tuple[int, int], int] = {}  # (channel, media) -> ts
        self.day_programmes: dict[tuple[int, str], list[Slot]] = {}
        self._cut: int | None = None                   # set by _keep_slots; save() deletes from here

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
            if e["id"] in self.exclude_media_ids:
                continue
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
                e["category"] = s.get("category") or "general"
            days = _json_field(s.get("anchor_days"))
            if not days:
                days = [0, 1, 2, 3, 4] if s.get("mode") == "strip" else [0]
            self.shows[s["id"]] = Show(
                id=s["id"], title=s["title"], year=s.get("year"), certificate=s.get("certificate"),
                genres=s.get("genres") or [], kids=bool(s.get("kids")),
                home_channel_id=s.get("home_channel_id"), mode=s.get("mode") or "auto",
                anchor_time=s.get("anchor_time"), anchor_days=[int(d) for d in days],
                rest_weeks=int(s.get("rest_weeks") or self.settings.get("series_rest_weeks", 4)),
                episodes=episodes, category=s.get("category") or "general",
                end_year=(s.get("year") + max((int(e.get("season") or 1) for e in episodes), default=1) - 1)
                if s.get("year") else None)
        self.movies: list[dict[str, Any]] = [effective(m) for m in rows_to_dicts(conn.execute(
            "SELECT * FROM media WHERE kind = 'movie' AND excluded = 0 AND missing = 0"
            " AND duration IS NOT NULL")) if m["id"] not in self.exclude_media_ids]
        for m in self.movies:
            m["kids"] = is_kids(m)
        self._era_pools()
        self.music: list[dict[str, Any]] = [effective(m) for m in rows_to_dicts(conn.execute(
            "SELECT * FROM media WHERE kind = 'music' AND excluded = 0 AND missing = 0 AND duration IS NOT NULL"))
            if m["id"] not in self.exclude_media_ids]
        self.adverts: list[dict[str, Any]] = [effective(m) for m in rows_to_dicts(conn.execute(
            "SELECT * FROM media WHERE kind = 'advert' AND excluded = 0 AND missing = 0"
            " AND duration IS NOT NULL"))]
        self.idents: list[dict[str, Any]] = [effective(m) for m in rows_to_dicts(conn.execute(
            "SELECT * FROM media WHERE kind = 'ident' AND excluded = 0 AND missing = 0"
            " AND duration IS NOT NULL"))]

    def _era_band(self, year: int | None, end_year: int | None = None) -> str:
        """Which configured era span an item falls in (by best-weighted overlap), for pool sizing."""
        key = (year, end_year)
        band = self._era_band_cache.get(key)
        if band is None:
            best, best_w = "none", 0.0
            for lo, hi, weight in self._global_spans:
                w = era_weight_spans(year, ((lo, hi, weight),), end_year)
                if w > best_w:
                    best, best_w = f"{lo}-{hi}", w
            band = self._era_band_cache[key] = best
        return band

    def _era_pools(self) -> None:
        self.era_pool: dict[tuple[str, str], int] = {}
        for show in self.shows.values():
            k = ("tv", self._era_band(show.year, show.end_year))
            self.era_pool[k] = self.era_pool.get(k, 0) + 1
        for m in self.movies:
            k = ("movie", self._era_band(m.get("year")))
            self.era_pool[k] = self.era_pool.get(k, 0) + 1

    def _pool_factor(self, kind: str, year: int | None, end_year: int | None = None) -> float:
        """Divide an item's weight by (pool size ** normalise) so eras with few titles are not
        drowned out by eras with many; normalise=1 makes airtime follow the era weights exactly."""
        norm = float(self.settings.get("era_pool_normalise", 0.5))
        if norm <= 0:
            return 1.0
        n = self.era_pool.get((kind, self._era_band(year, end_year)), 1)
        return 1.0 / (max(1, n) ** norm)

    def _bday_minutes(self, ts: int) -> int:
        """Minutes into the broadcast day's clock: local minute of day, or +1440 after midnight."""
        m = minutes_of_day(ts, self.tz)
        return m if m >= self.day_start_min else m + 1440

    def _load_history(self) -> None:
        """Last time each media item was placed (history or already-scheduled), and each
        show's latest placed episode so the cursor can continue from it.

        Slots inside a channel's rebuild window are about to be deleted, so they count for
        nothing; slots after the window on that channel (later days, already built) still
        count as repeats but not for the episode cursor, so the rebuilt day re-places the
        episodes it replaces rather than skipping past what later days already hold."""
        conn = self.conn
        self.last_placed: dict[int, int] = {}
        cursor_ts: dict[int, int] = {}

        def placed(media_id: int, ts: int, for_cursor: bool = True) -> None:
            self.last_placed[media_id] = max(self.last_placed.get(media_id, 0), ts)
            self.movie_placements.setdefault(media_id, []).append(ts)
            if for_cursor:
                cursor_ts[media_id] = max(cursor_ts.get(media_id, 0), ts)

        for r in conn.execute("SELECT media_id, started_at AS ts FROM history WHERE media_id IS NOT NULL"):
            placed(r["media_id"], r["ts"])
        for r in conn.execute("SELECT media_id, start_ts AS ts, channel_id, locked FROM schedule"
                              " WHERE media_id IS NOT NULL AND replay = 0"):
            window = self.rebuild.get(r["channel_id"])
            if window and not r["locked"] and r["ts"] >= window[0]:
                if window[1] is None or r["ts"] < window[1]:
                    continue
                placed(r["media_id"], r["ts"], for_cursor=False)
            else:
                placed(r["media_id"], r["ts"])

        cursors = {r["show_id"]: r for r in conn.execute("SELECT * FROM show_cursor")}
        for show in self.shows.values():
            latest_ts, latest_idx = None, None
            for idx, ep in enumerate(show.episodes):
                ts = cursor_ts.get(ep["id"])
                if ts is not None and (latest_ts is None or ts > latest_ts):
                    latest_ts, latest_idx = ts, idx
            if latest_idx is not None:
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

    def _channel_json(self, channel: dict[str, Any], key: str) -> Any:
        """A channel's JSON column, parsed once per build rather than once per candidate."""
        ck = (channel["id"], key)
        if ck not in self._channel_cols:
            self._channel_cols[ck] = _json_field(channel.get(key))
        return self._channel_cols[ck]

    def _channel_setting(self, channel: dict[str, Any], key: str) -> Any:
        """A channel's own era/kind weights, falling back to the global setting."""
        return self._channel_json(channel, key) or self.settings.get(key)

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
            genres = _json_field(r["mgenres"]) or []
            out.append(Slot(channel_id=r["channel_id"], day=r["day"], start_ts=r["start_ts"],
                            end_ts=r["end_ts"], media_id=r["media_id"], offset=r["offset"],
                            kind=r["kind"], title=r["title"], subtitle=r["subtitle"],
                            part=r["part"], replay=r["replay"], locked=r["locked"], id=r["id"],
                            block=r["block"], show_id=r["show_id"], genres=genres, year=r["myear"]))
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
                          yesterday: list[Slot], relax: int = 0,
                          last_show_id: int | None = None,
                          avoid_show_ids: set[int] = frozenset(),
                          slack: int = 0) -> tuple[dict[str, Any], Show | None] | None:
        """Pick a programme for a gap. Selection is two-stage so the TV/movie balance follows the
        channel's kind weights rather than the size of each pool: choose the kind, then the item.

        relax=0 normal rules; relax=1 ignore daypart preferences and daily show limits;
        relax=2 additionally allow movies that aired recently. Certificates are never relaxed.
        `slack` is how far past the gap a programme may run: the duration tolerance at closedown,
        zero when a fixed slot follows (it must not be overlapped)."""
        start_min = minutes_of_day(t, self.tz)
        weekday_n = datetime.fromtimestamp(t, self.tz).weekday()
        dayparts = dayparts_for_weekday(weekday_n, self.settings, self._channel_json(channel, "daypart_profile"))
        dp = daypart_for(start_min, dayparts)
        sport_block = float(dp.get("sport", 1.0)) >= 3.0   # docs/PLAN.md §4.5: above 3 sport forms a block
        dp_end = daypart_end_minutes(self._bday_minutes(t), dayparts, 1440)
        spans = self._channel_spans(channel)
        kind_weights = self._channel_setting(channel, "kind_weights")
        tol = slack
        movie_repeat = int(self.settings.get("movie_repeat_days", 21)) * 86400
        same_slot_bonus = float(self.settings.get("same_slot_bonus", 3.0))
        genre_penalty = float(self.settings.get("genre_repeat_penalty", 0.4))
        daily_limit = int(self.settings.get("show_daily_limit", 2))
        repeat_penalty = float(self.settings.get("show_repeat_penalty", 0.3))
        max_minutes = dp.get("max_minutes")
        weekend = weekday_n >= 5
        relaxed = relax >= 1
        kids_rule = channel.get("content") != "cartoons"
        kids_breakfast = weekend and bool(self.settings.get("weekend_kids_breakfast")) and dp.get("name") == "Breakfast"
        unknown_w = float(self.settings.get("unknown_year_weight", 0.0))

        def common_weight(item: dict[str, Any], kind: str, end_year: int | None = None) -> float:
            w = era_weight_spans(item.get("year"), spans, end_year, unknown_w)
            if w <= 0:
                return 0.0
            duration = float(item["duration"])
            if duration > gap + tol:
                return 0.0
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
                if sport_w < 0.5 and not relaxed:
                    return 0.0  # outside sport-friendly dayparts sport is a last resort, not a filler
                w *= sport_w if not relaxed else sport_w * 0.5  # in fallback, sport stays the last choice
            w *= self._pool_factor(kind, item.get("year"), end_year)
            if max_minutes and duration > max_minutes * 60 and not relaxed and not (is_sport and sport_block):
                w *= 0.15
            # Running well past the end of the daypart changes the feel of the next one: a sport
            # block must not swallow the evening, and a long film should not start at teatime.
            overrun_min = (start_min + duration / 60) - dp_end
            if overrun_min > 30 and not relaxed:
                w *= 0.02 if is_sport else (0.5 if kind == "movie" and overrun_min < 75 else 0.25)
            if prev is not None and prev.kind == "programme" and prev.genres and item.get("genres"):
                if set(g.lower() for g in prev.genres) & set(g.lower() for g in item["genres"]):
                    if not (is_sport and sport_block):   # sport blocks are meant to run together
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
                if show.id == last_show_id or show.id in avoid_show_ids:
                    sport_ok = (show.category == "sport" and weekend
                                and self.settings.get("sport_back_to_back_weekends", True))
                    if not sport_ok:
                        continue  # never two episodes of the same series back to back
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
                placements = self.movie_placements.get(m["id"], [])
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
        cands: list[tuple[float, dict[str, Any]]] = []
        fresh: list[tuple[float, dict[str, Any]]] = []
        family_only = bool(channel.get("family_safe_ads")) or channel.get("content") == "cartoons"
        era_w = self._advert_era_w
        for ad in self.adverts:
            if float(ad["duration"]) > gap:
                continue
            if family_only and not ad.get("family_safe", 1):
                continue  # no alcohol, tobacco or adult adverts on a family channel
            year = ad.get("year")
            w = era_w.get(year)
            if w is None:
                w = era_w[year] = era_weight_spans(year, self._advert_spans)
            if w <= 0:
                continue  # adverts must be from the configured decades
            if near_year and ad.get("year") is not None:
                w *= 3.0 if abs(ad["year"] - near_year) <= window else 1.0
            last = self.ad_last.get((channel["id"], ad["id"]))
            if last is not None and t - last < penalty_s:
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

    def _keep_slots(self, existing: list[Slot], day_end: int, force: bool, from_ts: int | None) -> list[Slot] | None:
        """The day's slots that survive this build, sorted, or None when the day is already
        complete and nothing was asked to be rebuilt. Records the cut point `save()` deletes from.

        force: rebuild everything that has not started and is not locked.
        from_ts: rebuild from this time onwards only (implies force for that part)."""
        if force or from_ts is not None:
            self._cut = max(self.now, from_ts) if from_ts is not None else self.now
            keep = [s for s in existing if s.locked or s.start_ts < self._cut]
        else:
            self._cut = None
            if existing and max(s.end_ts for s in existing) >= day_end - 60:
                return None
            keep = list(existing)
        keep.sort(key=lambda s: s.start_ts)
        return keep

    def _show_airing_at(self, channel_id: int, boundary: str, ts: int) -> int | None:
        """show_id of the programme that ends (`end_ts`) or starts (`start_ts`) exactly at ts."""
        assert boundary in ("start_ts", "end_ts")
        row = self.conn.execute(
            f"SELECT m.show_id FROM schedule s JOIN media m ON m.id = s.media_id WHERE s.channel_id = ?"
            f" AND s.{boundary} = ? AND s.kind = 'programme'", (channel_id, ts)).fetchone()
        return row["show_id"] if row else None

    def build_channel_day(self, channel: dict[str, Any], day: date, force: bool,
                          from_ts: int | None = None) -> list[Slot]:
        """Build (or complete) one channel-day. Returns the new slots, or [] if nothing to do
        (see `_keep_slots` for `force` and `from_ts`)."""
        if channel.get("content") == "music":
            return self.build_music_day(channel, day, force, from_ts)
        day_str = day.isoformat()
        day_start, day_end, next_day_start = day_bounds(day, self.settings, self.tz)
        rng = self._rng(channel["id"], day)
        rest_seconds = 7 * 86400

        keep = self._keep_slots(self._existing_slots(channel["id"], day_str), day_end, force, from_ts)
        if keep is None:
            return []
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
        tol = int(self.settings.get("duration_tolerance_minutes", 5)) * 60

        new_slots: list[Slot] = []
        all_slots: list[Slot] = list(keep)
        yesterday = self._yesterday_programmes(channel["id"], day)
        t = day_start
        pat_idx = 0
        overrun = int(self.settings.get("end_of_day_overrun_minutes", 30)) * 60
        prev: Slot | None = keep[-1] if keep else None
        last_programme_year: int | None = None
        # What precedes 08:00 is the tail of yesterday's overnight replay; kept slots update this
        # as the walk passes them (they are in fixed_queue), so do not pre-seed from `keep`.
        last_show_id = self._show_airing_at(channel["id"], "end_ts", day_start)
        iterations = 0
        fixed_queue = [f for f in fixed if f[0] >= t]

        def emit(slot: Slot) -> None:
            nonlocal prev
            new_slots.append(slot)
            all_slots.append(slot)
            prev = slot

        def fill_to(target: int) -> None:
            """Close a small gap before a fixed item with adverts or idents, then filler, so the
            channel-day stays contiguous (the guide and the player both rely on that)."""
            nonlocal t
            t = self._pad(channel, rng, day_str, t, target, last_programme_year, emit, limit=target)
            if t < target:
                emit(Slot(channel_id=channel["id"], day=day_str, start_ts=t, end_ts=target, media_id=None,
                          offset=0, kind="filler", title="Programmes will continue shortly"))
                t = target

        while t < day_end and iterations < MAX_ITERATIONS_PER_DAY:
            iterations += 1
            # A fixed item starting now (or that we have run into)?
            if fixed_queue and t >= fixed_queue[0][0] - 60:
                fs, fe, payload = fixed_queue.pop(0)
                if t < fs:
                    fill_to(fs)
                if isinstance(payload, Slot):
                    t = max(t, fe)
                    prev = payload
                    if payload.kind == "programme":
                        last_show_id = payload.show_id
                    continue
                _, show, ep = payload
                start = max(t, fs)
                slot = self._programme_slot(channel, day_str, start, ep, show)
                emit(slot)
                show.advance(start, show.rest_weeks * rest_seconds)
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
            token = pattern[pat_idx % len(pattern)]
            pat_idx += 1
            next_fixed = fixed_queue[0][2] if fixed_queue else None
            next_show_id = (next_fixed.show_id if isinstance(next_fixed, Slot)
                            else next_fixed[1].id if next_fixed else None)

            if token in ("show", "tv", "movie") and rounding and ads_on:
                # Tidy start time: pad with adverts up to the next 5-minute boundary.
                target = -(-t // rounding) * rounding
                if 0 < target - t <= 4 * 60 and target < boundary:
                    t = self._pad(channel, rng, day_str, t, target, last_programme_year, emit, limit=boundary)
                    gap = boundary - t
            slack = 0
            if not fixed_queue and token in ("show", "tv", "movie"):
                gap += overrun  # the last programme of the day may run past closedown
                slack = tol

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

            avoid = {next_show_id} if next_show_id else set()
            choice = self._choose_programme(channel, rng, t, gap, token, placed_today, prev, yesterday,
                                            last_show_id=last_show_id, avoid_show_ids=avoid, slack=slack)
            if choice is None and token != "show":
                choice = self._choose_programme(channel, rng, t, gap, "show", placed_today, prev, yesterday,
                                                last_show_id=last_show_id, avoid_show_ids=avoid, slack=slack)
            for relax in (1, 2):
                if choice is None:
                    choice = self._choose_programme(channel, rng, t, gap, "show", placed_today, prev, yesterday, relax=relax,
                                                    last_show_id=last_show_id, avoid_show_ids=avoid, slack=slack)
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
                last_show_id = show.id
            else:
                last_show_id = None
                self.movie_placements.setdefault(item["id"], []).append(t)
            self.last_placed[item["id"]] = t
            last_programme_year = item.get("year")
            t = slot.end_ts

        # Overnight replay.
        replay_slots = self._overnight(channel, day_str, day_end, next_day_start, all_slots)
        return new_slots + replay_slots

    # --- music channel ------------------------------------------------------------------

    def build_music_day(self, channel: dict[str, Any], day: date, force: bool, from_ts: int | None) -> list[Slot]:
        """A music channel's day is a sequence of blocks (genre/decade filters, two of them
        concerts). Each block is filled with videos not played recently; consecutive slots share
        the block name so guides show one entry per block."""
        day_str = day.isoformat()
        day_start, day_end, next_day_start = day_bounds(day, self.settings, self.tz)
        keep = self._keep_slots(self._existing_slots(channel["id"], day_str), day_end, force, from_ts)
        if keep is None:
            return []
        rng = self._rng(channel["id"], day)
        blocks = self.settings.get("music_blocks") or []
        video_repeat = int(self.settings.get("music_video_repeat_hours", 36)) * 3600
        concert_repeat = int(self.settings.get("music_concert_repeat_days", 14)) * 86400
        allowed_decades = [int(d) for d in (self.settings.get("music_decades") or [])]
        library = [m for m in self.music if not allowed_decades or m.get("year") is None
                   or (m["year"] // 10) * 10 in allowed_decades]
        used_today: set[int] = {s.media_id for s in keep if s.media_id}
        played_today: dict[int, int] = {}
        new_slots: list[Slot] = []
        all_slots: list[Slot] = list(keep)
        t = max(day_start, max((s.end_ts for s in keep), default=day_start))
        if not library:
            self.log.append(f"{channel['name']} {day_str}: no music videos in the library for decades {allowed_decades}")
            new_slots.append(Slot(channel_id=channel["id"], day=day_str, start_ts=t, end_ts=day_end, media_id=None,
                                  offset=0, kind="filler", title="No music videos"))
            return new_slots + self._overnight(channel, day_str, day_end, next_day_start, all_slots + new_slots)

        def block_start(b: dict[str, Any]) -> int:
            bm = hhmm_to_minutes(b["start"])
            return bm if bm >= self.day_start_min else bm + 1440

        def block_for(ts: int) -> dict[str, Any]:
            mins = self._bday_minutes(ts)
            current = blocks[0] if blocks else {"name": "Music", "genres": [], "decades": []}
            for b in blocks:
                if mins >= block_start(b):
                    current = b
            return current

        def block_end(ts: int) -> int:
            mins = self._bday_minutes(ts)
            ends = [block_start(b) for b in blocks if block_start(b) > mins]
            if not ends:
                return day_end
            return ts + (min(ends) - mins) * 60

        def matches(m: dict[str, Any], b: dict[str, Any], level: int) -> bool:
            """level 0: genre and decade; 1: decade only; 2: anything (within the channel's decades)."""
            genres = [g.lower() for g in (m.get("genres") or [])]
            want_g = [g.lower() for g in (b.get("genres") or [])]
            want_d = [int(d) for d in (b.get("decades") or [])]
            if level <= 0 and want_g and not any(g in genres for g in want_g):
                return False
            if level <= 1 and want_d and (m.get("year") is None or (m["year"] // 10) * 10 not in want_d):
                return False
            return True

        def pick(b: dict[str, Any], gap: int, concert: bool) -> dict[str, Any] | None:
            repeat = concert_repeat if concert else video_repeat
            # Widen step by step: exact block -> decade only -> anything -> already played today.
            for level, allow_recent, allow_today in ((0, False, False), (1, False, False), (2, False, False),
                                                     (0, True, False), (2, True, False), (2, True, True)):
                cands: list[tuple[float, dict[str, Any]]] = []
                for m in library:
                    if bool(m.get("concert")) != concert:
                        continue
                    if m["id"] in used_today and not allow_today:
                        continue
                    if float(m["duration"]) > gap or not matches(m, b, level):
                        continue
                    last = self.last_placed.get(m["id"])
                    age = (t - last) if last is not None else None
                    if age is not None and age < repeat and not allow_recent:
                        continue
                    w = 2.0 if age is None else min(2.0, max(0.02, age / repeat))
                    if allow_today:
                        w *= 1.0 / (1 + played_today.get(m["id"], 0))
                    cands.append((w, m))
                if cands:
                    return rng.choices(cands, weights=[c[0] for c in cands], k=1)[0][1]
            return None

        guard = 0
        concert_done_in_block: str | None = None
        while t < day_end and guard < 3000:
            guard += 1
            b = block_for(t)
            b_end = min(block_end(t), day_end)
            gap = b_end - t
            item = None
            if b.get("concert") and concert_done_in_block != f"{b['name']}@{b['start']}":
                item = pick(b, gap + 20 * 60, True)   # a concert may overrun its block a little
                concert_done_in_block = f"{b['name']}@{b['start']}"
            if item is None:
                item = pick(b, max(gap, 60), False)
            if item is None:
                item = pick(b, 24 * 3600, False)   # anything at all
            if item is None:
                new_slots.append(Slot(channel_id=channel["id"], day=day_str, start_ts=t, end_ts=b_end, media_id=None,
                                      offset=0, kind="filler", title=b.get("name", "Music"), block=b.get("name")))
                t = b_end
                continue
            duration = max(1, int(round(float(item["duration"]))))
            title, sub = slot_titles(item)
            slot = Slot(channel_id=channel["id"], day=day_str, start_ts=t, end_ts=t + duration, media_id=item["id"],
                        offset=0, kind="programme", title=title, subtitle=sub, block=b.get("name", "Music"),
                        genres=item.get("genres") or [], year=item.get("year"))
            new_slots.append(slot)
            all_slots.append(slot)
            used_today.add(item["id"])
            played_today[item["id"]] = played_today.get(item["id"], 0) + 1
            self.last_placed[item["id"]] = t
            t = slot.end_ts
        return new_slots + self._overnight(channel, day_str, day_end, next_day_start, all_slots)

    def _pad(self, channel: dict[str, Any], rng: random.Random, day_str: str, t: int, target: int,
             near_year: int | None, emit: Callable[[Slot], None], limit: int) -> int:
        """Fill t..target with adverts (ad channels) or idents; returns the new t. Idents are
        continuity clips, so at most two run together; the caller puts filler after that."""
        guard = 0
        idents = 0
        while t < target and guard < 20:
            guard += 1
            gap = target - t
            item = None
            kind = "advert"
            if channel.get("ads_enabled"):
                item = self._choose_advert(channel, rng, t, gap, near_year)
            if item is None and idents < 2:
                item = self._choose_ident(channel, rng, gap)
                kind = "ident"
                idents += 1
            if item is None:
                break
            slot = self._media_slot(channel, day_str, t, item, kind)
            emit(slot)
            if kind == "advert":
                self.ad_last[(channel["id"], item["id"])] = t
            t = slot.end_ts
        return t

    def _programme_slot(self, channel: dict[str, Any], day_str: str, start: int, item: dict[str, Any],
                        show: Show | None) -> Slot:
        duration = int(round(float(item["duration"])))
        title, subtitle = slot_titles(item, show.title if show else None)
        return Slot(channel_id=channel["id"], day=day_str, start_ts=start, end_ts=start + duration,
                    media_id=item["id"], offset=0, kind="programme", title=title, subtitle=subtitle,
                    show_id=show.id if show else None, genres=item.get("genres") or [],
                    year=item.get("year"))

    def _media_slot(self, channel: dict[str, Any], day_str: str, start: int, item: dict[str, Any], kind: str) -> Slot:
        """An advert or ident slot; the subtitle is just the year."""
        duration = max(1, int(round(float(item["duration"]))))
        return Slot(channel_id=channel["id"], day=day_str, start_ts=start, end_ts=start + duration,
                    media_id=item["id"], offset=0, kind=kind, title=item["title"],
                    subtitle=str(item.get("year") or ""), year=item.get("year"))

    def _overnight(self, channel: dict[str, Any], day_str: str, day_end: int, next_day_start: int,
                   day_slots: list[Slot]) -> list[Slot]:
        """00:00 to 08:00: replay the day from the channel's `overnight_replay_from`, looping a
        short day, without butting the same series against the day's end or tomorrow's start."""
        replay_from = channel.get("overnight_replay_from") or self.settings.get("day_start", "08:00")
        day = parse_day(day_str)
        from_ts = local_ts(day, replay_from, self.tz)
        if from_ts < local_ts(day, self.settings.get("day_start", "08:00"), self.tz):
            from_ts += 86400
        source = [s for s in sorted(day_slots, key=lambda s: s.start_ts)
                  if s.start_ts >= from_ts and s.kind != "filler"]
        # The replay follows straight on from the day's last programme: never start it with
        # another episode of that same series.
        last_prog = next((s for s in reversed(day_slots) if s.kind == "programme"), None)
        if last_prog is not None and last_prog.show_id is not None:
            while source and (source[0].kind != "programme" or source[0].show_id == last_prog.show_id):
                source.pop(0)
        # If tomorrow is already built, the replay must not end with tomorrow's opening series.
        tomorrow_first = self._show_airing_at(channel["id"], "start_ts", next_day_start)
        out: list[Slot] = []
        t = max(day_end, max((s.end_ts for s in day_slots), default=day_end))
        queue = list(source)
        loops = 0
        while True:
            if not queue:
                # A short day (thin library) is replayed again until 08:00.
                loops += 1
                if not source or loops > 12:
                    break
                queue = list(source)
            s = queue.pop(0)
            if t >= next_day_start:
                break
            if (s.kind == "programme" and tomorrow_first is not None and s.show_id == tomorrow_first
                    and t + s.duration >= next_day_start):
                continue  # would run straight into the same series at 08:00
            if out and out[-1].kind == "programme" and s.kind == "programme" and s.show_id is not None \
                    and out[-1].show_id == s.show_id and not queue:
                continue  # looping a very short day: do not butt the same series together
            end = min(t + s.duration, next_day_start)
            out.append(Slot(channel_id=channel["id"], day=day_str, start_ts=t, end_ts=end,
                            media_id=s.media_id, offset=s.offset, kind=s.kind, title=s.title,
                            subtitle=s.subtitle, part=s.part, replay=1, block=s.block))
            t = end
        if t < next_day_start:
            out.append(Slot(channel_id=channel["id"], day=day_str, start_ts=t, end_ts=next_day_start, media_id=None,
                            offset=0, kind="filler", title="Programmes will resume at 08:00", replay=1))
        return out

    def _hhmm(self, ts: int) -> str:
        return datetime.fromtimestamp(ts, self.tz).strftime("%H:%M")

    # --- persistence ---------------------------------------------------------------------

    def save(self, channel_id: int, day: date, slots: list[Slot]) -> None:
        """Persist a channel-day built by `build_channel_day` (which set the cut point)."""
        day_str = day.isoformat()
        conn = self.conn
        with tx(conn):
            # Remove replaced slots: unlocked, not started, plus all old replay slots for the day.
            conn.execute("DELETE FROM schedule WHERE channel_id = ? AND day = ? AND replay = 1",
                         (channel_id, day_str))
            if self._cut is not None:
                conn.execute("DELETE FROM schedule WHERE channel_id = ? AND day = ? AND locked = 0"
                             " AND start_ts >= ? AND replay = 0", (channel_id, day_str, self._cut))
            conn.executemany(
                "INSERT INTO schedule(channel_id, day, start_ts, end_ts, media_id, offset, kind, part,"
                " replay, locked, title, subtitle, block) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(s.channel_id, s.day, s.start_ts, s.end_ts, s.media_id, s.offset, s.kind, s.part,
                  s.replay, s.locked, s.title, s.subtitle, s.block) for s in slots])


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
    channels = enabled_channels(conn)
    if channel_numbers:
        channels = [c for c in channels if c["number"] in channel_numbers]
    rebuild: Rebuild = {c["id"]: (now, None) for c in channels} if force else {}
    builder = Builder(conn, now=now, seed=seed, progress=progress, rebuild=rebuild)
    built = 0
    programmes = 0
    for i in range(days):
        day = start_day + timedelta(days=i)
        for channel in channels:
            slots = builder.build_channel_day(channel, day, force)
            if not slots:
                continue
            builder.save(channel["id"], day, slots)
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
    log.info("build from %s for %d days (force=%s): %s", start_day, days, force, summary)
    for note in builder.log:
        log.warning("note: %s", note)
    return {"status": status, "summary": summary, "notes": builder.log, "run_id": run_id,
            "start_day": start_day.isoformat(), "days": days, "built": built}


def horizon_end(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(end_ts) AS e FROM schedule").fetchone()
    return row["e"] if row and row["e"] else None


def needs_rebuild(conn: sqlite3.Connection, now: int | None = None) -> bool:
    now = now or now_ts()
    end = horizon_end(conn)
    threshold = int(all_settings(conn).get("rebuild_when_days_left", 2)) * 86400
    return end is None or end - now < threshold


def rebuild_from(conn: sqlite3.Connection, channel_id: int, from_ts: int, *,
                 now: int | None = None, seed: int | None = None,
                 exclude_media_ids: set[int] | None = None) -> dict[str, Any]:
    """Rebuild one channel from a point in time to the end of that broadcast day.

    Used by the admin schedule editor after a slot is removed, replaced or inserted, and by
    the readiness check to substitute programmes whose files are not available."""
    settings = all_settings(conn)
    tz = tz_of(conn)
    now = now or now_ts()
    from_ts = max(from_ts, now)
    day = broadcast_day_for(from_ts, settings, tz)
    if seed is None:
        seed = int(hashlib.sha256(f"{day.isoformat()}:{from_ts}".encode()).hexdigest()[:8], 16)
    builder = Builder(conn, now=now, seed=seed, exclude_media_ids=exclude_media_ids,
                      rebuild={channel_id: (from_ts, day_bounds(day, settings, tz)[2])})
    if exclude_media_ids:
        # Unavailable files must not survive as kept future slots either.
        with tx(conn):
            conn.execute("DELETE FROM schedule WHERE channel_id = ? AND start_ts >= ? AND replay = 0 AND media_id IN (%s)"
                         % ",".join("?" * len(exclude_media_ids)), (channel_id, from_ts, *exclude_media_ids))
    channel = next((c for c in builder.channels if c["id"] == channel_id), None)
    if channel is None:
        return {"status": "error", "summary": "channel not found or disabled"}
    slots = builder.build_channel_day(channel, day, force=True, from_ts=from_ts)
    builder.save(channel_id, day, slots)
    return {"status": "ok" if not builder.log else "warning",
            "summary": f"{sum(1 for s in slots if s.kind == 'programme' and not s.replay)} programmes",
            "notes": builder.log}
