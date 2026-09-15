"""Shared FastAPI dependencies, serialisers and small helpers for the API modules."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from typing import Any, Iterator

from fastapi import Depends, Request

from ... import db as dbm
from ...db import effective, row_to_dict
from ..auth import require_admin


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = dbm.connect(request.app.state.cfg.db_path)
    try:
        yield conn
    finally:
        conn.close()


def admin_conn(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> sqlite3.Connection:
    require_admin(request, conn)
    return conn


def run_cmd(args: list[str], timeout: float = 5) -> tuple[int, str, str]:
    """Run a command; (returncode, stdout, stderr) stripped. A missing binary or a timeout
    counts as a failure (returncode 1, the error in stderr) rather than an exception."""
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return out.returncode, out.stdout.strip(), out.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


MEDIA_PUBLIC = ("id", "kind", "show_id", "season", "episode", "title", "year", "duration", "artist", "concert", "family_safe",
                "vcodec", "acodec", "width", "height", "interlaced", "hwdec", "certificate",
                "genres", "plot", "channel_hint", "excluded", "missing", "attention", "overrides",
                "transcoded_path", "size", "source_id")


def media_public(row: sqlite3.Row | dict[str, Any] | None, with_path: bool = False) -> dict[str, Any] | None:
    if row is None:
        return None
    d = row_to_dict(row) if isinstance(row, sqlite3.Row) else dict(row)
    eff = effective(d)
    out = {k: eff.get(k) for k in MEDIA_PUBLIC if k in eff}
    out["scanned"] = {k: d.get(k) for k in ("title", "year", "certificate", "genres", "plot")}
    out["filename"] = d.get("path", "").rsplit("/", 1)[-1]
    if with_path:
        out["path"] = d.get("path")
    return out


def show_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    d = row_to_dict(row) if isinstance(row, sqlite3.Row) else dict(row)
    eff = effective(d)
    keys = ("id", "title", "year", "certificate", "genres", "plot", "kids", "category", "home_channel_id",
            "mode", "anchor_time", "anchor_days", "rest_weeks", "excluded", "missing", "overrides",
            "source_id")
    out = {k: eff.get(k) for k in keys}
    out["scanned"] = {k: d.get(k) for k in ("title", "year", "certificate", "genres", "plot", "kids")}
    out["folder"] = d.get("path", "").rsplit("/", 1)[-1]
    for extra in ("episode_count", "next_season", "next_episode", "attention_count", "end_year"):
        if extra in d:
            out[extra] = d[extra]
    return out


def slot_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """A schedule slot (or a merged guide entry from `pitv.guide`) without file paths."""
    d = dict(row)
    out = {k: d.get(k) for k in ("id", "channel_id", "day", "start_ts", "end_ts", "media_id",
                                 "offset", "kind", "part", "replay", "locked", "title", "subtitle", "block")}
    for k in ("year", "certificate", "duration", "plot", "show_id", "season", "episode", "hwdec", "media_kind",
              "items", "video_title", "video_id"):
        if k in d:
            out[k] = d[k]
    if "genres" in d and isinstance(d["genres"], str):
        try:
            out["genres"] = json.loads(d["genres"])
        except ValueError:
            out["genres"] = []
    return out
