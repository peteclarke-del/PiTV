"""Bands: a stretch of a channel's day under one title, filled with several items.

A band says when it starts, how long it runs, and what may go in it: which kinds of item
(music videos, episodes, films), which genres and decades, and whether it wants one long
feature rather than a run of short items. The scheduler fills it while it builds the day and
the guide shows the band as a single programme, with whatever is playing inside it.

An hour of disco videos called "Disco Lunch", a Saturday cartoon morning and a double bill are
the same thing to PiTV: nothing here knows what music is. A channel's content label says what it
is for; the only rule that reads it is the line-up generator, which gives material carrying no
genre at all to the general channels. Nothing here depends on it."""

from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..db import as_bool, as_int, as_text, genre_list, rows_to_dicts
from .rules import hhmm_to_minutes, local_ts
from .slots import seconds

ITEM_MINUTES = 15             # default longest item a band treats as one of its own; see is_feature
KINDS = ("music", "episode", "movie")
MAX_BAND_MINUTES = 12 * 60
FEATURE_OVERRUN = 20 * 60     # how far past its band a feature may run when nothing shorter fits
MAX_STEPS = 3000              # items one stretch can hold; a runaway loop ends as a caption, not a hang
Placement = tuple[int, dict[str, Any]]   # (start ts, item) for the walk to turn into a slot


