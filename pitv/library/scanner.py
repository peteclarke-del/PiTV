"""Walk the configured sources and keep the shows/media tables in step with the disk."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..db import now_ts, row_to_dict, run_log_finish, run_log_start, tx
from .naming import (is_video, looks_like_sample, parse_certificate_tag, parse_episode,
                     parse_title_year)
from .nfo import read_nfo
from .probe import probe_cached

Progress = Callable[[str, int, int], None]
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
                  channel_hint: int | None = None, ffprobe_binary: str = "ffprobe") -> None:
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
        genres = nfo.genres if nfo else []
        kids = int(nfo.kids) if nfo else 0
        plot = nfo.plot if nfo else None
        row = conn.execute("SELECT id FROM shows WHERE path = ?", (str(show_dir),)).fetchone()
        if row:
            show_id = row["id"]
            conn.execute(
                "UPDATE shows SET title=?, year=?, certificate=?, genres=?, plot=?, kids=?,"
                " missing=0, updated_at=? WHERE id=?",
                (title, year, cert, json.dumps(genres), plot, kids, now_ts(), show_id))
        else:
            cur = conn.execute(
                "INSERT INTO shows(source_id, path, title, year, certificate, genres, plot, kids,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (source["id"], str(show_dir), title, year, cert, json.dumps(genres), plot, kids,
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


def _scan_flat(conn: sqlite3.Connection, source: dict, kind: str, summary: ScanSummary,
               seen: set[str], progress: Progress, ffprobe_binary: str) -> None:
    root = Path(source["path"])
    files = sorted(p for p in root.rglob("*") if p.is_file() and is_video(p))
    total = len(files)
    for i, f in enumerate(files, 1):
        progress(f.name, i, total)
        ty = parse_title_year(f.stem)
        year = ty.year or _year_from_parents(f, root)
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


def _assign_home_channels(conn: sqlite3.Connection) -> None:
    """Give every show without a home channel the least-loaded enabled channel."""
    channels = [r["id"] for r in conn.execute("SELECT id FROM channels WHERE enabled = 1 ORDER BY number")]
    if not channels:
        return
    load = {cid: 0 for cid in channels}
    for r in conn.execute("SELECT home_channel_id AS c, COUNT(*) AS n FROM shows"
                          " WHERE home_channel_id IS NOT NULL GROUP BY home_channel_id"):
        if r["c"] in load:
            load[r["c"]] = r["n"]
    order = {"U": 0, "PG": 1, "12": 2, "12A": 2, "15": 3, "18": 4}
    rows = conn.execute("SELECT id, certificate, kids, title FROM shows WHERE home_channel_id IS NULL"
                        " AND excluded = 0 AND missing = 0").fetchall()
    # Sort by kids then certificate so consecutive assignments alternate channels and every
    # channel ends up with a similar mix of daytime-friendly and post-watershed series.
    rows = sorted(rows, key=lambda r: (-r["kids"], order.get((r["certificate"] or "PG").upper(), 1), r["title"]))
    for r in rows:
        cid = min(channels, key=lambda c: (load[c], c))
        conn.execute("UPDATE shows SET home_channel_id = ? WHERE id = ?", (cid, r["id"]))
        load[cid] += 1


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
        _assign_home_channels(conn)
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
            summary = scan_source(conn, source, prog, ffprobe_binary)
            details.append(f"{source['name']}: {summary.text()}")
            details.extend(f"{source['name']}: {m}" for m in summary.messages)
            if summary.messages:
                status = "warning"
        except Exception as exc:  # noqa: BLE001 - keep scanning other sources
            details.append(f"{source['name']}: failed: {exc!r}")
            status = "error"
    run_log_finish(conn, run_id, status, "; ".join(details), details)
    return run_id
