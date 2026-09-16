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
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..db import (
    DEFAULT_SETTINGS,
    all_settings,
    effective,
    enabled_channels,
    now_ts,
    rows_to_dicts,
    run_log_finish,
    run_log_start,
    tx,
)
from ..lineup import nas_only_for
from . import bands
from .rules import (
    EraSpans,
    allowed_at,
    broadcast_day_for,
    day_bounds,
    daypart_end_minutes,
    daypart_for,
    dayparts_for_weekday,
    era_spans,
    era_weight_spans,
    hhmm_to_minutes,
    in_decades,
    is_kids,
    local_ts,
    minutes_of_day,
    parse_pattern,
    tz_of,
)

Progress = Callable[[str], None] | None
# Per channel, the window of unlocked slots a build is about to replace: (from_ts, to_ts) with
# to_ts None for "everything from from_ts on" (a forced horizon build).
Rebuild = dict[int, tuple[int, int | None]]

# Upper bound on walk steps for one channel-day. Only a pattern that can never place a
# programme (adverts or idents alone) gets near it; the rest of such a day becomes filler.
MAX_STEPS_PER_DAY = 3000
WEEK = 7 * 86400
FILLER_TITLE = "Programmes will continue shortly"
STAND_IN_IDENT_SECONDS = 10     # the shipped test signal's length (pitv/assets)
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
    wanted_id: int | None = None
    wanted_spec: dict[str, Any] | None = None   # external entry placed; a wanted row is created on save
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

    def advance(self, ts: int) -> None:
        """Move past the episode placed at `ts`; after the last one the series rests."""
        self.next_index += 1
        if self.next_index >= len(self.episodes):
            self.next_index = 0
            self.resting_until = ts + self.rest_weeks * WEEK


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
    """A broadcast day as written in the schedule and the API (YYYY-MM-DD)."""
    return date.fromisoformat(value)


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


def _seconds(item: dict[str, Any]) -> int:
    """Slot length for a file: whole seconds, never zero so a slot always has an end."""
    return max(1, round(float(item["duration"])))


def _cap(gap: int, room: int | None) -> int:
    """A gap limited to the room left before a kept slot (None: nothing follows)."""
    return gap if room is None else min(gap, room)


def _first_minutes(slots: list[Slot], tz: ZoneInfo) -> dict[int, int]:
    """show_id -> local minute of day of its first programme among `slots` (sorted by time)."""
    out: dict[int, int] = {}
    for s in slots:
        if s.show_id is not None and s.show_id not in out:
            out[s.show_id] = minutes_of_day(s.start_ts, tz)
    return out