@dataclass(frozen=True)
class Band:
    id: int
    channel_id: int
    name: str
    start: str                 # HH:MM in the broadcast day
    minutes: int | None        # None runs to the next band, or the end of the day
    days: tuple[int, ...]      # weekdays it runs on (0 = Monday); empty = every day
    kinds: tuple[str, ...]
    genres: tuple[str, ...]
    decades: tuple[int, ...]
    feature: bool              # open with one long item, then fill the rest
    fetch: str = ""            # what to ask pitv_content for; empty follows the channel
    only_matching: bool | None = None  # None follows the channel; True: only labelled matches
    max_minutes: int | None = None     # longest item this band treats as one of its own
    last_fetch_at: int | None = None   # when material was last asked for (wanted.request_band_material)

    def on(self, weekday: int) -> bool:
        return not self.days or weekday in self.days

    def wants(self, item: dict[str, Any], strict: bool = True) -> bool:
        """Whether an item suits this band. Without `strict` the genres are ignored, which is
        how a band widens its search when nothing matches exactly."""
        if strict and self.genres and {g.lower() for g in self.genres}.isdisjoint(
                g.lower() for g in (item.get("genres") or [])):
            return False
        return self.dated(item) is not False

    def dated(self, item: dict[str, Any]) -> bool | None:
        """Whether the item falls in this band's decades: True, False, or None when its year is
        unknown. A "Sixties & Seventies" must never play something from 2004, so a known year
        outside the band's decades is refused at every step of the search; an unknown year is
        allowed only once the search has widened, when the alternative is dead air."""
        if not self.decades:
            return True
        year = item.get("year")
        if year is None:
            return None
        return (int(year) // 10) * 10 in self.decades


def is_feature(item: dict[str, Any], minutes: int = ITEM_MINUTES) -> bool:
    """A concert, a film or anything else too long to be one item among several.

    A band is a run of short things under one title, so the length that divides them is what the
    band says it wants (`fill.max_minutes`, else the channel's or the global setting). Anything
    at or above it, and anything the index calls a concert, can only appear as a band's opening
    feature."""
    return bool(item.get("concert")) or float(item.get("duration") or 0) >= minutes * 60


def clean(doc: Any) -> dict[str, Any]:
    """One band as the admin sends it, checked. Raises ValueError with the reason."""
    if not isinstance(doc, dict):
        raise ValueError("a band must be an object")   # noqa: TRY004 - the caller answers 400, not 500
    name = (as_text(doc.get("name")) or "").strip()
    start = (as_text(doc.get("start")) or "").strip()
    if not name:
        raise ValueError("a band needs a name; it is the title the guide shows")
    if not _is_hhmm(start):
        raise ValueError(f"band {name}: start must be HH:MM")
    minutes = as_int(doc.get("minutes"))
    if minutes is not None and not 1 <= minutes <= MAX_BAND_MINUTES:
        raise ValueError(f"band {name}: minutes must be between 1 and {MAX_BAND_MINUTES}, or empty")
    days = sorted({d for d in (as_int(x) for x in (doc.get("days") or [])) if d is not None and 0 <= d <= 6})
    fill = doc.get("fill") if isinstance(doc.get("fill"), dict) else {}
    kinds = [k for k in (as_text(x) for x in (fill.get("kinds") or [])) if k in KINDS]
    decades = sorted({d for d in (as_int(x) for x in (fill.get("decades") or [])) if d and 1900 <= d <= 2100})
    return {"name": name[:80], "start": start, "minutes": minutes, "days": days,
            "fill": {"kinds": kinds or ["music"], "genres": genre_list(fill.get("genres")),
                     "decades": decades, "feature": bool(as_bool(fill.get("feature"))),
                     "fetch": (as_text(fill.get("fetch")) or "")[:40],
                     "only_matching": _tri(fill.get("only_matching")),
                     "max_minutes": _item_minutes(fill.get("max_minutes"))},
            "enabled": bool(as_bool(doc.get("enabled"), True))}


def _tri(value: Any) -> bool | None:
    """A band's yes, no, or "as the channel says"."""
    return None if value in (None, "", "inherit") else bool(as_bool(value))


def _item_minutes(value: Any) -> int | None:
    """A band's own item length, or None to follow the channel and the settings."""
    minutes = as_int(value)
    return minutes if minutes is not None and 1 <= minutes <= MAX_BAND_MINUTES else None


def _is_hhmm(value: str) -> bool:
    parts = value.split(":")
    return len(parts) == 2 and all(p.isdigit() for p in parts) and int(parts[0]) < 24 and int(parts[1]) < 60


def _row_to_band(row: dict[str, Any]) -> Band:
    fill = row.get("fill")
    fill = json.loads(fill) if isinstance(fill, str) and fill else (fill or {})
    days = row.get("days")
    days = json.loads(days) if isinstance(days, str) and days else (days or [])
    return Band(id=row["id"], channel_id=row["channel_id"], name=row["name"], start=row["start"],
                minutes=as_int(row.get("minutes")), days=tuple(int(d) for d in days),
                kinds=tuple(k for k in (fill.get("kinds") or ["music"]) if k in KINDS) or ("music",),
                genres=tuple(genre_list(fill.get("genres"))),
                decades=tuple(int(d) for d in (fill.get("decades") or [])),
                feature=bool(fill.get("feature")), fetch=as_text(fill.get("fetch")) or "",
                only_matching=_tri(fill.get("only_matching")), max_minutes=as_int(fill.get("max_minutes")),
                last_fetch_at=as_int(row.get("last_fetch_at")))


def load(conn: sqlite3.Connection) -> dict[int, list[Band]]:
    """Every enabled band, by channel, in the order they run."""
    out: dict[int, list[Band]] = {}
    for row in rows_to_dicts(conn.execute("SELECT * FROM band WHERE enabled = 1 ORDER BY channel_id, start")):
        out.setdefault(row["channel_id"], []).append(_row_to_band(row))
    return out


def save(conn: sqlite3.Connection, channel_id: int, docs: list[Any], now: int) -> int:
    """Replace a channel's bands with `docs` (already cleaned). Returns how many were written."""
    conn.execute("DELETE FROM band WHERE channel_id = ?", (channel_id,))
    for doc in docs:
        conn.execute(
            "INSERT INTO band(channel_id, name, start, minutes, days, fill, enabled, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (channel_id, doc["name"], doc["start"], doc["minutes"], json.dumps(doc["days"]),
             json.dumps(doc["fill"]), int(doc["enabled"]), now))
    return len(docs)


