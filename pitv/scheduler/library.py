"""What a build has to schedule, and what has aired of it already.

One `Library` is loaded per build: every usable series, film, music video, advert and ident
with the admin's overrides applied, the line-up entries with nothing on disk yet (externals),
each channel's bands, and the history that every repeat rule and episode cursor reads. The
Builder walks days; this is the shelf it takes things from and the ledger of what it has
taken. Nothing here chooses anything.

Loading it is the expensive part of a build, so it is loaded once and indexed per channel:
line-ups are exclusive, so each channel's candidates are found once rather than filtered out
of the whole library for every gap."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..db import LIVE, effective, rows_to_dicts
from . import bands
from .policy import SchedulerPolicy
from .rules import era_spans, era_weight_spans, is_kids
from .slots import Show, json_field

# Per channel, the window of unlocked slots a build is about to replace: (from_ts, to_ts) with
# to_ts None for "everything from from_ts on" (a forced horizon build).
Rebuild = dict[int, tuple[int, int | None]]

# The rows a build may place: live, with a known length so the slot has an end.
USABLE = f"{LIVE} AND duration IS NOT NULL"


class Library:
    def __init__(self, conn: sqlite3.Connection, policy: SchedulerPolicy, *, now: int,
                 rebuild: Rebuild | None = None, exclude_media_ids: set[int] | None = None,
                 only_media_ids: set[int] | None = None, allow_external: bool = True) -> None:
        self.conn = conn
        self.policy = policy
        self.now = now
        self.rebuild: Rebuild = rebuild or {}
        self.exclude_media_ids: set[int] = set(exclude_media_ids or ())
        # When set, only these files may be placed (readiness substitutes with what is playable now).
        self.only_media_ids = only_media_ids
        self.allow_external = allow_external
        # Parsed once: the candidate loops run tens of thousands of times per build.
        self._global_spans = era_spans(policy.value("era_weights"))
        self._pool_norm = policy.number("era_pool_normalise")
        self._era_band_cache: dict[tuple[int | None, int | None], str] = {}
        self._load_library()
        self._load_history()
        self.started_channels = {int(r[0]) for r in conn.execute(
            "SELECT DISTINCT channel_id FROM history UNION SELECT DISTINCT channel_id FROM schedule"
            " WHERE kind='programme' AND start_ts < ?", (now,))}
        # What this build has placed so far, which the rest of the build must respect.
        self.ad_last: dict[tuple[int, int], int] = {}          # (channel, media) -> ts
        # A short remote series is requested as a bundle; its last-resort repeat re-airs the
        # bundle already requested rather than one five-minute episode and an advert break.
        self.external_short_runs: dict[int, list[dict[str, Any]]] = {}

    # --- loading -------------------------------------------------------------------

    def allowed(self, media_id: int) -> bool:
        return media_id not in self.exclude_media_ids and (self.only_media_ids is None or media_id in self.only_media_ids)

    def playable(self, kind: str, tail: str = "") -> list[dict[str, Any]]:
        """Every file of `kind` this build may place, with admin overrides applied."""
        rows = rows_to_dicts(self.conn.execute(
            f"SELECT * FROM media WHERE kind = ? AND {USABLE}" + tail, (kind,)))
        return [effective(m) for m in rows if self.allowed(m["id"])]

    def _load_library(self) -> None:
        conn = self.conn
        self.shows: dict[int, Show] = {}
        pinned_shows = {r[0] for r in conn.execute(
            "SELECT show_id FROM lineup WHERE pinned = 1 AND enabled = 1 AND show_id IS NOT NULL")}
        pinned_movies = {r[0] for r in conn.execute(
            "SELECT media_id FROM lineup WHERE pinned = 1 AND enabled = 1 AND media_id IS NOT NULL")}
        show_rows = rows_to_dicts(conn.execute(
            f"SELECT * FROM shows WHERE {LIVE}"))
        by_show: dict[int, list[dict[str, Any]]] = {}
        for e in self.playable("episode", " AND show_id IS NOT NULL"
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
                e["lineup_pinned"] = s["id"] in pinned_shows
            days = json_field(s.get("anchor_days"))
            if not days:
                days = [0, 1, 2, 3, 4] if s.get("mode") == "strip" else [0]
            self.shows[s["id"]] = Show(
                id=s["id"], title=s["title"], year=s.get("year"),
                home_channel_id=s.get("home_channel_id"), mode=s.get("mode") or "auto",
                anchor_time=s.get("anchor_time"), anchor_days=[int(d) for d in days],
                rest_weeks=int(s.get("rest_weeks") or self.policy.integer("series_rest_weeks")),
                episodes=episodes, category=s.get("category") or "general",
                pinned=s["id"] in pinned_shows,
                end_year=(s.get("year") + max((int(e.get("season") or 1) for e in episodes), default=1) - 1)
                if s.get("year") else None)
        self.movies = self.playable("movie")
        for m in self.movies:
            m["kids"] = is_kids(m)
            m["lineup_pinned"] = m["id"] in pinned_movies
        self._era_pools()
        self.music = self.playable("music")
        self.adverts = self.playable("advert")
        self.bands = bands.load(self.conn)
        self._pools: dict[str, list[dict[str, Any]]] = {"music": self.music}
        self.idents = self.playable("ident")
        self._load_externals()
        # Line-ups are exclusive, so each channel's candidates are indexed once rather than
        # filtered out of the whole library for every gap. List order follows the load order,
        # which keeps a seeded build reproducible.
        self.free_shows: dict[int | None, list[Show]] = {}
        self.anchored: dict[int | None, list[Show]] = {}
        for show in self.shows.values():
            (self.anchored if show.anchored else self.free_shows).setdefault(show.home_channel_id, []).append(show)
        self.movies_on: dict[int | None, list[dict[str, Any]]] = {}
        for m in self.movies:
            self.movies_on.setdefault(m.get("home_channel_id"), []).append(m)
        self.externals_on: dict[int, list[dict[str, Any]]] = {}
        for e in self.externals:
            self.externals_on.setdefault(e["channel_id"], []).append(e)

    def band_pool(self, kind: str) -> list[dict[str, Any]]:
        """Every item of a kind a band may use, loaded once per build."""
        if kind not in self._pools:
            self._pools[kind] = self.playable(kind)
        return self._pools[kind]

    def rebuilt(self, channel_id: int, ts: int) -> bool:
        """True when `ts` lies inside the window of `channel_id` this build replaces."""
        window = self.rebuild.get(channel_id)
        return window is not None and ts >= window[0] and (window[1] is None or ts < window[1])

    def _replaceable_request(self, users: list[sqlite3.Row]) -> bool:
        """True when no slot that will survive this build uses the request: every slot referring
        to it (`users`) is unlocked, still to come and inside a window being rebuilt."""
        return all(not sl["locked"] and sl["start_ts"] > self.now and self.rebuilt(sl["channel_id"], sl["start_ts"])
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
        default_minutes = self.policy.integer("external_episode_minutes")
        for e in entries:
            lineup_id = int(e["id"])
            e["lineup_id"] = lineup_id
            e["id"] = -lineup_id   # negative: never collides with a media id
            e["genres"] = json_field(e.get("genres")) or []
            e["kids"] = is_kids(e)
            e["next_number"] = max(raised.get(lineup_id, 0), int(e.get("next_episode") or 1) - 1) + 1
            open_requests = standing.get(lineup_id, [])
            e["spare_wanted"] = [{"id": w["id"], "episode": w["episode"]} for w in open_requests
                                 if self._replaceable_request(users.get(w["id"], []))]
            if e["kind"] == "show" and open_requests:
                latest = max(open_requests, key=lambda w: (int(w["episode"] or 0), int(w["id"])))
                number = int(latest["episode"] or 1)
                e["last_spec"] = {"kind": "episode", "lineup_id": lineup_id,
                                  "title": f"Episode {number}", "season": 1, "episode": number,
                                  "year": e.get("year"), "reuse": int(latest["id"])}
            e["duration"] = float(e.get("episode_minutes") or default_minutes) * 60
            e["category"] = "sport" if any(str(g).casefold() == "sport" for g in e["genres"]) else "general"
            e["kind"] = "episode" if e["kind"] == "show" else "movie"
            self.externals.append(e)

    # --- eras ---------------------------------------------------------------------------

    def era_band(self, year: int | None, end_year: int | None = None) -> str:
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
            k = ("tv", self.era_band(show.year, show.end_year))
            self.era_pool[k] = self.era_pool.get(k, 0) + 1
        for m in self.movies:
            k = ("movie", self.era_band(m.get("year")))
            self.era_pool[k] = self.era_pool.get(k, 0) + 1

    def pool_factor(self, kind: str, year: int | None, end_year: int | None = None) -> float:
        """Divide an item's weight by (pool size ** normalise) so eras with few titles are not
        drowned out by eras with many; normalise=1 makes airtime follow the era weights exactly."""
        if self._pool_norm <= 0:
            return 1.0
        n = self.era_pool.get((kind, self.era_band(year, end_year)), 1)
        return 1.0 / (max(1, n) ** self._pool_norm)

    # --- what has aired ----------------------------------------------------------------

    def _load_history(self) -> None:
        """Last time each media item was placed (history or already-scheduled), and each
        show's latest placed episode so the cursor can continue from it.

        Slots inside a channel's rebuild window are about to be deleted, so they count for
        nothing; slots after the window on that channel (later days, already built) still
        count as repeats but not for the episode cursor, so the rebuilt day re-places the
        episodes it replaces rather than skipping past what later days already hold.

        Only the latest airing in history matters: it is never later than now, so it is the
        one nearest to any slot being built. Scheduled adverts and idents are skipped; their
        repeat rule is per build (the Builder's `ad_last`)."""
        conn = self.conn
        self.last_placed: dict[int, int] = {}
        # film media_id -> its latest airing in history plus every scheduled one; the repeat
        # rule measures to the nearest in either direction.
        self.movie_placements: dict[int, list[int]] = {}
        self.show_last_placed: dict[int, int] = {}
        self.external_last_placed: dict[int, int] = {}
        films = {m["id"] for m in self.movies}
        media_show = {ep["id"]: show.id for show in self.shows.values() for ep in show.episodes}
        cursor_ts: dict[int, int] = {}

        def placed(media_id: int, ts: int, for_cursor: bool = True) -> None:
            self.last_placed[media_id] = max(self.last_placed.get(media_id, 0), ts)
            if media_id in films:
                self.movie_placements.setdefault(media_id, []).append(ts)
            show_id = media_show.get(media_id)
            if show_id is not None:
                self.show_last_placed[show_id] = max(self.show_last_placed.get(show_id, 0), ts)
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
            elif not self.rebuilt(r["channel_id"], r["ts"]):
                placed(r["media_id"], r["ts"], for_cursor=False)

        for r in conn.execute("SELECT w.lineup_id, s.start_ts, s.channel_id, s.locked FROM schedule s"
                              " JOIN wanted w ON w.id = s.wanted_id WHERE w.lineup_id IS NOT NULL"
                              " AND s.replay = 0"):
            window = self.rebuild.get(r["channel_id"])
            survives = r["locked"] or window is None or r["start_ts"] < window[0] or not self.rebuilt(r["channel_id"], r["start_ts"])
            if survives:
                lid = int(r["lineup_id"])
                self.external_last_placed[lid] = max(self.external_last_placed.get(lid, 0), r["start_ts"])

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
