"""Read Kodi style .nfo sidecar files (tvshow.nfo, <episode>.nfo, <movie>.nfo).

Only a handful of fields matter to a scheduler: year, certificate, genres, plot, and for
episodes the aired date. NFO files in the wild are often not well formed XML (URLs
appended, stray bytes), so parsing is defensive and returns partial results.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

_CERT_RE = re.compile(r"(?:UK|GB|Rated)?\s*:?\s*(U|PG|12A|12|15|18|R18)\b", re.IGNORECASE)
_KIDS_GENRES = {"animation", "children", "children's", "kids", "family", "cartoon"}


@dataclass
class NfoInfo:
    title: str | None = None
    year: int | None = None
    premiered: str | None = None
    certificate: str | None = None
    genres: list[str] = field(default_factory=list)
    plot: str | None = None
    aired: str | None = None
    season: int | None = None
    episode: int | None = None
    artist: str | None = None
    tags: list[str] = field(default_factory=list)
    runtime: int | None = None

    @property
    def kids(self) -> bool:
        return any(g.lower() in _KIDS_GENRES for g in self.genres)


def _text(root: ET.Element, tag: str) -> str | None:
    el = root.find(tag)
    if el is not None and el.text and el.text.strip():
        return el.text.strip()
    return None


def _int(value: str | None) -> int | None:
    if not value:
        return None
    m = re.search(r"\d{1,4}", value)
    return int(m.group()) if m else None


def normalise_certificate(raw: str | None) -> str | None:
    """Map 'UK:15', 'Rated PG', 'GB-12A', 'TV-14' etc. to a UK certificate."""
    if not raw:
        return None
    m = _CERT_RE.search(raw)
    if m:
        return m.group(1).upper()
    us = raw.upper()
    if "TV-Y" in us or us in ("G", "TV-G"):
        return "U"
    if "TV-PG" in us:
        return "PG"
    if "PG-13" in us or "TV-14" in us:
        return "12"
    if "TV-MA" in us or us.endswith("NC-17"):
        return "18"
    if us.endswith(" R") or us == "R":
        return "15"
    return None


def read_nfo(path: Path) -> NfoInfo | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    start = text.find("<")
    if start < 0:
        return None
    text = text[start:]
    # Kodi appends scraper URLs after the closing root tag; cut them off.
    for root_tag in ("</tvshow>", "</movie>", "</episodedetails>", "</musicvideo>"):
        idx = text.find(root_tag)
        if idx >= 0:
            text = text[: idx + len(root_tag)]
            break
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None

    info = NfoInfo()
    info.title = _text(root, "title")
    info.premiered = _text(root, "premiered") or _text(root, "aired")
    info.year = _int(_text(root, "year")) or (_int(info.premiered[:4]) if info.premiered else None)
    info.certificate = normalise_certificate(_text(root, "mpaa") or _text(root, "certification"))
    info.genres = [g.text.strip() for g in root.findall("genre") if g.text and g.text.strip()]
    info.plot = _text(root, "plot") or _text(root, "outline")
    info.aired = _text(root, "aired")
    info.season = _int(_text(root, "season"))
    info.episode = _int(_text(root, "episode"))
    info.artist = _text(root, "artist")
    info.tags = [t.text.strip() for t in root.findall("tag") if t.text and t.text.strip()]
    info.runtime = _int(_text(root, "runtime"))
    return info