def export(conn: sqlite3.Connection, channel_id: int) -> list[dict[str, Any]]:
    """A channel's bands as the admin edits them."""
    rows = rows_to_dicts(conn.execute("SELECT * FROM band WHERE channel_id = ? ORDER BY start", (channel_id,)))
    out = []
    for row in rows:
        b = _row_to_band(row)
        out.append({"name": b.name, "start": b.start, "minutes": b.minutes, "days": list(b.days),
                    "enabled": bool(row["enabled"]), "last_fetch_at": b.last_fetch_at,
                    "fill": {"kinds": list(b.kinds), "genres": list(b.genres), "decades": list(b.decades),
                             "feature": b.feature, "fetch": b.fetch, "only_matching": b.only_matching,
                             "max_minutes": b.max_minutes}})
    return out


class Filler:
    """Chooses what goes in a channel's bands for one day.

    Items a narrow band needs are held back from the broad bands earlier in the day, or a
    "Disco Lunch" finds its disco already played by the morning. A band widens its search step
    by step: its own genres and decades, then decades only, then anything of its kinds, and only
    then something already played today."""

    def __init__(self, bands: list[Band], pool: list[dict[str, Any]], *, item_repeat: int, feature_repeat: int,
                 rng: random.Random, last_placed: dict[int, int], item_minutes: int = ITEM_MINUTES,
                 strict: bool = False) -> None:
        self.pool = pool
        self.item_minutes = item_minutes
        self.strict = strict
        self.rng = rng
        self.last_placed = last_placed
        self.item_repeat = item_repeat
        self.feature_repeat = feature_repeat
        self.used_today: set[int] = set()
        self.played_today: dict[int, int] = {}
        self.last_id: int | None = None
        wanted = {b.id: {m["id"] for m in pool if b.wants(m)} for b in bands}
        claimed = {b.id: (wanted[b.id] if b.genres else set()) for b in bands}
        self.reserved = {b.id: set().union(*(c for i, c in claimed.items() if i != b.id), set()) - claimed[b.id]
                         for b in bands}

    def _suits(self, band: Band, m: dict[str, Any], feature: bool, genres: str, strict: bool) -> bool:
        """The part of the search that does not depend on the clock: length, kind of item,
        decades, labels and genre, at one step of the widening."""
        if is_feature(m, band.max_minutes or self.item_minutes) != feature:
            return False
        # A band billed as a concert is a concert slot, not a slot for any unusually long music
        # file: the index's classification is the authority, or an unclassified compilation
        # passes for a concert. The channel's own time has no such billing to live up to.
        if feature and band.feature and band.kinds == ("music",) and not m.get("concert"):
            return False
        dated = band.dated(m)
        if dated is False or (dated is None and (genres == "match" or strict)):
            return False      # the wrong decade, ever; an unknown year only once widened
        if strict and not (m.get("genres") and m.get("year")):
            return False      # this channel takes only what the index has labelled
        if band.genres:
            wanted = {g.lower() for g in band.genres}
            theirs = {str(g).lower() for g in (m.get("genres") or [])}
            if genres == "match" and wanted.isdisjoint(theirs):
                return False
            if genres == "unknown" and theirs and wanted.isdisjoint(theirs):
                return False    # tagged as something else: not this band's, unless nothing else is left
        return True

    def note(self, item: dict[str, Any], at: int) -> None:
        self.last_id = item["id"]
        self.used_today.add(item["id"])
        self.played_today[item["id"]] = self.played_today.get(item["id"], 0) + 1
        self.last_placed[item["id"]] = at

    def pick(self, band: Band, at: int, gap: float, feature: bool, fit: float | None = None,
             exclude: set[int] | None = None, fresh: bool = False) -> dict[str, Any] | None:
        """One item for a band, searched for in widening steps.

        The steps are (its genres and decades), (any genre it has no word on), (a genre it did
        not ask for), then the same three again allowing something played recently, and finally
        something already shown today. An item whose genre is known and is not the band's is
        held back until the last two steps: an untagged file might be disco, a file tagged metal
        is not, so a "Disco Lunch" takes the untagged one first.

        A channel (or a single band) set to take only labelled matches keeps the first step
        alone: the item must carry a genre and a year, both of which the band asked for. Such a
        band gives its time back when the library has nothing, and that shortfall is what asks
        pitv_content for more.

        `fit` is the stretch the caller would like filled exactly: an item that ends within ten
        minutes of it is strongly preferred, so long items are chosen to meet the next fixed
        point rather than leave a scrap that only the same few short files can plug. The item
        just played is never chosen again while there is any other, and nothing in `exclude`
        (what this airing of the band has already shown) is chosen at all. `fresh` stops the
        search before it allows anything played within the repeat gap."""
        repeat = self.feature_repeat if feature else self.item_repeat
        reserved = self.reserved.get(band.id, set())
        strict = self.strict if band.only_matching is None else band.only_matching
        steps = (("match", False, False), ("unknown", False, False), ("any", False, False),
                 ("match", True, False), ("unknown", True, False), ("any", True, True))
        if strict:
            # A strict band may repeat a correctly classified item as its final fallback; it may
            # never fill the remainder with an unknown or wrong genre merely to avoid a card.
            steps = (("match", False, False), ("match", True, False), ("match", True, True))
        if fresh:
            steps = tuple(step for step in steps if not step[1])
        for genres, allow_recent, allow_today in steps:
            candidates: list[tuple[float, dict[str, Any]]] = []
            for m in self.pool:
                if float(m["duration"]) > gap or (exclude and m["id"] in exclude):
                    continue
                if not self._suits(band, m, feature, genres, strict):
                    continue
                if m["id"] in self.used_today and not allow_today:
                    continue
                last = self.last_placed.get(m["id"])
                age = (at - last) if last is not None else None
                if age is not None and age < repeat and not allow_recent:
                    continue
                weight = 2.0 if age is None else min(2.0, max(0.02, age / repeat))
                if allow_today:
                    weight *= 1.0 / (1 + self.played_today.get(m["id"], 0)) ** 2
                elif m["id"] in reserved:
                    weight *= 0.1
                if fit is not None and abs(fit - float(m["duration"])) <= 600:
                    weight *= 8.0
                candidates.append((weight, m))
            if len(candidates) > 1:
                candidates = [c for c in candidates if c[1]["id"] != self.last_id]
            if candidates:
                return self.rng.choices(candidates, weights=[c[0] for c in candidates], k=1)[0][1]
        return None


