"""Bands: a stretch of a channel's day under one title, filled with several items.

A band says when it starts, how long it runs, and what may go in it: which kinds of item
(music videos, episodes, films), which genres and decades, and whether it wants one long
feature rather than a run of short items. The scheduler fills it while it builds the day and
the guide shows the band as a single programme, with whatever is playing inside it.

An hour of disco videos called "Disco Lunch", a Saturday cartoon morning and a double bill are
the same thing to PiTV: nothing here knows what music is. Channels keep a content label for
people to read, but no part of the build depends on it."""

from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..db import as_bool, as_int, as_text, genre_list, rows_to_dicts

FEATURE_MINUTES = 35          # an item at least this long counts as a feature (a concert, a film)
KINDS = ("music", "episode", "movie")
MAX_BAND_MINUTES = 12 * 60


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
    last_fetch_at: int | None = None   # when material was last asked for (wanted.request_band_material)

    def on(self, weekday: int) -> bool:
        return not self.days or weekday in self.days

    def wants(self, item: dict[str, Any], strict: bool = True) -> bool:
        """Whether an item suits this band. Without `strict` the genres are ignored, which is
        how a band widens its search when nothing matches exactly."""
        if strict and self.genres and {g.lower() for g in self.genres}.isdisjoint(
                g.lower() for g in (item.get("genres") or [])):
            return False
        if self.decades:
            year = item.get("year")
            return year is not None and (year // 10) * 10 in self.decades
        return True


def is_feature(item: dict[str, Any]) -> bool:
    """A concert, a film or anything else long enough to stand on its own in a band."""
    return bool(item.get("concert")) or float(item.get("duration") or 0) >= FEATURE_MINUTES * 60


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
            "fill": {"kinds": kinds or ["music"], "genres": [g.lower() for g in genre_list(fill.get("genres"))],
                     "decades": decades, "feature": bool(as_bool(fill.get("feature"))),
                     "fetch": (as_text(fill.get("fetch")) or "")[:40]},
            "enabled": bool(as_bool(doc.get("enabled"), True))}


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
                genres=tuple(str(g).lower() for g in (fill.get("genres") or [])),
                decades=tuple(int(d) for d in (fill.get("decades") or [])),
                feature=bool(fill.get("feature")), fetch=as_text(fill.get("fetch")) or "",
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
                    "enabled": bool(row["enabled"]),
                    "fill": {"kinds": list(b.kinds), "genres": list(b.genres), "decades": list(b.decades),
                             "feature": b.feature, "fetch": b.fetch}})
    return out


class Filler:
    """Chooses what goes in a channel's bands for one day.

    Items a narrow band needs are held back from the broad bands earlier in the day, or a
    "Disco Lunch" finds its disco already played by the morning. A band widens its search step
    by step: its own genres and decades, then decades only, then anything of its kinds, and only
    then something already played today."""

    def __init__(self, bands: list[Band], pool: list[dict[str, Any]], *, item_repeat: int, feature_repeat: int,
                 rng: random.Random, last_placed: dict[int, int]) -> None:
        self.pool = pool
        self.rng = rng
        self.last_placed = last_placed
        self.item_repeat = item_repeat
        self.feature_repeat = feature_repeat
        self.used_today: set[int] = set()
        self.played_today: dict[int, int] = {}
        wanted = {b.id: {m["id"] for m in pool if b.wants(m)} for b in bands}
        claimed = {b.id: (wanted[b.id] if b.genres else set()) for b in bands}
        self.reserved = {b.id: set().union(*(c for i, c in claimed.items() if i != b.id), set()) - claimed[b.id]
                         for b in bands}

    def note(self, item: dict[str, Any], at: int) -> None:
        self.used_today.add(item["id"])
        self.played_today[item["id"]] = self.played_today.get(item["id"], 0) + 1
        self.last_placed[item["id"]] = at

    def pick(self, band: Band, at: int, gap: float, feature: bool) -> dict[str, Any] | None:
        repeat = self.feature_repeat if feature else self.item_repeat
        reserved = self.reserved.get(band.id, set())
        for strict, decades_only, allow_recent, allow_today in (
                (True, False, False, False), (False, True, False, False), (False, False, False, False),
                (True, False, True, False), (False, False, True, False), (False, False, True, True)):
            candidates: list[tuple[float, dict[str, Any]]] = []
            for m in self.pool:
                if is_feature(m) != feature or float(m["duration"]) > gap:
                    continue
                if m["id"] in self.used_today and not allow_today:
                    continue
                if (strict or decades_only) and not band.wants(m, strict=strict):
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
                candidates.append((weight, m))
            if candidates:
                return self.rng.choices(candidates, weights=[c[0] for c in candidates], k=1)[0][1]
        return None
