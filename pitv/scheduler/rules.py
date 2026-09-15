"""Time, era, certificate and daypart rules shared by the builder and the web API."""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..db import KIDS_GENRES, get_setting

CERT_ORDER = ["U", "PG", "12", "12A", "15", "18"]
log = logging.getLogger("pitv.rules")


def tz_of(conn: sqlite3.Connection) -> ZoneInfo:
    name = get_setting(conn, "timezone", "Europe/London")
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
        log.warning("unknown timezone %r; using Europe/London", name)
        return ZoneInfo("Europe/London")


def hhmm_to_minutes(value: str) -> int:
    h, m = value.strip().split(":")
    return int(h) * 60 + int(m)


def minutes_to_hhmm(minutes: int) -> str:
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def local_dt(ts: int | float, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ts, tz)


def local_ts(day: date, hhmm: str, tz: ZoneInfo) -> int:
    """UNIX timestamp of a local wall-clock time on a calendar day."""
    minutes = hhmm_to_minutes(hhmm)
    dt = datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(minutes=minutes)
    return int(dt.timestamp())


def minutes_of_day(ts: int | float, tz: ZoneInfo) -> int:
    dt = local_dt(ts, tz)
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


def broadcast_day_for(ts: int | float, settings: dict[str, Any], tz: ZoneInfo) -> date:
    """The broadcast day a timestamp belongs to (early mornings belong to the previous day)."""
    dt = local_dt(ts, tz)
    if dt.hour * 60 + dt.minute < hhmm_to_minutes(settings.get("day_start", "08:00")):
        return (dt - timedelta(days=1)).date()
    return dt.date()


def era_weight(year: int | None, era_weights: dict[str, float], end_year: int | None = None,
               unknown: float = 0.0) -> float:
    """Weight for an item made in `year`. A series running from `year` to `end_year` gets the
    best weight of any year in its run, so a 1978 show that ran into the 80s still counts.
    Items with no year get `unknown` (0 excludes them)."""
    if year is None:
        return float(unknown)
    best = 0.0
    for span, weight in era_weights.items():
        try:
            lo, hi = (int(x) for x in span.split("-"))
        except ValueError:
            continue
        if lo <= year <= hi or (end_year is not None and year <= hi and end_year >= lo):
            best = max(best, float(weight))
    return best


def normalise_cert(cert: str | None) -> str | None:
    if not cert:
        return None
    c = cert.strip().upper()
    return c if c in CERT_ORDER else None


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
    genres = item.get("genres") or []
    return any(str(g).lower() in KIDS_GENRES for g in genres)


def allowed_at(item: dict[str, Any], start_minutes: int, settings: dict[str, Any], kids_rule: bool = True) -> bool:
    """Certificate and kids rules for a programme starting at a given local minute of day.
    kids_rule=False (cartoon channels) lets children's programmes run all evening."""
    cert = effective_cert(item, settings)
    earliest = cert_earliest_minutes(cert, settings, item.get("kind") or "movie")
    # The broadcast day wraps past midnight; a start after 00:00 counts as late night.
    start = start_minutes if start_minutes >= hhmm_to_minutes(settings.get("day_start", "08:00")) else start_minutes + 1440
    if earliest and start < earliest:
        return False
    if kids_rule and is_kids(item) and item.get("kind") != "movie":
        cutoff = hhmm_to_minutes(settings.get("kids_cutoff", "21:00"))
        if start >= cutoff:
            return False
    return True


def daypart_for(start_minutes: int, dayparts: list[dict[str, Any]]) -> dict[str, Any]:
    current = dayparts[0] if dayparts else {"name": "Any", "tv": 1.0, "movie": 1.0, "kids": 1.0}
    for dp in dayparts:
        if start_minutes >= hhmm_to_minutes(dp["start"]):
            current = dp
    return current


def parse_pattern(pattern: str) -> list[str]:
    tokens = [t.strip().lower() for t in pattern.replace(";", ",").split(",")]
    valid = {"show", "tv", "movie", "ad", "ident", "break"}
    out = [t for t in tokens if t in valid]
    return out or ["show"]


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
    ends = sorted(hhmm_to_minutes(dp["start"]) for dp in dayparts if hhmm_to_minutes(dp["start"]) > start_minutes)
    return ends[0] if ends else day_end_minutes