# --- placing bands in a day --------------------------------------------------------------

def band_start(band: Band, day: date, day_start_min: int, tz: ZoneInfo) -> int:
    """A band's start time on this broadcast day. A time earlier than the day's own start
    belongs to the small hours at its end, so "00:30" on a day that opens at 08:00 is tomorrow
    morning, not twenty-four hours ago."""
    minute = hhmm_to_minutes(band.start)
    at = day + timedelta(days=1) if minute < day_start_min else day
    return local_ts(at, band.start, tz)


def timetable(todays: list[Band], day: date, day_start: int, day_end: int, next_day_start: int,
              day_start_min: int, tz: ZoneInfo) -> list[tuple[int, int, Band]]:
    """The bands of one day as (start, end, band), in order. A band without a length runs to
    the next one, or to closedown.

    A band that starts before closedown keeps the length it was given: a two hour band at
    23:30 runs two hours, into the small hours, rather than being cut to thirty minutes because
    midnight arrived. What follows it is the overnight, which simply starts later. This is the
    one place the timetable is worked out; what the scheduler places and what the top-up asks
    for both come from it."""
    todays = [b for b in todays if b.on(day.weekday())]
    starts = [band_start(b, day, day_start_min, tz) for b in todays]
    out: list[tuple[int, int, Band]] = []
    for i, band in enumerate(todays):
        start = starts[i]
        later = [s for s in starts[i + 1:] if s > start]
        end = start + band.minutes * 60 if band.minutes else (later[0] if later else day_end)
        if later:
            end = min(end, later[0])
        start, end = max(start, day_start), min(end, next_day_start)
        if start < day_end and end - start >= 60:
            out.append((start, end, band))
    return sorted(out, key=lambda x: x[0])


