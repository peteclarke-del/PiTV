"""Time, era, certificate and daypart rules shared by the builder and the web API."""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from .. import genres as genre_rules
from ..db import get_setting

_CERTIFICATE_ALIASES = {
    "U": "U", "UC": "U", "ALL": "U", "G": "U", "TV-Y": "U", "TV-Y7": "U", "TV-G": "U",
    "PG": "PG", "TV-PG": "PG", "APPROVED": "PG", "PASSED": "PG",
    "PG-13": "12", "TV-14": "12", "12": "12", "12A": "12A",
    "R": "15", "15": "15", "TV-MA": "18", "NC-17": "18", "18": "18",
}
PATTERN_TOKENS = frozenset({"show", "tv", "movie", "ad", "ident", "break"})
log = logging.getLogger("pitv.rules")


def tz_of(conn: sqlite3.Connection) -> ZoneInfo:
    """The configured broadcast timezone; every wall-clock rule is evaluated in it."""
    name = get_setting(conn, "timezone", "Europe/London")
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
        log.warning("unknown timezone %r; using Europe/London", name)
        return ZoneInfo("Europe/London")


@lru_cache(maxsize=1024)
def hhmm_to_minutes(value: str) -> int:
    """Cached: the scheduler asks this for the same handful of setting strings per candidate."""
    h, m = value.strip().split(":")
    return int(h) * 60 + int(m)


def local_ts(day: date, hhmm: str, tz: ZoneInfo) -> int:
    """UNIX timestamp of a local wall-clock time on a calendar day."""
    minutes = hhmm_to_minutes(hhmm)
    dt = datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(minutes=minutes)
    return int(dt.timestamp())


def minutes_of_day(ts: float, tz: ZoneInfo) -> int:
    dt = datetime.fromtimestamp(ts, tz)
    return dt.hour * 60 + dt.minute


def day_bounds(day: date, settings: dict[str, Any], tz: ZoneInfo) -> tuple[int, int, int]:
    """(day_start, day_end, next_day_start) timestamps for a broadcast day.

    With the defaults, a broadcast day runs 08:00 to 00:00 and the overnight replay fills
    00:00 to 08:00 of the following calendar day.
    """
    start_hhmm = settings.get("day_start", "08:00")
    end_hhmm = settings.get("day_end", "00:00")
    day_start = local_ts(day, start_hhmm, tz)
    end_minutes = hhmm_to_minutes(end_hhmm)
    if end_minutes <= hhmm_to_minutes(start_hhmm):
        day_end = local_ts(day + timedelta(days=1), end_hhmm, tz)
    else:
        day_end = local_ts(day, end_hhmm, tz)
    next_day_start = local_ts(day + timedelta(days=1), start_hhmm, tz)
    return day_start, day_end, next_day_start


def broadcast_day_for(ts: float, settings: dict[str, Any], tz: ZoneInfo) -> date:
    """The broadcast day a timestamp belongs to (early mornings belong to the previous day)."""
    dt = datetime.fromtimestamp(ts, tz)
    if dt.hour * 60 + dt.minute < hhmm_to_minutes(settings.get("day_start", "08:00")):
        return (dt - timedelta(days=1)).date()
    return dt.date()


EraSpans = tuple[tuple[int, int, float], ...]


def era_spans(era_weights: dict[str, Any] | None) -> EraSpans:
    """Parse {"1980-1989": 0.4, ...} once into (lo, hi, weight) triples; malformed keys are ignored."""
    out: list[tuple[int, int, float]] = []
    for span, weight in (era_weights or {}).items():
        try:
            lo, hi = (int(x) for x in str(span).split("-"))
            out.append((lo, hi, float(weight)))
        except (ValueError, TypeError):
            continue
    return tuple(out)


def era_weight_spans(year: int | None, spans: EraSpans, end_year: int | None = None,
                     unknown: float = 0.0) -> float:
    """Weight for an item made in `year`. A series running from `year` to `end_year` gets the
    best weight of any year in its run, so a 1978 show that ran into the 80s still counts.
    Items with no year get `unknown` (0 excludes them)."""
    if year is None:
        return float(unknown)
    best = 0.0
    for lo, hi, weight in spans:
        if lo <= year <= hi or (end_year is not None and year <= hi and end_year >= lo):
            best = max(best, weight)
    return best


def normalise_cert(cert: str | None) -> str | None:
    if not cert:
        return None
    # NFOs commonly use ``UK:PG`` or ``BBFC PG``; media managers also write a list with the
    # country on each (``US:R / US:Rated R``), and online sources often return US ratings. A
    # British entry is preferred wherever it stands in the list, then the first that reads.
    parts = [part.strip() for part in cert.upper().split("/") if part.strip()]
    parts.sort(key=lambda part: not re.match(r"(?:UK|GB|BBFC)\b", part))
    for part in parts:
        # A country is set off by a colon; the British forms also by a space, a slash or a dash.
        c = re.sub(r"^(?:(?:UK|GB|BBFC)\s*[:/-]?|[A-Z]{2}\s*:)\s*", "", part)
        for reading in (part, c, re.sub(r"^RATED\s+", "", c).strip()):
            if reading in _CERTIFICATE_ALIASES:
                return _CERTIFICATE_ALIASES[reading]
    return None


