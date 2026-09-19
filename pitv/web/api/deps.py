"""Shared FastAPI dependencies, serialisers and small helpers for the API modules."""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request

from ... import db as dbm
from ... import genres as genre_rules
from ...db import DEFAULT_SETTINGS, effective, genre_list, get_setting, row_to_dict
from ...logsetup import tail
from ..auth import is_content_client, require_admin


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = dbm.connect(request.app.state.cfg.db_path)
    try:
        yield conn
    finally:
        conn.close()


def admin_conn(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> sqlite3.Connection:
    require_admin(request, conn)
    return conn


async def read_json(request: Request, empty: Any = None) -> Any:
    """The request body as JSON, or `empty` when there is none. 415 when it is labelled as
    something else (the rule FastAPI applies to Body() parameters, which also takes an
    unlabelled body as JSON); 400 when it does not parse."""
    if not (await request.body()).strip():
        return empty
    ctype = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if ctype and ctype != "application/json" and not ctype.endswith("+json"):
        raise HTTPException(415, "send the body as application/json")
    try:
        return await request.json()
    except ValueError as exc:
        raise HTTPException(400, "the body is not valid JSON") from exc


def content_conn(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> sqlite3.Connection:
    """pitv_content's side of the contract: its shared token, else an admin session."""
    if not is_content_client(request):
        require_admin(request, conn)
    return conn


async def _json_object(request: Request) -> dict[str, Any]:
    body = await read_json(request)
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a JSON object")
    return body


async def admin_json(request: Request, _: sqlite3.Connection = Depends(admin_conn)) -> dict[str, Any]:
    """A JSON object body, read only once the caller is known to be an admin.

    FastAPI parses a Body() parameter before it runs any dependency, so on a Body() endpoint an
    anonymous caller gets the whole upload parsed before the 401. The endpoints that take whole
    documents (and so a larger size cap in app.RequestGuard) declare their body with this."""
    return await _json_object(request)


async def content_json(request: Request, _: sqlite3.Connection = Depends(content_conn)) -> dict[str, Any]:
    """As admin_json, for the endpoints pitv_content calls with its token."""
    return await _json_object(request)


def optional_int(value: Any, name: str) -> int | None:
    """A whole number from a JSON body, None for null or "", 400 for anything else."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise HTTPException(400, f"{name} must be a whole number")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"{name} must be a whole number") from exc


def optional_text(value: Any, name: str, limit: int = 500) -> str | None:
    """Trimmed text from a JSON body, None for null or blank, 400 for a non-string."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(400, f"{name} must be text")
    return value.strip()[:limit] or None


def tool_url(conn: sqlite3.Connection) -> str:
    """Base URL of pitv_content's local API."""
    return get_setting(conn, "content_tool_url") or DEFAULT_SETTINGS["content_tool_url"]


def log_tail(path: Path, lines: int, q: str = "", level: str = "") -> list[dict[str, Any]]:
    """The last `lines` entries of a log (clamped to 10..5000), filtered by substring and
    minimum level; 503 when the file is there but cannot be read."""
    try:
        return tail(path, max(10, min(lines, 5000)), q, level.upper())
    except OSError as exc:
        raise HTTPException(503, f"{path.name} cannot be read ({exc.strerror or 'I/O error'})") from exc


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
                "genres", "plot", "channel_hint", "home_channel_id", "excluded", "missing", "attention", "overrides",
                "cache_path", "origin", "size", "source_id", "uid")


def media_public(row: sqlite3.Row | dict[str, Any] | None, with_path: bool = False) -> dict[str, Any] | None:
    if row is None:
        return None
    d = row_to_dict(row) if isinstance(row, sqlite3.Row) else dict(row)
    eff = effective(d)
    out = {k: eff.get(k) for k in MEDIA_PUBLIC if k in eff}
    out["indexed"] = {k: d.get(k) for k in ("title", "year", "certificate", "genres", "plot")}
    # What it is, as placement reads it; `programme_type_set` says the owner chose it.
    out["programme_type"] = genre_rules.programme_type(eff.get("kind"), eff.get("genres") or [], None, eff.get("programme_type"))
    out["programme_type_set"] = bool(eff.get("programme_type"))
    out["enriched"] = d.get("enriched") or {}
    out["metadata_source"] = d.get("metadata_source")
    out["cached"] = bool(d.get("cache_path")) or d.get("origin") in ("cache", "online")
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
    out["cartoon"] = genre_rules.is_cartoon(eff.get("genres") or [])
    out["programme_type"] = genre_rules.programme_type("show", eff.get("genres") or [], eff.get("category"), eff.get("programme_type"))
    out["programme_type_set"] = bool(eff.get("programme_type"))
    out["indexed"] = {k: d.get(k) for k in ("title", "year", "certificate", "genres", "plot", "kids")}
    out["enriched"] = d.get("enriched") or {}
    out["metadata_source"] = d.get("metadata_source")
    # path holds the index uid (show:<source>:<folder>) or fetched:show:<title>:<year>
    out["folder"] = d.get("path", "").split(":", 2)[-1]
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
    if "genres" in d:
        out["genres"] = genre_list(d["genres"])
    return out


# Player state fields that describe the machine rather than the picture: file paths on the
# NAS, input devices, cache location, codec details. Admin sessions see them; the public
# guide and remote do not.
_PLAYER_PRIVATE = ("file", "input_devices", "maintenance", "stream", "hwdec", "on_pi", "clock_offset")


def player_public(state: dict[str, Any], admin: bool) -> dict[str, Any]:
    if admin or not state:
        return state
    out = {k: v for k, v in state.items() if k not in _PLAYER_PRIVATE}
    if isinstance(out.get("cache"), dict):
        out["cache"] = {k: v for k, v in out["cache"].items() if k != "dir"}
    if out.get("error"):
        out["error"] = "playback problem (details in the admin log)"
    return out