def fill_band(band: Band, start: int, end: int, filler: Filler, hard_end: int | None = None,
              overrun: int = 0, already: set[int] | None = None) -> tuple[list[Placement], int]:
    """What the library has for one band, and where that material ends.

    A band billed as a concert opens with one concert chosen to meet the band's length. A band
    of short items plays what it may, nothing twice in one airing: three songs are three songs,
    not the same three for two hours. When there is nothing more the material simply ends, and
    the caller holds the rest of the band's stretch under the band's own card; a band is never
    padded with something it did not ask for. The last item may run `overrun` seconds past the
    end, as programmes did on air, and the following band starts when it finishes."""
    placed: list[Placement] = []
    t = start
    want_feature = band.feature and not already    # a band carried on after a rebuild has had its feature
    for _ in range(MAX_STEPS):
        if t >= end:
            break
        opening_feature = want_feature
        gap = (hard_end - t) if hard_end is not None else (end - t + overrun)
        shown = {m["id"] for _, m in placed} | (already or set())
        # A band has a length and its feature is chosen to meet it: one that ends within a few
        # minutes of the band's end is strongly preferred, and only a channel with nothing that
        # close takes a longer one rather than open the band with no feature at all.
        item = filler.pick(band, t, gap, feature=opening_feature, exclude=shown,
                           fit=end - t if opening_feature else None)
        if item is None and opening_feature and hard_end is None:
            item = filler.pick(band, t, end - t + FEATURE_OVERRUN, feature=True, exclude=shown, fit=end - t)
        want_feature = False
        if item is None:
            break
        placed.append((t, item))
        filler.note(item, t)
        t += seconds(item)
        if opening_feature:
            break
    return placed, t


def fill_free(channel_id: int, kinds: tuple[str, ...], decades: tuple[int, ...], start: int, end: int,
              filler: Filler, overrun: int = 0) -> tuple[list[Placement], int]:
    """Time between bands on a channel that has no pattern: ordinary airtime for that channel,
    the same kinds of item held to the channel's own decades, belonging to no band.

    Short items are what bands are made of, and a band's stretch is where they belong under
    its name. Outside the bands, and through the small hours, a run of them is eight hours of
    three minute entries nobody chose, each played several times a night. So long items carry
    this time (concerts, films, sessions too long to be a band item), each chosen to end near
    the next fixed point, and short items plug what no long item fits. Anything not played
    within its repeat gap comes before anything that has been, long or short, so a channel
    with few long items is not reduced to the same concert all day. The last item may run
    `overrun` seconds past `end`, as a band's may."""
    free = Band(id=0, channel_id=channel_id, name="", start="", minutes=None, days=(),
                kinds=kinds or ("music",), genres=(), decades=decades, feature=False)
    placed: list[Placement] = []
    t = start
    for _ in range(MAX_STEPS):
        if t >= end:
            break
        gap = end - t + overrun
        item = (filler.pick(free, t, gap, feature=True, fit=end - t, fresh=True)
                or filler.pick(free, t, gap, feature=False, fresh=True)
                or filler.pick(free, t, gap, feature=True, fit=end - t)
                or filler.pick(free, t, gap, feature=False))
        if item is None:
            break
        placed.append((t, item))
        filler.note(item, t)
        t += seconds(item)
    return placed, t
