"""Walk the configured sources and keep the shows/media tables in step with the disk."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..db import CARTOON_GENRES, MUSIC_GENRES, get_setting, now_ts, row_to_dict, run_log_finish, run_log_start, tx
from .naming import (is_video, looks_like_sample, parse_certificate_tag, parse_decade_dir,
                     parse_episode, parse_music, parse_title_year)
from .nfo import read_nfo
from .probe import probe_cached

Progress = Callable[[str, int, int], None]
log = logging.getLogger("pitv.scanner")
_YEAR_DIR = re.compile(r"^(19[3-9]\d|20[0-4]\d)$")
_CHANNEL_DIR = re.compile(r"(?:ch(?:annel)?\s*_?)?(\d{1,2})$", re.IGNORECASE)
_EXTRAS_DIRS = {"extras", "featurettes", "behind the scenes", "deleted scenes", "trailers",
                "interviews", "scenes", "shorts", "other", "sample", "samples"}


@dataclass
class ScanSummary:
    source_id: int
    shows_seen: int = 0
    media_seen: int = 0
    media_new: int = 0
    media_missing: int = 0
    probe_failures: int = 0
    messages: list[str] = field(default_factory=list)

    def text(self) -> str:
        return (f"{self.media_seen} items ({self.media_new} new, {self.media_missing} missing,"
                f" {self.probe_failures} probe failures)")


def _noop(_msg: str, _done: int, _total: int) -> None:
    pass


def _count_videos(root: Path) -> int:
    return sum(1 for p in root.rglob("*") if p.is_file() and is_video(p))


def _stat(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, int(st.st_mtime)


def _upsert_media(conn: sqlite3.Connection, seen: set[str], summary: ScanSummary, *,
                  source_id: int, kind: str, path: Path, title: str, year: int | None,
                  show_id: int | None = None, season: int | None = None,
                  episode: int | None = None, certificate: str | None = None,
                  genres: list[str] | None = None, plot: str | None = None,
                  channel_hint: int | None = None, ffprobe_binary: str = "ffprobe",
                  artist: str | None = None, concert: int = 0) -> None:
    size, mtime = _stat(path)
    probe = probe_cached(conn, path, size, mtime, ffprobe_binary)
    attention: list[str] = []
    if probe is None or not probe.duration:
        summary.probe_failures += 1
        attention.append("Could not read duration (ffprobe failed)")
    if year is None and kind in ("episode", "movie", "advert"):
        attention.append("No year found")
    if kind == "movie" and certificate is None:
        attention.append("No certificate (treated as 15, post-watershed)")
    if probe and not probe.hwdec and (probe.height or 0) >= 720:
        attention.append(f"Software decode only ({probe.vcodec}, {probe.height}p)")

    existing = conn.execute("SELECT id FROM media WHERE path = ?", (str(path),)).fetchone()
    params = dict(
        source_id=source_id, kind=kind, show_id=show_id, season=season, episode=episode,
        title=title, year=year, path=str(path), size=size, mtime=mtime,
        duration=probe.duration if probe else None,
        vcodec=probe.vcodec if probe else None, acodec=probe.acodec if probe else None,
        width=probe.width if probe else None, height=probe.height if probe else None,
        interlaced=int(probe.interlaced) if probe else 0,
        hwdec=int(probe.hwdec) if probe else 0,
        certificate=certificate, genres=json.dumps(genres or []), plot=plot,
        channel_hint=channel_hint, attention="; ".join(attention) or None, updated_at=now_ts(),
        artist=artist, concert=concert,
    )
    if existing:
        sets = ", ".join(f"{k} = :{k}" for k in params)
        conn.execute(f"UPDATE media SET {sets}, missing = 0 WHERE id = :id",
                     {**params, "id": existing["id"]})
    else:
        cols = ", ".join(params)
        vals = ", ".join(f":{k}" for k in params)
        conn.execute(f"INSERT INTO media({cols}) VALUES ({vals})", params)
        summary.media_new += 1
    seen.add(str(path))
    summary.media_seen += 1


def _scan_tv(conn: sqlite3.Connection, source: dict, summary: ScanSummary, seen: set[str],
             progress: Progress, ffprobe_binary: str) -> None:
    root = Path(source["path"])
    total = _count_videos(root)
    done = 0
    show_dirs = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    for show_dir in show_dirs:
        ty = parse_title_year(show_dir.name)
        nfo = read_nfo(show_dir / "tvshow.nfo")
        title = (nfo.title if nfo and nfo.title else ty.title)
        year = ty.year or (nfo.year if nfo else None)
        cert = (nfo.certificate if nfo else None) or parse_certificate_tag(show_dir.name)
        genres = list(nfo.genres) if nfo else []
        kids = int(nfo.kids) if nfo else 0
        plot = nfo.plot if nfo else None
        category = source.get("category") or "general"
        if category == "sport" and not any(g.lower() == "sport" for g in genres):
            genres.append("Sport")
        if category == "kids":
            kids = 1
        row = conn.execute("SELECT id FROM shows WHERE path = ?", (str(show_dir),)).fetchone()
        if row:
            show_id = row["id"]
            conn.execute(
                "UPDATE shows SET title=?, year=?, certificate=?, genres=?, plot=?, kids=?, category=?,"
                " missing=0, updated_at=? WHERE id=?",
                (title, year, cert, json.dumps(genres), plot, kids, category, now_ts(), show_id))
        else:
            cur = conn.execute(
                "INSERT INTO shows(source_id, path, title, year, certificate, genres, plot, kids, category,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (source["id"], str(show_dir), title, year, cert, json.dumps(genres), plot, kids, category,
                 now_ts()))
            show_id = int(cur.lastrowid)
        summary.shows_seen += 1

        files = sorted(p for p in show_dir.rglob("*") if p.is_file() and is_video(p)
                       and not looks_like_sample(p)
                       and not any(part.lower() in _EXTRAS_DIRS for part in p.relative_to(show_dir).parts[:-1]))
        for f in files:
            done += 1
            progress(f"{title}: {f.name}", done, total)
            ep = parse_episode(f.relative_to(show_dir), title)
            enfo = read_nfo(f.with_suffix(".nfo"))
            ep_title = (enfo.title if enfo and enfo.title else ep.title)
            season = ep.season if ep.season is not None else (enfo.season if enfo else None)
            episode = ep.episode if ep.episode is not None else (enfo.episode if enfo else None)
            ep_year = None
            if enfo and enfo.aired:
                try:
                    ep_year = int(enfo.aired[:4])
                except ValueError:
                    ep_year = None
            _upsert_media(conn, seen, summary, source_id=source["id"], kind="episode", path=f,
                          title=ep_title, year=ep_year or year, show_id=show_id, season=season,
                          episode=episode, certificate=cert, genres=genres,
                          plot=(enfo.plot if enfo else None), ffprobe_binary=ffprobe_binary)
            if season is None or episode is None:
                conn.execute("UPDATE media SET attention = COALESCE(attention || '; ', '') ||"
                             " 'Season/episode not recognised' WHERE path = ?", (str(f),))


def _pick_movie_file(folder: Path) -> Path | None:
    candidates = [p for p in folder.rglob("*") if p.is_file() and is_video(p)
                  and not looks_like_sample(p)
                  and not any(part.lower() in _EXTRAS_DIRS for part in p.relative_to(folder).parts[:-1])]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def _scan_movies(conn: sqlite3.Connection, source: dict, summary: ScanSummary, seen: set[str],
                 progress: Progress, ffprobe_binary: str) -> None:
    root = Path(source["path"])
    entries = sorted(p for p in root.iterdir() if not p.name.startswith("."))
    total = len(entries)
    for i, entry in enumerate(entries, 1):
        progress(entry.name, i, total)
        if entry.is_dir():
            f = _pick_movie_file(entry)
            if f is None:
                continue
            ty = parse_title_year(entry.name)
            nfo = read_nfo(f.with_suffix(".nfo")) or read_nfo(entry / "movie.nfo")
        elif entry.is_file() and is_video(entry):
            f = entry
            ty = parse_title_year(entry.stem)
            nfo = read_nfo(f.with_suffix(".nfo"))
        else:
            continue
        title = nfo.title if nfo and nfo.title else ty.title
        year = ty.year or (nfo.year if nfo else None)
        cert = (nfo.certificate if nfo else None) or parse_certificate_tag(entry.name) \
            or parse_certificate_tag(f.name)
        genres = nfo.genres if nfo else []
        _upsert_media(conn, seen, summary, source_id=source["id"], kind="movie", path=f,
                      title=title, year=year, certificate=cert, genres=genres,
                      plot=(nfo.plot if nfo else None), ffprobe_binary=ffprobe_binary)
        if nfo and nfo.kids:
            conn.execute("UPDATE media SET genres = ? WHERE path = ?",
                         (json.dumps(list(dict.fromkeys(genres + ["Children"]))), str(f)))


def _year_from_parents(path: Path, root: Path) -> int | None:
    for parent in path.relative_to(root).parents:
        if parent.name and _YEAR_DIR.match(parent.name):
            return int(parent.name)
    return None


_ADULT_DIRS = {"adult", "18", "alcohol", "tobacco", "not for kids", "grown-ups"}
_FAMILY_DIRS = {"kids", "family", "children", "family safe", "toys"}


def advert_family_safe(path: Path, root: Path, keywords: list[str]) -> bool:
    """False for adverts that must never air on a family channel: alcohol, tobacco, adult or
    gambling, judged from folder names and keywords in the file name. A Kids/Family folder
    always wins; an override in the admin UI beats both."""
    parts = [p.lower() for p in path.relative_to(root).parts[:-1]]
    if any(p in _FAMILY_DIRS for p in parts):
        return True
    if any(p in _ADULT_DIRS for p in parts):
        return False
    name = path.stem.lower()
    return not any(k.lower() in name for k in keywords)


def _scan_flat(conn: sqlite3.Connection, source: dict, kind: str, summary: ScanSummary,
               seen: set[str], progress: Progress, ffprobe_binary: str) -> None:
    root = Path(source["path"])
    files = sorted(p for p in root.rglob("*") if p.is_file() and is_video(p))
    total = len(files)
    keywords = get_setting(conn, "adult_advert_keywords") or []
    for i, f in enumerate(files, 1):
        progress(f.name, i, total)
        ty = parse_title_year(f.stem)
        year = ty.year or _year_from_parents(f, root)
        anfo = read_nfo(f.with_suffix(".nfo")) if kind == "advert" else None
        if kind == "advert":
            safe = int(advert_family_safe(f, root, keywords))
            tags = {t.lower() for t in (anfo.tags if anfo else [])}
            if tags & {"alcohol", "tobacco", "adult", "gambling", "18"}:
                safe = 0
            elif tags & {"family", "kids", "children", "family-safe"}:
                safe = 1
            if anfo and anfo.year and year is None:
                year = anfo.year
        hint = None
        if kind == "ident":
            for parent in f.relative_to(root).parents:
                m = _CHANNEL_DIR.search(parent.name)
                if parent.name and m:
                    hint = int(m.group(1))
                    break
        _upsert_media(conn, seen, summary, source_id=source["id"], kind=kind, path=f,
                      title=ty.title, year=year, channel_hint=hint,
                      ffprobe_binary=ffprobe_binary)
        if kind == "advert":
            conn.execute("UPDATE media SET family_safe = ?, plot = COALESCE(?, plot) WHERE path = ?",
                         (safe, anfo.plot if anfo else None, str(f)))


_CONCERT_DIRS = {"concerts", "concert", "live", "gigs", "festivals", "full shows"}


def _scan_music(conn: sqlite3.Connection, source: dict, summary: ScanSummary, seen: set[str],
                progress: Progress, ffprobe_binary: str) -> None:
    """Music videos: `Genre/Artist - Title (1984).mp4`, `1980s/Disco/...`, `Concerts/Artist - Live at X (1986).mp4`.
    Genre and decade come from any ancestor folder; a concert is anything under a concerts
    folder or longer than 35 minutes."""
    root = Path(source["path"])
    genre_names = {g.lower() for g in (get_setting(conn, "music_genres") or MUSIC_GENRES)}
    files = sorted(p for p in root.rglob("*") if p.is_file() and is_video(p) and not looks_like_sample(p))
    total = len(files)
    for i, f in enumerate(files, 1):
        progress(f.name, i, total)
        mi = parse_music(f.stem)
        genres: list[str] = []
        decade = None
        concert = 0
        for parent in f.relative_to(root).parents:
            n = parent.name
            if not n:
                continue
            if n.lower() in genre_names and n.title() not in genres:
                genres.append(n.title())
            if n.lower() in _CONCERT_DIRS:
                concert = 1
            d = parse_decade_dir(n)
            if d and decade is None:
                decade = d
            if _YEAR_DIR.match(n) and mi.year is None:
                mi = parse_music(f.stem)
                mi.year = int(n)
        nfo = read_nfo(f.with_suffix(".nfo"))
        if nfo:
            genres = list(dict.fromkeys(genres + [g for g in nfo.genres]))
            mi.year = mi.year or nfo.year
            if nfo.artist and not mi.artist:
                mi.artist = nfo.artist
                mi.title = nfo.title or mi.title
            if any(t.lower() in ("concert", "live") for t in nfo.tags):
                concert = 1
        year = mi.year or (decade + 5 if decade else None)   # mid-decade when only the decade is known
        title = f"{mi.artist} - {mi.title}" if mi.artist else mi.title
        _upsert_media(conn, seen, summary, source_id=source["id"], kind="music", path=f, title=title,
                      year=year, genres=genres, ffprobe_binary=ffprobe_binary, artist=mi.artist, concert=concert)
        row = conn.execute("SELECT id, duration, attention FROM media WHERE path = ?", (str(f),)).fetchone()
        extra = []
        if row and row["duration"] and row["duration"] >= 35 * 60 and not concert:
            conn.execute("UPDATE media SET concert = 1 WHERE id = ?", (row["id"],))
        if not genres:
            extra.append("No genre folder (put it under e.g. Pop/ or Rock/)")
        if mi.year is None and decade:
            extra.append(f"Year approximated from the {decade}s folder")
        if extra and row:
            att = "; ".join(x for x in ([row["attention"]] if row["attention"] else []) + extra)
            conn.execute("UPDATE media SET attention = ? WHERE id = ?", (att, row["id"]))


def _is_cartoon(genres: list[str], cartoon_genres: set[str]) -> bool:
    return any(g.lower() in cartoon_genres for g in genres)


def _tag_cartoons(conn: sqlite3.Connection) -> None:
    """Category 'cartoon' is informational (dayparts, the admin); channel membership is the
    line-up's job, driven by each channel's genre lists."""
    cartoon_genres = {g.lower() for g in (get_setting(conn, "cartoon_genres") or CARTOON_GENRES)}
    for r in conn.execute("SELECT id, genres FROM shows WHERE category = 'general' AND missing = 0"):
        if _is_cartoon(json.loads(r["genres"] or "[]"), cartoon_genres):
            conn.execute("UPDATE shows SET category = 'cartoon' WHERE id = ?", (r["id"],))


def scan_source(conn: sqlite3.Connection, source: dict, progress: Progress = _noop,
                ffprobe_binary: str = "ffprobe") -> ScanSummary:
    summary = ScanSummary(source_id=source["id"])
    root = Path(source["path"])
    if not root.is_dir():
        summary.messages.append(f"Path not available: {root}")
        return summary
    seen: set[str] = set()
    with tx(conn):
        if source["type"] == "tv":
            _scan_tv(conn, source, summary, seen, progress, ffprobe_binary)
        elif source["type"] == "movie":
            _scan_movies(conn, source, summary, seen, progress, ffprobe_binary)
        elif source["type"] == "music":
            _scan_music(conn, source, summary, seen, progress, ffprobe_binary)
        else:
            _scan_flat(conn, source, source["type"], summary, seen, progress, ffprobe_binary)
        # Anything under this source we did not see has gone missing.
        for r in conn.execute("SELECT id, path FROM media WHERE source_id = ? AND missing = 0",
                              (source["id"],)):
            if r["path"] not in seen:
                conn.execute("UPDATE media SET missing = 1 WHERE id = ?", (r["id"],))
                summary.media_missing += 1
        if source["type"] == "tv":
            conn.execute("UPDATE shows SET missing = 1 WHERE source_id = ? AND id NOT IN"
                         " (SELECT DISTINCT show_id FROM media WHERE show_id IS NOT NULL AND missing = 0)",
                         (source["id"],))
        _tag_cartoons(conn)
        conn.execute("UPDATE sources SET last_scanned_at = ?, last_scan_summary = ? WHERE id = ?",
                     (now_ts(), summary.text(), source["id"]))
    return summary


def scan_all(conn: sqlite3.Connection, progress: Progress = _noop,
             ffprobe_binary: str = "ffprobe", source_ids: list[int] | None = None) -> int:
    run_id = run_log_start(conn, "scan")
    details: list[str] = []
    status = "ok"
    sources = [row_to_dict(r) for r in conn.execute("SELECT * FROM sources WHERE enabled = 1 ORDER BY id")]
    if source_ids:
        sources = [s for s in sources if s["id"] in source_ids]
    for source in sources:
        def prog(msg: str, done: int, total: int, _s=source) -> None:
            progress(f"[{_s['name']}] {msg}", done, total)
        try:
            log.info("scanning %s (%s) at %s", source["name"], source["type"], source["path"])
            summary = scan_source(conn, source, prog, ffprobe_binary)
            log.info("%s: %s", source["name"], summary.text())
            for m in summary.messages:
                log.warning("%s: %s", source["name"], m)
            details.append(f"{source['name']}: {summary.text()}")
            details.extend(f"{source['name']}: {m}" for m in summary.messages)
            if summary.messages:
                status = "warning"
        except Exception as exc:  # noqa: BLE001 - keep scanning other sources
            log.exception("scan of %s failed", source["name"])
            details.append(f"{source['name']}: failed: {exc!r}")
            status = "error"
    # New series and films get a channel; existing membership is untouched.
    from ..lineup import bind_fetched, generate, restore_if_empty
    restore_if_empty(conn)
    bound = bind_fetched(conn)
    if bound["bound"]:
        details.append(f"line-up: {bound['bound']} fetched file(s) bound to their slots")
    gen = generate(conn)
    if gen["assigned"] or gen["unmatched"]:
        details.append(f"line-up: {gen['assigned']} placed, {gen['unmatched']} matched no channel")
    run_log_finish(conn, run_id, status, "; ".join(details), details)
    return run_id
