"""Parse titles, years, seasons and episodes out of Kodi/Plex style file and folder names.

The parsers are deliberately tolerant: the library was not built for this project, so a
best effort on every common convention beats strictness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePath

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mpg", ".mpeg", ".ts", ".m2ts", ".mov", ".wmv",
    ".flv", ".webm", ".vob", ".divx", ".ogm",
}

_YEAR = r"(?:19[3-9]\d|20[0-4]\d)"
_TITLE_YEAR_PAREN = re.compile(rf"^(?P<title>.+?)\s*[\(\[](?P<year>{_YEAR})[\)\]]")
_TITLE_YEAR_DOTTED = re.compile(rf"^(?P<title>.+?)[\.\s_-]+(?P<year>{_YEAR})(?:[\.\s_-]|$)")
_LEADING_YEAR = re.compile(rf"^(?P<year>{_YEAR})\s*[-_. ]+\s*(?P<title>.+)$")

_SXXEYY = re.compile(r"[Ss](?P<s>\d{1,4})\s*[Ee](?P<e>\d{1,3})(?:[-Ee]+\d{1,3})*")
_DATED = re.compile(r"(?<!\d)(?P<y>19[3-9]\d|20[0-4]\d)[-. ](?P<m>0[1-9]|1[0-2])[-. ](?P<d>0[1-9]|[12]\d|3[01])(?!\d)")
_NXNN = re.compile(r"(?<!\d)(?P<s>\d{1,2})[xX](?P<e>\d{1,3})(?!\d)")
_SEASON_DIR = re.compile(r"^(?:season|series|s)\s*_?(?P<s>\d{1,4})$", re.IGNORECASE)
_SPECIALS_DIR = re.compile(r"^specials?$", re.IGNORECASE)
_EP_ONLY = re.compile(r"^(?:e|ep|episode)?\s*_?(?P<e>\d{1,3})\b", re.IGNORECASE)
_CERT_TAG = re.compile(r"\[(?P<cert>U|PG|12A?|15|18)\]", re.IGNORECASE)
_JUNK = re.compile(r"[\.\s_]+")


def clean_title(raw: str) -> str:
    t = _JUNK.sub(" ", raw).strip(" -_.")
    return re.sub(r"\s{2,}", " ", t)


@dataclass
class TitleYear:
    title: str
    year: int | None


def parse_title_year(name: str) -> TitleYear:
    """'Blake's 7 (1978)' -> ('Blake's 7', 1978); 'Top.Gun.1986.1080p' -> ('Top Gun', 1986)."""
    name = name.strip()
    m = _TITLE_YEAR_PAREN.match(name)
    if m:
        return TitleYear(clean_title(m.group("title")), int(m.group("year")))
    m = _LEADING_YEAR.match(name)
    if m:
        return TitleYear(clean_title(m.group("title")), int(m.group("year")))
    m = _TITLE_YEAR_DOTTED.match(name)
    if m:
        return TitleYear(clean_title(m.group("title")), int(m.group("year")))
    return TitleYear(clean_title(name), None)


def parse_certificate_tag(name: str) -> str | None:
    m = _CERT_TAG.search(name)
    return m.group("cert").upper() if m else None


@dataclass
class EpisodeInfo:
    season: int | None
    episode: int | None
    title: str


def parse_episode(file_path: PurePath, show_title: str | None = None) -> EpisodeInfo:
    """Work out season/episode from the file name, falling back to the season folder."""
    stem = file_path.stem
    season: int | None = None
    episode: int | None = None
    title = stem

    m = _SXXEYY.search(stem) or _NXNN.search(stem)
    dm = _DATED.search(stem)
    if m:
        season, episode = int(m.group("s")), int(m.group("e"))
        title = stem[m.end():]
    elif dm:
        # Date-based programmes (Grandstand, Match of the Day): season = year, episode = MMDD,
        # which keeps them in broadcast order.
        season, episode = int(dm.group("y")), int(dm.group("m")) * 100 + int(dm.group("d"))
        title = stem[dm.end():] or f"{dm.group('d')}/{dm.group('m')}/{dm.group('y')}"
    else:
        parent = file_path.parent.name
        sm = _SEASON_DIR.match(parent)
        if sm:
            season = int(sm.group("s"))
        elif _SPECIALS_DIR.match(parent):
            season = 0
        em = _EP_ONLY.match(stem)
        if em:
            episode = int(em.group("e"))
            title = stem[em.end():]

    if season is None:
        parent = file_path.parent.name
        sm = _SEASON_DIR.match(parent)
        if sm:
            season = int(sm.group("s"))
        elif _SPECIALS_DIR.match(parent):
            season = 0

    title = clean_title(title)
    if show_title and title.lower().startswith(show_title.lower()):
        title = clean_title(title[len(show_title):])
    # Strip quality/junk tags that follow the title.
    title = re.split(r"\b(?:720p|1080p|480p|576p|x264|x265|h264|hevc|web|dvd|bluray|hdtv)\b",
                     title, maxsplit=1, flags=re.IGNORECASE)[0]
    title = clean_title(title)
    if not title:
        title = f"Episode {episode}" if episode is not None else stem
    return EpisodeInfo(season, episode, title)


def is_video(path: PurePath) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS and not path.name.startswith(".")


def looks_like_sample(path: PurePath) -> bool:
    n = path.stem.lower()
    return n == "sample" or n.endswith("-sample") or n.endswith(".sample") or "/sample/" in str(path).lower()


_ARTIST_TITLE = re.compile(r"^(?P<artist>[^-–]+?)\s+[-–]\s+(?P<title>.+)$")
_DECADE_DIR = re.compile(r"^(?:(?P<full>19[3-9]0|20[0-4]0)s?|(?P<short>[3-9]0)s)$", re.IGNORECASE)


@dataclass
class MusicInfo:
    artist: str | None
    title: str
    year: int | None


def parse_music(name: str) -> MusicInfo:
    """'Queen - Radio Ga Ga (1984)' -> artist Queen, title Radio Ga Ga, year 1984."""
    ty = parse_title_year(name)
    m = _ARTIST_TITLE.match(ty.title)
    if m:
        return MusicInfo(clean_title(m.group("artist")), clean_title(m.group("title")), ty.year)
    return MusicInfo(None, ty.title, ty.year)


def parse_decade_dir(name: str) -> int | None:
    """'1980s', '80s', '1980' -> 1980."""
    m = _DECADE_DIR.match(name.strip())
    if not m:
        return None
    if m.group("full"):
        return int(m.group("full"))
    return 1900 + int(m.group("short"))
