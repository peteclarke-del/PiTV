"""The broadcast clock: the timezone, wall-clock conversions and the shape of a broadcast day.

A leaf module. Everything else in the scheduler, the policy included, may import it; it imports
nothing of theirs, which is what keeps the rules free to read the policy without a cycle.

A broadcast day opens at `day_start` (08:00) and runs through the small hours of the next
calendar day, so a minute of day is only comparable once the hours after midnight are counted
on from the evening before them: `bday_minutes`.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from ..db import get_setting

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR
WEEK = 7 * DAY
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


def broadcast_ts(day: date, hhmm: str, day_start_min: int, tz: ZoneInfo) -> int:
    """The timestamp of a wall-clock time on a broadcast day. A time earlier than the day's own
    start belongs to the small hours at its end, so "00:30" on a day that opens at 08:00 is
    tomorrow morning, not twenty-four hours ago."""
    at = day + timedelta(days=1) if hhmm_to_minutes(hhmm) < day_start_min else day
    return local_ts(at, hhmm, tz)


def bday_minutes(minute: int, day_start_min: int) -> int:
    """A local minute of day on the broadcast day's clock: the hours before `day_start` belong to
    the end of the day before, so they count on from midnight (00:30 is 1470, after 23:59)."""
    return minute if minute >= day_start_min else minute + 1440