class Builder:
    def __init__(self, conn: sqlite3.Connection, *, now: int | None = None,
                 seed: int | None = None, exclude_media_ids: set[int] | None = None,
                 rebuild: Rebuild | None = None, allow_external: bool = True,
                 only_media_ids: set[int] | None = None) -> None:
        self.conn = conn
        self.exclude_media_ids: set[int] = set(exclude_media_ids or ())
        # When set, only these files may be placed (readiness substitutes with what is playable now).
        self.only_media_ids = only_media_ids
        self.allow_external = allow_external
        self.rebuild: Rebuild = rebuild or {}
        self.settings = all_settings(conn)
        self.tz = tz_of(conn)
        self.now = now or now_ts()
        self.seed = seed if seed is not None else 0
        self.log: list[str] = []
        self.channels = enabled_channels(conn)
        # Minute of day the broadcast day starts (08:00 = 480); minutes before it belong to the
        # previous day and are counted past 1440 so comparisons stay monotonic.
        self.day_start_min = hhmm_to_minutes(self.settings.get("day_start", "08:00"))
        # Placeholders need external_lead_days counted from today's calendar date, not the
        # broadcast day: at 07:00 the fetch runs at 01:00 and 05:00 are already over.
        self._today = datetime.fromtimestamp(self.now, self.tz).date()
        # Parsed once: the candidate loops run tens of thousands of times per build.
        self._global_spans = era_spans(self.settings.get("era_weights"))
        self._advert_spans = era_spans(self.settings.get("advert_era_weights")
                                       or DEFAULT_SETTINGS["advert_era_weights"])
        self._pool_norm = float(self.settings.get("era_pool_normalise", 0.5))
        self._advert_window = int(self.settings.get("advert_year_window", 3))
        self._advert_penalty = int(self.settings.get("advert_repeat_penalty_hours", 6)) * 3600
        self._advert_pools: dict[int, list[tuple[dict[str, Any], float, float]]] = {}
        self._era_band_cache: dict[tuple[int | None, int | None], str] = {}
        self._channel_cols: dict[tuple[int, str], Any] = {}
        self._load_library()
        self._load_history()
        self.ad_last: dict[tuple[int, int], int] = {}  # (channel, media) -> ts
        # (channel, day) -> {show_id: minute it first aired}, for the same-slot bonus next day.
        self._day_minutes: dict[tuple[int, str], dict[int, int]] = {}
        # (channel, day) -> time from which save() replaces unlocked slots; None adds only.
        self._cuts: dict[tuple[int, str], int | None] = {}
        # (lineup_id, episode) -> wanted id, so every placement of one request in this build
        # shares a row (a film's later airings, a placeholder's overnight replay).
        self._requests: dict[tuple[int, int | None], int] = {}

    # --- loading -------------------------------------------------------------------

    def _allowed(self, media_id: int) -> bool:
        return media_id not in self.exclude_media_ids and (self.only_media_ids is None or media_id in self.only_media_ids)

    def _playable(self, kind: str, tail: str = "") -> list[dict[str, Any]]:
        """Every file of `kind` this build may place, with admin overrides applied."""
        rows = rows_to_dicts(self.conn.execute(
            "SELECT * FROM media WHERE kind = ? AND excluded = 0 AND missing = 0"
            " AND duration IS NOT NULL" + tail, (kind,)))
        return [effective(m) for m in rows if self._allowed(m["id"])]

    def _load_library(self) -> None:
        conn = self.conn
        self.shows: dict[int, Show] = {}
        show_rows = rows_to_dicts(conn.execute(
            "SELECT * FROM shows WHERE excluded = 0 AND missing = 0"))
        by_show: dict[int, list[dict[str, Any]]] = {}
        for e in self._playable("episode", " AND show_id IS NOT NULL"
                                " ORDER BY show_id, COALESCE(season, 999), COALESCE(episode, 999), path"):
            by_show.setdefault(e["show_id"], []).append(e)
        # Series owned by a transient external entry air only through the placeholders that
        # requested each episode; scheduling their cached files again would defeat "transient".
        transient_owned = {r[0] for r in conn.execute(
            "SELECT show_id FROM lineup WHERE source != 'library' AND transient = 1 AND show_id IS NOT NULL")}
        for row in show_rows:
            if row["id"] in transient_owned:
                continue
            s = effective(row)
            episodes = by_show.get(s["id"], [])
            if not episodes:
                continue
            for e in episodes:  # episodes inherit show-level metadata when they lack it
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
                id=s["id"], title=s["title"], year=s.get("year"),
                home_channel_id=s.get("home_channel_id"), mode=s.get("mode") or "auto",
                anchor_time=s.get("anchor_time"), anchor_days=[int(d) for d in days],
                rest_weeks=int(s.get("rest_weeks") or self.settings.get("series_rest_weeks", 4)),
                episodes=episodes, category=s.get("category") or "general",
                end_year=(s.get("year") + max((int(e.get("season") or 1) for e in episodes), default=1) - 1)
                if s.get("year") else None)
        self.movies = self._playable("movie")
        for m in self.movies:
            m["kids"] = is_kids(m)
        self._era_pools()
        self.music = self._playable("music")
        self.adverts = self._playable("advert")
        self.bands = bands.load(self.conn)
        self._pools: dict[str, list[dict[str, Any]]] = {"music": self.music}
        self._decades: dict[int, tuple[int, ...]] = {}
        self.idents = self._playable("ident")
        self._load_externals()
        # Line-ups are exclusive, so each channel's candidates are indexed once rather than
        # filtered out of the whole library for every gap. List order follows the load order,
        # which keeps a seeded build reproducible.
        self._free_shows: dict[int | None, list[Show]] = {}
        self._anchored: dict[int | None, list[Show]] = {}
        for show in self.shows.values():
            (self._anchored if show.anchored else self._free_shows).setdefault(show.home_channel_id, []).append(show)
        self._movies_on: dict[int | None, list[dict[str, Any]]] = {}
        for m in self.movies:
            self._movies_on.setdefault(m.get("home_channel_id"), []).append(m)
        self._externals_on: dict[int, list[dict[str, Any]]] = {}
        for e in self.externals:
            self._externals_on.setdefault(e["channel_id"], []).append(e)

    def _rebuilt(self, channel_id: int, ts: int) -> bool:
        """True when `ts` lies inside the window of `channel_id` this build replaces."""
        window = self.rebuild.get(channel_id)
        return window is not None and ts >= window[0] and (window[1] is None or ts < window[1])

    def _replaceable_request(self, users: list[sqlite3.Row]) -> bool:
        """True when no slot that will survive this build uses the request: every slot referring
        to it (`users`) is unlocked, still to come and inside a window being rebuilt."""
        return all(not sl["locked"] and sl["start_ts"] > self.now and self._rebuilt(sl["channel_id"], sl["start_ts"])
                   for sl in users)

    def _load_externals(self) -> None:
        """Line-up entries with no material on disk. Each becomes a candidate whose placement
        raises a wanted item for pitv_content. A request whose slots all fall inside the window
        being rebuilt is reused first, so a rebuild keeps the same episode numbers; new episodes
        continue from the highest request still standing, or from the entry's `next_episode`
        when the admin has set a starting point."""
        self.externals: list[dict[str, Any]] = []
        if not self.allow_external:
            return
        entries = rows_to_dicts(self.conn.execute(
            "SELECT * FROM lineup WHERE enabled = 1 AND source != 'library'"
            " AND (kind = 'show' OR media_id IS NULL)"))
        if not entries:
            return
        raised: dict[int, int] = {}                       # lineup_id -> highest episode requested
        standing: dict[int, list[sqlite3.Row]] = {}       # lineup_id -> requests still open
        for w in self.conn.execute("SELECT id, lineup_id, episode, status FROM wanted"
                                   " WHERE lineup_id IS NOT NULL ORDER BY lineup_id, episode, id"):
            raised[w["lineup_id"]] = max(raised.get(w["lineup_id"], 0), int(w["episode"] or 0))
            if w["status"] not in ("failed", "done"):
                standing.setdefault(w["lineup_id"], []).append(w)
        users: dict[int, list[sqlite3.Row]] = {}          # wanted id -> slots using it
        for sl in self.conn.execute("SELECT wanted_id, channel_id, start_ts, locked FROM schedule"
                                    " WHERE wanted_id IS NOT NULL"):
            users.setdefault(sl["wanted_id"], []).append(sl)
        default_minutes = int(self.settings.get("external_episode_minutes", 30))
        for e in entries:
            lineup_id = int(e["id"])
            e["lineup_id"] = lineup_id
            e["id"] = -lineup_id   # negative: never collides with a media id
            e["genres"] = _json_field(e.get("genres")) or []
            e["next_number"] = max(raised.get(lineup_id, 0), int(e.get("next_episode") or 1) - 1) + 1
            e["spare_wanted"] = [{"id": w["id"], "episode": w["episode"]} for w in standing.get(lineup_id, [])
                                 if self._replaceable_request(users.get(w["id"], []))]
            e["duration"] = float(e.get("episode_minutes") or default_minutes) * 60
            e["category"] = "general"
            e["kind"] = "episode" if e["kind"] == "show" else "movie"
            self.externals.append(e)

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
        if self._pool_norm <= 0:
            return 1.0
        n = self.era_pool.get((kind, self._era_band(year, end_year)), 1)
        return 1.0 / (max(1, n) ** self._pool_norm)

    def _bday(self, minute: int) -> int:
        """A local minute of day on the broadcast day's clock: +1440 after midnight."""
        return minute if minute >= self.day_start_min else minute + 1440

    def _bday_minutes(self, ts: int) -> int:
        return self._bday(minutes_of_day(ts, self.tz))

    def _load_history(self) -> None:
        """Last time each media item was placed (history or already-scheduled), and each
        show's latest placed episode so the cursor can continue from it.

        Slots inside a channel's rebuild window are about to be deleted, so they count for
        nothing; slots after the window on that channel (later days, already built) still
        count as repeats but not for the episode cursor, so the rebuilt day re-places the
        episodes it replaces rather than skipping past what later days already hold.

        Only the latest airing in history matters: it is never later than now, so it is the
        one nearest to any slot being built. Scheduled adverts and idents are skipped; their
        repeat rule is per build (`ad_last`)."""
        conn = self.conn
        self.last_placed: dict[int, int] = {}
        # film media_id -> its latest airing in history plus every scheduled one; the repeat
        # rule measures to the nearest in either direction.
        self.movie_placements: dict[int, list[int]] = {}
        films = {m["id"] for m in self.movies}
        cursor_ts: dict[int, int] = {}

        def placed(media_id: int, ts: int, for_cursor: bool = True) -> None:
            self.last_placed[media_id] = max(self.last_placed.get(media_id, 0), ts)
            if media_id in films:
                self.movie_placements.setdefault(media_id, []).append(ts)
            if for_cursor:
                cursor_ts[media_id] = max(cursor_ts.get(media_id, 0), ts)

        for r in conn.execute("SELECT media_id, MAX(started_at) AS ts FROM history"
                              " WHERE media_id IS NOT NULL GROUP BY media_id"):
            placed(r["media_id"], r["ts"])
        for r in conn.execute("SELECT media_id, start_ts AS ts, channel_id, locked FROM schedule"
                              " WHERE media_id IS NOT NULL AND replay = 0 AND kind = 'programme'"):
            window = self.rebuild.get(r["channel_id"])
            if r["locked"] or window is None or r["ts"] < window[0]:
                placed(r["media_id"], r["ts"])
            elif not self._rebuilt(r["channel_id"], r["ts"]):
                placed(r["media_id"], r["ts"], for_cursor=False)

        cursors = {r["show_id"]: r for r in conn.execute("SELECT * FROM show_cursor")}
        for show in self.shows.values():
            latest_ts, latest_idx = None, None
            for idx, ep in enumerate(show.episodes):
                ts = cursor_ts.get(ep["id"])
                if ts is not None and (latest_ts is None or ts > latest_ts):
                    latest_ts, latest_idx = ts, idx
            if latest_idx is not None:
                show.next_index = latest_idx
                show.advance(latest_ts)
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
                     subtitle=r["subtitle"], part=r["part"], replay=r["replay"], locked=r["locked"],
                     block=r["block"], wanted_id=r["wanted_id"], show_id=r["show_id"],
                     genres=_json_field(r["mgenres"]) or [], year=r["myear"])
                for r in rows]

    def _yesterday_minutes(self, channel_id: int, day: date) -> dict[int, int]:
        """show_id -> minute it first aired on this channel the day before."""
        key = (channel_id, (day - timedelta(days=1)).isoformat())
        if key not in self._day_minutes:
            self._day_minutes[key] = _first_minutes(
                [s for s in self._existing_slots(channel_id, key[1]) if s.kind == "programme"], self.tz)
        return self._day_minutes[key]

    # --- choosing ----------------------------------------------------------------------

    def _external_allowed(self, channel: dict[str, Any], t: int) -> bool:
        """Material not on disk may be placed only when NAS-only is off for the channel and the
        slot is far enough ahead for pitv_content to fetch it (external_lead_days)."""
        if nas_only_for(channel, self.settings):
            return False
        lead = int(self.settings.get("external_lead_days", 2))
        return (broadcast_day_for(t, self.settings, self.tz) - self._today).days >= lead

    def _choose_programme(self, channel: dict[str, Any], rng: random.Random, t: int, gap: int,
                          token: str, placed_today: dict[int, int], prev: Slot | None,
                          yesterday: dict[int, int], barred: set[int], relax: int = 0,
                          slack: int = 0) -> tuple[dict[str, Any], Show | None] | None:
        """Pick a programme for a gap. Selection is two-stage so the TV/movie balance follows the
        channel's kind weights rather than the size of each pool: choose the kind, then the item.

        relax=0 normal rules; relax=1 ignore daypart preferences and daily show limits;
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
        movie_repeat = int(self.settings.get("movie_repeat_days", 21)) * 86400
        same_slot_bonus = float(self.settings.get("same_slot_bonus", 3.0))
        genre_penalty = float(self.settings.get("genre_repeat_penalty", 0.4))
        daily_limit = int(self.settings.get("show_daily_limit", 2))
        repeat_penalty = float(self.settings.get("show_repeat_penalty", 0.3))
        max_minutes = dp.get("max_minutes")
        weekend = weekday_n >= 5
        relaxed = relax >= 1
        kids_rule = not channel.get("kids_any_time")
        kids_breakfast = weekend and bool(self.settings.get("weekend_kids_breakfast")) and dp.get("name") == "Breakfast"
        unknown_w = float(self.settings.get("unknown_year_weight", 0.0))
        prev_genres = ({g.lower() for g in prev.genres}
                       if prev is not None and prev.kind == "programme" and prev.genres else set())

        decades = self._channel_decades(channel)

        def common_weight(item: dict[str, Any], kind: str, end_year: int | None = None) -> float:
            if not in_decades(item.get("year"), decades, end_year):
                return 0.0
            w = era_weight_spans(item.get("year"), spans, end_year, unknown_w)
            if w <= 0:
                return 0.0
            duration = float(item["duration"])
            if duration > gap + slack:
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
                # Outside sport-friendly dayparts sport is admitted only at the final relaxation
                # step, after recently aired films: it is a last resort, not a filler.
                if sport_w < 0.5 and relax < 2:
                    return 0.0
                w *= sport_w if not relaxed else sport_w * 0.5
            w *= self._pool_factor(kind, item.get("year"), end_year)
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
            for show in self._free_shows.get(channel["id"], ()):
                if show.id in barred:
                    sport_ok = (show.category == "sport" and weekend
                                and self.settings.get("sport_back_to_back_weekends", True))
                    if not sport_ok:
                        continue  # never two episodes of the same series back to back
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
                y_min = yesterday.get(show.id)
                if y_min is not None:
                    w *= same_slot_bonus if abs(y_min - start_min) <= 30 else 0.6
                tv_cands.append((w, ep, show))
        if token in ("show", "movie"):
            for m in self._movies_on.get(channel["id"], ()):
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

        externals = self._externals_on.get(channel["id"])
        if externals and not relaxed and self._external_allowed(channel, t):
            ext_w = float(self.settings.get("external_weight", 0.7))
            for e in externals:
                if e["id"] in barred or (e.get("show_id") and e["show_id"] in barred):
                    continue
                episode = e["kind"] == "episode"
                if token not in ("show", "tv" if episode else "movie"):
                    continue
                if placed_today.get(e["id"], 0) >= (daily_limit if episode else 1):
                    continue
                w = common_weight(e, "tv" if episode else "movie") * ext_w
                if w <= 0:
                    continue
                (tv_cands if episode else movie_cands).append((w, e, None))

        kinds: list[tuple[float, list[tuple[float, dict[str, Any], Show | None]]]] = [
            (float(kind_weights.get(kind, 1.0)) * (1.0 if relaxed else float(dp.get(kind, 1.0))), cands)
            for kind, cands in (("tv", tv_cands), ("movie", movie_cands)) if cands]
        if not kinds:
            return None
        pool = rng.choices(kinds, weights=[max(k[0], 1e-6) for k in kinds], k=1)[0][1]
        pick = rng.choices(pool, weights=[c[0] for c in pool], k=1)[0]
        return pick[1], pick[2]

    def _advert_pool(self, channel: dict[str, Any]) -> list[tuple[dict[str, Any], float, float]]:
        """(advert, duration, era weight) for every advert the channel may carry, in library
        order. Built once per channel: `_choose_advert` runs for every break of every day."""
        pool = self._advert_pools.get(channel["id"])
        if pool is None:
            family_only = bool(channel.get("family_safe_ads")) or channel.get("content") == "cartoons"
            pool = []
            for ad in self.adverts:
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
        fits = [i for i in self.idents if float(i["duration"]) <= gap]
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
        if any(i.get("home_channel_id") in (None, channel["id"]) for i in self.idents):
            return None   # it has idents; none fitted this gap
        return {"id": None, "title": channel.get("short_name") or channel["name"], "duration": STAND_IN_IDENT_SECONDS}

    # --- building ----------------------------------------------------------------------

    def _anchors_for(self, channel: dict[str, Any], day: date, day_start: int, day_end: int) -> list[tuple[int, Show]]:
        out = []
        weekday = day.weekday()
        for show in self._anchored.get(channel["id"], ()):
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
            return [s for s in existing if s.locked or s.start_ts < cut]
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
            end = ts + _seconds(ep)
            if any(not (end <= fs or ts >= fe) for fs, fe, _ in fixed):
                self.log.append(f"{channel['name']} {day_str}: anchor {show.title} at {show.anchor_time} clashes with a kept slot")
                continue
            fixed.append((ts, end, ("anchor", show, ep)))
            placed_today[show.id] = placed_today.get(show.id, 0) + 1
        day_bands = self._bands_for(channel, day, day_start, day_end)
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
        rounding = int(self.settings.get("start_rounding_minutes", 5)) * 60
        tol = int(self.settings.get("duration_tolerance_minutes", 5)) * 60
        overrun = int(self.settings.get("end_of_day_overrun_minutes", 30)) * 60

        new_slots: list[Slot] = []
        all_slots: list[Slot] = list(keep)
        yesterday = self._yesterday_minutes(channel["id"], day)
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

        def fill_to(target: int, note: bool = False) -> None:
            """Close the gap up to `target` with adverts or idents, then filler, so the
            channel-day stays contiguous (the guide and the player both rely on that)."""
            nonlocal t
            t = self._pad(channel, rng, day_str, t, target, last_programme_year, emit)
            if t < target:
                if note and target - t > 120:
                    self.log.append(f"{channel['name']} {day_str}: filler {(target - t) // 60} min at {self._hhmm(t)}")
                emit(self._filler(channel, day_str, t, target))
                t = target

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
                    t = self._fill_band(channel, day_str, payload[1], max(t, fs), fe, filler, emit)
                    continue
                _, show, ep = payload
                start = max(t, fs)
                slot = self._programme_slot(channel, day_str, start, ep, show)
                emit(slot)
                show.advance(start)
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
                fill_to(boundary)      # a bands-only channel: pad whatever the bands leave
                continue
            token = pattern[pat_idx % len(pattern)]
            pat_idx += 1

            if token in ("ad", "break"):
                for _ in range(ads_per_break if token == "break" else 1):
                    ad = self._choose_advert(channel, rng, t, boundary - t, last_programme_year)
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
                # Tidy start time: pad with adverts up to the next rounding boundary.
                target = -(-t // rounding) * rounding
                if 0 < target - t <= 4 * 60 and target < boundary:
                    t = self._pad(channel, rng, day_str, t, target, last_programme_year, emit)
                    gap = boundary - t
            slack = 0
            if not fixed_queue:
                gap += overrun  # the last programme of the day may run past closedown
                slack = tol

            next_fixed = fixed_queue[0][2] if fixed_queue else None
            next_show_id = (next_fixed.show_id if isinstance(next_fixed, Slot)
                            else next_fixed[1].id if next_fixed else None)
            barred = {x for x in (last_show_id, next_show_id) if x}
            attempts = [(token, 0)] + ([("show", 0)] if token != "show" else []) + [("show", 1), ("show", 2)]
            choice = None
            for tok, relax in attempts:
                choice = self._choose_programme(channel, rng, t, gap, tok, placed_today, prev, yesterday,
                                                barred, relax=relax, slack=slack)
                if choice is not None:
                    break
            if choice is None:
                fill_to(boundary, note=True)   # nothing fits this gap
                continue
            item, show = choice
            if item["id"] < 0:   # external line-up entry: placeholder slot plus a wanted request
                slot = self._external_slot(channel, day_str, t, item)
                emit(slot)
                placed_today[item["id"]] = placed_today.get(item["id"], 0) + 1
                last_show_id = item["id"] if item["kind"] == "episode" else None
                last_programme_year = item.get("year")
                t = slot.end_ts
                continue
            slot = self._programme_slot(channel, day_str, t, item, show)
            emit(slot)
            if show is not None:
                show.advance(t)
                placed_today[show.id] = placed_today.get(show.id, 0) + 1
                last_show_id = show.id
            else:
                last_show_id = None
                self.movie_placements.setdefault(item["id"], []).append(t)
            self.last_placed[item["id"]] = t
            last_programme_year = item.get("year")
            t = slot.end_ts
            if show is not None:
                t = self._short_episode_run(channel, day_str, show, slot, gap - slot.duration, emit, placed_today)

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
        self._day_minutes[(channel["id"], day_str)] = _first_minutes(
            sorted((s for s in all_slots if s.kind == "programme"), key=lambda s: s.start_ts), self.tz)
        return new_slots + self._overnight(channel, day, day_end, next_day_start, all_slots)

    # --- bands ---------------------------------------------------------------------------

    def _channel_decades(self, channel: dict[str, Any]) -> tuple[int, ...]:
        """The decades a channel plays; empty means any. Held as JSON on the channel row."""
        cached = self._decades.get(channel["id"])
        if cached is None:
            raw = _json_field(channel.get("decades")) or []
            cached = self._decades[channel["id"]] = tuple(int(d) for d in raw if isinstance(d, (int, float, str))
                                                          and str(d).isdigit())
        return cached

    def _bands_for(self, channel: dict[str, Any], day: date, day_start: int, day_end: int
                   ) -> list[tuple[int, int, bands.Band]]:
        """This channel's bands for this day as (start, end, band). A band without a length runs
        to the next one, or to the end of the day."""
        todays = [b for b in self.bands.get(channel["id"], ()) if b.on(day.weekday())]
        out: list[tuple[int, int, bands.Band]] = []
        starts = [self._band_start(day, b.start) for b in todays]
        for i, band in enumerate(todays):
            start = starts[i]
            later = [s for s in starts[i + 1:] if s > start]
            end = start + band.minutes * 60 if band.minutes else (later[0] if later else day_end)
            if later:
                end = min(end, later[0])
            start, end = max(start, day_start), min(end, day_end)
            if end - start >= 60:
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
        pool = [m for kind in sorted(kinds) for m in self._band_pool(kind)
                if in_decades(m.get("year"), decades)]
        if not pool:
            self.log.append(f"{channel['name']} {day.isoformat()}: nothing in the library for its bands ({', '.join(sorted(kinds))})")
        # A channel may keep its own repeat gaps; empty follows the global settings.
        item_hours = channel.get("band_item_repeat_hours")
        feature_days = channel.get("band_feature_repeat_days")
        return bands.Filler(todays, pool, rng=rng, last_placed=self.last_placed,
                            item_repeat=int(item_hours if item_hours is not None
                                            else self.settings.get("band_item_repeat_hours", 36)) * 3600,
                            feature_repeat=int(feature_days if feature_days is not None
                                               else self.settings.get("band_feature_repeat_days", 14)) * 86400)

    def _band_pool(self, kind: str) -> list[dict[str, Any]]:
        """Every item of a kind a band may use, loaded once per build."""
        if kind not in self._pools:
            self._pools[kind] = self.music if kind == "music" else self._playable(kind)
        return self._pools[kind]

    def _fill_band(self, channel: dict[str, Any], day_str: str, band: bands.Band, start: int, end: int,
                   filler: bands.Filler | None, emit: Callable[[Slot], None]) -> int:
        """Fill one band with items under its name; the guide shows them as one programme."""
        t = start
        if filler is None:
            emit(self._filler(channel, day_str, t, end, title=band.name, block=band.name))
            return end
        want_feature = band.feature
        steps = 0
        while t < end and steps < MAX_STEPS_PER_DAY:
            steps += 1
            # A feature may run a little past its band rather than be dropped for being long.
            gap = (end - t + 20 * 60) if want_feature else (end - t)
            item = filler.pick(band, t, gap, feature=want_feature)
            if item is None and want_feature:
                item = filler.pick(band, t, end - t, feature=False)
            want_feature = False
            if item is None:
                emit(self._filler(channel, day_str, t, end, title=band.name, block=band.name))
                return end
            slot = self._programme_slot(channel, day_str, t, item, None, block=band.name)
            emit(slot)
            filler.note(item, t)
            t = slot.end_ts
        return t

    def _pad(self, channel: dict[str, Any], rng: random.Random, day_str: str, t: int, target: int,
             near_year: int | None, emit: Callable[[Slot], None]) -> int:
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
                        show: Show | None, block: str | None = None) -> Slot:
        title, subtitle = slot_titles(item, show.title if show else None)
        return Slot(channel_id=channel["id"], day=day_str, start_ts=start, end_ts=start + _seconds(item),
                    media_id=item["id"], offset=0, kind="programme", title=title, subtitle=subtitle,
                    show_id=show.id if show else None, genres=item.get("genres") or [],
                    year=item.get("year"), block=block)

    def _external_slot(self, channel: dict[str, Any], day_str: str, start: int, e: dict[str, Any]) -> Slot:
        """A programme that is not on disk yet. Episodes number on from the entry's counter; a
        wanted row raised by an earlier build for a slot since replaced is reused first."""
        duration = int(e["duration"])
        if e["kind"] == "episode":
            if e["spare_wanted"]:
                spare = e["spare_wanted"].pop(0)
                number, reuse = int(spare["episode"] or 1), int(spare["id"])
            else:
                number, reuse = e["next_number"], None
                e["next_number"] += 1
            subtitle = f"Episode {number}"
            spec = {"kind": "episode", "lineup_id": e["lineup_id"], "title": subtitle, "season": 1,
                    "episode": number, "year": e.get("year"), "reuse": reuse}
        else:
            # One request serves every airing of a film, so a spare is shared rather than used up.
            spare = e["spare_wanted"][0] if e["spare_wanted"] else None
            subtitle = f"({e['year']})" if e.get("year") else ""
            spec = {"kind": "movie", "lineup_id": e["lineup_id"], "title": e["title"], "season": None,
                    "episode": None, "year": e.get("year"), "reuse": int(spare["id"]) if spare else None}
        return Slot(channel_id=channel["id"], day=day_str, start_ts=start, end_ts=start + duration, media_id=None,
                    offset=0, kind="programme", title=e["title"], subtitle=subtitle, show_id=None,
                    genres=e.get("genres") or [], year=e.get("year"), wanted_spec=spec)

    def _next_day_slots(self, channel_id: int, next_day_start: int) -> list[Slot]:
        """Tomorrow's own slots, if tomorrow has been built: the overnight's last resort."""
        row = self.conn.execute("SELECT day FROM schedule WHERE channel_id = ? AND start_ts >= ? AND replay = 0"
                                " AND kind = 'programme' ORDER BY start_ts LIMIT 1",
                                (channel_id, next_day_start)).fetchone()
        return self._existing_slots(channel_id, row["day"]) if row else []

    def _short_episode_run(self, channel: dict[str, Any], day_str: str, show: Show, first: Slot, room: int,
                           emit: Callable[[Slot], None], placed_today: dict[int, int]) -> int:
        """Run several short episodes of one series together, and return where the run ends.

        A five minute cartoon on its own leaves the day in scraps and the guide unreadable, so
        episodes under `short_episode_minutes` are followed straight away by the next ones, in
        order, until the run reaches `short_episode_run_minutes`. They share the series title as
        their block, so the guide shows one entry, as it does for a band."""
        threshold = 60 * self._channel_minutes(channel, "short_episode_minutes")
        target = 60 * self._channel_minutes(channel, "short_episode_run_minutes")
        if not threshold or not target or first.duration >= threshold:
            return first.end_ts
        first.block = first.block or show.title
        t = first.end_ts
        while t - first.start_ts < target:
            episode = show.next_episode()
            if episode is None:
                break
            seconds = _seconds(episode)
            if seconds > room or seconds >= threshold:
                break
            slot = self._programme_slot(channel, day_str, t, episode, show, block=first.block)
            emit(slot)
            show.advance(t)
            placed_today[show.id] = placed_today.get(show.id, 0) + 1
            self.last_placed[episode["id"]] = t
            room -= seconds
            t = slot.end_ts
        return t

    def _channel_minutes(self, channel: dict[str, Any], key: str) -> int:
        """A channel's own value for a minutes setting, or the global one when it has none."""
        own = channel.get(key)
        return int(own if own is not None else self.settings.get(key, 0) or 0)

    def _media_slot(self, channel: dict[str, Any], day_str: str, start: int, item: dict[str, Any], kind: str) -> Slot:
        """An advert or ident slot; the subtitle is just the year."""
        return Slot(channel_id=channel["id"], day=day_str, start_ts=start, end_ts=start + _seconds(item),
                    media_id=item["id"], offset=0, kind=kind, title=item["title"],
                    subtitle=str(item.get("year") or ""), year=item.get("year"))

    def _overnight(self, channel: dict[str, Any], day: date, day_end: int, next_day_start: int,
                   day_slots: list[Slot]) -> list[Slot]:
        """00:00 to 08:00: replay the day from the channel's `overnight_replay_from`, looping a
        short day, without butting the same series against the day's end or tomorrow's start."""
        day_str = day.isoformat()
        replay_from = channel.get("overnight_replay_from") or self.settings.get("day_start", "08:00")
        from_day = day + timedelta(days=1) if hhmm_to_minutes(replay_from) < self.day_start_min else day
        from_ts = local_ts(from_day, replay_from, self.tz)
        ordered = sorted(day_slots, key=lambda s: s.start_ts)
        source = [s for s in ordered if s.start_ts >= from_ts and s.kind != "filler"]
        if not source:
            # A day with nothing to replay is a day built before the library had anything in it.
            # Showing a caption until morning is worse than opening tomorrow early, so the
            # overnight takes the next day's programmes when they are already built.
            source = self._next_day_slots(channel["id"], next_day_start)
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
                continue  # would run straight into the same series at 08:00
            end = min(t + s.duration, next_day_start)
            # A replayed placeholder keeps its request (wanted_id, or wanted_spec until save)
            # so the delivered file binds to the replay too.
            out.append(replace(s, day=day_str, start_ts=t, end_ts=end, replay=1, locked=0))
            t = end
        if t < next_day_start:
            out.append(self._filler(channel, day_str, t, next_day_start,
                                    title=f"Programmes will resume at {self.settings.get('day_start', '08:00')}",
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
                conn.execute("DELETE FROM schedule WHERE channel_id = ? AND day = ? AND locked = 0"
                             " AND start_ts >= ? AND replay = 0", (channel_id, day_str, cut))
            self._raise_wanted(slots)
            conn.executemany(
                "INSERT INTO schedule(channel_id, day, start_ts, end_ts, media_id, offset, kind, part,"
                " replay, locked, title, subtitle, block, wanted_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(s.channel_id, s.day, s.start_ts, s.end_ts, s.media_id, s.offset, s.kind, s.part,
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


def _seed_for(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)


def build_horizon(conn: sqlite3.Connection, *, start_day: date | None = None,
                  days: int | None = None, force: bool = False,
                  channel_numbers: list[int] | None = None, seed: int | None = None,
                  progress: Progress = None, now: int | None = None) -> dict[str, Any]:
    """Build `days` broadcast days from `start_day` for the enabled channels (or those
    numbered in `channel_numbers`). Without `force` only incomplete days are extended; with it
    everything unlocked from now on is rebuilt. Logged as a `schedule` run."""
    settings = all_settings(conn)
    tz = tz_of(conn)
    now = now or now_ts()
    if start_day is None:
        start_day = broadcast_day_for(now, settings, tz)
    days = days or int(settings.get("horizon_days", 7))
    if seed is None:
        seed = _seed_for(start_day.isoformat())
    run_id = run_log_start(conn, "schedule")
    channels = enabled_channels(conn)
    if channel_numbers:
        channels = [c for c in channels if c["number"] in channel_numbers]
    built = 0
    programmes = 0
    notes: list[str] = []
    try:
        builder = Builder(conn, now=now, seed=seed,
                          rebuild={c["id"]: (now, None) for c in channels} if force else None)
        notes = builder.log
        for i in range(days):
            day = start_day + timedelta(days=i)
            for channel in channels:
                slots = builder.build_channel_day(channel, day, force)
                if not slots:
                    continue
                builder.save(channel["id"], day, slots)
                n = sum(1 for s in slots if s.kind == "programme" and not s.replay)
                programmes += n
                built += 1
                if progress:
                    progress(f"{day} {channel['name']}: {n} programmes")
        withdraw_orphaned_requests(conn)
    except Exception as exc:
        # Days saved before the failure stand (each is its own transaction); the run log must
        # not be left showing a build still running.
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


def withdraw_orphaned_requests(conn: sqlite3.Connection) -> int:
    """Line-up requests that no slot uses any more (their slots were rebuilt away) are withdrawn,
    so pitv_content is never asked for material nothing will air and numbering cannot drift."""
    with tx(conn):
        cur = conn.execute("DELETE FROM wanted WHERE lineup_id IS NOT NULL AND status IN ('queued', 'failed')"
                           " AND id NOT IN (SELECT wanted_id FROM schedule WHERE wanted_id IS NOT NULL)")
    return cur.rowcount


def horizon_end(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT MAX(end_ts) AS e FROM schedule").fetchone()
    return row["e"] if row and row["e"] else None


def needs_rebuild(conn: sqlite3.Connection, now: int | None = None) -> bool:
    now = now or now_ts()
    end = horizon_end(conn)
    threshold = int(all_settings(conn).get("rebuild_when_days_left", 2)) * 86400
    return end is None or end - now < threshold


def refill_empty_days(conn: sqlite3.Connection, now: int | None = None) -> dict[str, Any]:
    """Build again the days that were built when there was nothing to schedule.

    A day built from an empty library is a full day of filler, which still counts as a built
    day: the horizon looks complete and nothing rebuilds it when the material finally arrives.
    So after a catalogue import, any channel-day with no programme left to come is built again
    from now on. Days that already carry programmes are left alone, so a viewer's evening does
    not reshuffle because a few adverts were indexed."""
    now = now or now_ts()
    rows = conn.execute(
        "SELECT channel_id, day, MIN(start_ts) AS first_ts,"
        " SUM(CASE WHEN kind = 'programme' THEN 1 ELSE 0 END) AS programmes"
        " FROM schedule WHERE end_ts > ? GROUP BY channel_id, day ORDER BY day", (now,)).fetchall()
    days = programmes = 0
    for row in rows:
        if row["programmes"]:
            continue
        result = rebuild_from(conn, row["channel_id"], max(int(row["first_ts"]), now), now=now)
        if result["status"] == "error":
            continue
        days += 1
        programmes += int(result["summary"].split(" ", 1)[0])
    summary = f"{days} empty channel-days rebuilt, {programmes} programmes"
    if days:
        log.info("refill: %s", summary)
    return {"status": "ok", "days": days, "programmes": programmes, "summary": summary}


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
    from_ts = max(from_ts, now)
    day = broadcast_day_for(from_ts, settings, tz)
    if seed is None:
        seed = _seed_for(f"{day.isoformat()}:{from_ts}")
    builder = Builder(conn, now=now, seed=seed, exclude_media_ids=exclude_media_ids, allow_external=allow_external,
                      only_media_ids=only_media_ids, rebuild={channel_id: (from_ts, day_bounds(day, settings, tz)[2])})
    channel = next((c for c in builder.channels if c["id"] == channel_id), None)
    if channel is None:
        return {"status": "error", "summary": "channel not found or disabled"}
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
    return {"status": "ok" if not builder.log else "warning",
            "summary": f"{sum(1 for s in slots if s.kind == 'programme' and not s.replay)} programmes",
            "notes": builder.log}
