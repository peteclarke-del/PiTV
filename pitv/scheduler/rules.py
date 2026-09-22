"""Time, era, certificate and daypart rules shared by the builder and the web API."""

from __future__ import annotations

import logging
import re
from typing import Any

from .. import genres as genre_rules
from .clock import (  # noqa: F401  (re-exported: the web API, the player and the tests import them here)
    bday_minutes,
    broadcast_day_for,
    day_bounds,
    hhmm_to_minutes,
    local_ts,
    minutes_of_day,
    tz_of,
)

_CERTIFICATE_ALIASES = {
    "U": "U", "UC": "U", "ALL": "U", "G": "U", "TV-Y": "U", "TV-Y7": "U", "TV-G": "U",
    "PG": "PG", "TV-PG": "PG", "APPROVED": "PG", "PASSED": "PG",
    "PG-13": "12", "TV-14": "12", "12": "12", "12A": "12A",
    "R": "15", "15": "15", "TV-MA": "18", "NC-17": "18", "18": "18",
}
# What a channel's pattern may say. `show` is a programme of any kind and `movie` insists on a
# film; there is no token for an episode, because that is what `show` already asks for on a
# channel whose kind weights favour television. One advert is `ad`, and a longer break is written
# as more of them, bounded by the channel's own limit. `ident` is the channel's own ident.
PATTERN_TOKENS = frozenset({"show", "movie", "ad", "ident"})
PROGRAMME_TOKENS = frozenset({"show", "movie"})   # a channel asking for one of these has a line-up
# Patterns written before the tokens were reduced. `tv` said "an episode rather than a film",
# which `show` and the kind weights already decide between, and `break` stood for a whole break,
# which is now written as the adverts it holds.
_TOKEN_WAS = {"tv": "show", "break": "ad"}
log = logging.getLogger("pitv.rules")


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
        return bday_minutes(minute, day_start)

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
    """A channel's pattern (``show, ident, ad, ad``) as tokens. A token that was retired is read
    as what replaced it, so a pattern saved by an older version still means what it said;
    anything else is dropped, and a pattern that says nothing is a plain run of programmes."""
    tokens = (t.strip().lower() for t in pattern.replace(";", ",").split(","))
    tokens = (_TOKEN_WAS.get(t, t) for t in tokens)
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