def effective_cert(item: dict[str, Any], settings: dict[str, Any]) -> str:
    cert = normalise_cert(item.get("certificate"))
    if cert:
        return cert
    if item.get("kind") == "movie":
        return settings.get("unknown_movie_certificate", "15")
    return settings.get("unknown_tv_certificate", "PG")


def cert_earliest_minutes(cert: str, settings: dict[str, Any], kind: str = "movie") -> int:
    """Earliest local minute a certificate may start. TV shows only obey the 15/18 rules:
    a 12-rated series was routinely repeated in the daytime on 1980s television."""
    key = "watershed" if kind == "movie" else "tv_watershed"
    watershed = settings.get(key) or {}
    hhmm = watershed.get(cert, "00:00")
    return hhmm_to_minutes(hhmm)


def is_kids(item: dict[str, Any]) -> bool:
    if item.get("kids"):
        return True
    return genre_rules.is_childrens(item.get("genres") or [])


def allowed_at(item: dict[str, Any], start_minutes: int, settings: dict[str, Any], kids_rule: bool = True) -> bool:
    """Certificate and kids rules for a programme starting at a given local minute of day.
    kids_rule=False (cartoon channels) lets children's programmes run all evening."""
    # The broadcast day wraps past midnight: times after 00:00 count as late night, for the
    # start and for a watershed or cutoff set after midnight alike. A watershed of 00:00
    # means no restriction.
    day_start = hhmm_to_minutes(settings.get("day_start", "08:00"))

    def late(minute: int) -> int:
        return minute if minute >= day_start else minute + 1440

    start = late(start_minutes)
    earliest = cert_earliest_minutes(effective_cert(item, settings), settings, item.get("kind") or "movie")
    if earliest and start < late(earliest):
        return False
    kids_cutoff = late(hhmm_to_minutes(settings.get("kids_cutoff", "21:00")))
    return not (kids_rule and is_kids(item) and item.get("kind") != "movie" and start >= kids_cutoff)


def daypart_for(start_minutes: int, dayparts: list[dict[str, Any]]) -> dict[str, Any]:
    """The daypart on air at a minute of day: the last one (in list order) already started."""
    current = dayparts[0] if dayparts else {"name": "Any", "tv": 1.0, "movie": 1.0, "kids": 1.0}
    for dp in dayparts:
        if start_minutes >= hhmm_to_minutes(dp["start"]):
            current = dp
    return current


def in_decades(year: int | None, decades: tuple[int, ...] | list[int], end_year: int | None = None,
               unknown_ok: bool = True) -> bool:
    """Whether something belongs to one of a channel's decades. No decades means any; a series
    that ran into a listed decade counts. An unknown year is allowed, as its era weight already
    decides how often it airs, unless the channel takes only what it knows the date of."""
    if not decades:
        return True
    if year is None:
        return unknown_ok
    last = end_year if end_year and end_year >= year else year
    return any((y // 10) * 10 in decades for y in range(year, last + 1))


def parse_pattern(pattern: str) -> list[str]:
    """A channel's pattern (``show, ad, ad``) as tokens; unknown tokens are dropped."""
    tokens = (t.strip().lower() for t in pattern.replace(";", ",").split(","))
    return [t for t in tokens if t in PATTERN_TOKENS] or ["show"]


def dayparts_for_weekday(weekday: int, settings: dict[str, Any], channel_profile: Any = None) -> list[dict[str, Any]]:
    """Daypart list for a weekday (0=Mon). A channel profile may be a plain list (weekdays only)
    or {"weekday": [...], "saturday": [...], "sunday": [...]}; missing parts fall back to global."""
    key = {5: "saturday", 6: "sunday"}.get(weekday, "weekday")
    if isinstance(channel_profile, dict):
        part = channel_profile.get(key)
        if part:
            return part
    elif isinstance(channel_profile, list) and channel_profile and key == "weekday":
        return channel_profile
    setting = {"weekday": "dayparts", "saturday": "dayparts_saturday", "sunday": "dayparts_sunday"}[key]
    return settings.get(setting) or settings.get("dayparts") or []


def daypart_end_minutes(start_minutes: int, dayparts: list[dict[str, Any]], day_end_minutes: int = 1440) -> int:
    """Minute of day at which the daypart containing start_minutes ends."""
    starts = (hhmm_to_minutes(dp["start"]) for dp in dayparts)
    return min((m for m in starts if m > start_minutes), default=day_end_minutes)
