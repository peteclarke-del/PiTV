"""Shared FastAPI dependencies and small serialisers."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterator

from fastapi import Depends, Request

from ... import db as dbm
from ...db import effective, row_to_dict


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = dbm.connect(request.app.state.cfg.db_path)
    try:
        yield conn
    finally:
        conn.close()


def admin_conn(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> sqlite3.Connection:
    from ..auth import require_admin
    require_admin(request, conn)
    return conn


MEDIA_PUBLIC = ("id", "kind", "show_id", "season", "episode", "title", "year", "duration",
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
    keys = ("id", "title", "year", "certificate", "genres", "plot", "kids", "home_channel_id",
            "mode", "anchor_time", "anchor_days", "rest_weeks", "excluded", "missing", "overrides",
            "source_id")
    out = {k: eff.get(k) for k in keys}
    out["scanned"] = {k: d.get(k) for k in ("title", "year", "certificate", "genres", "plot", "kids")}
    out["folder"] = d.get("path", "").rsplit("/", 1)[-1]
    for extra in ("episode_count", "next_season", "next_episode", "attention_count", "end_year"):
        if extra in d:
            out[extra] = d[extra]
    return out


def slot_public(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    out = {k: d.get(k) for k in ("id", "channel_id", "day", "start_ts", "end_ts", "media_id",
                                 "offset", "kind", "part", "replay", "locked", "title", "subtitle")}
    for k in ("year", "certificate", "duration", "plot", "show_id", "season", "episode", "hwdec", "media_kind"):
        if k in d:
            out[k] = d[k]
    if "genres" in d and isinstance(d["genres"], str):
        try:
            out["genres"] = json.loads(d["genres"])
        except ValueError:
            out["genres"] = []
    return out


SLOT_QUERY = ("SELECT s.*, m.year, m.certificate, m.duration, m.plot, m.show_id, m.season, m.episode,"
              " m.hwdec, m.genres, m.kind AS media_kind FROM schedule s"
              " LEFT JOIN media m ON m.id = s.media_id")
