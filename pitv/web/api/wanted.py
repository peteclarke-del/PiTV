"""Admin API for the wanted list (requests to pitv_content)."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ...db import now_ts, row_to_dict, rows_to_dicts, tx
from ...wanted import queue_gaps
from .deps import admin_conn, optional_int, optional_text

router = APIRouter(prefix="/api", dependencies=[Depends(admin_conn)])
WANTED_KINDS = ("episode", "movie", "advert", "music")


@router.get("/wanted")
def list_wanted(conn: sqlite3.Connection = Depends(admin_conn)):
    rows = conn.execute("SELECT w.*, s.title AS show_title FROM wanted w LEFT JOIN shows s ON s.id = w.show_id"
                        " ORDER BY CASE w.status WHEN 'queued' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END, w.id DESC"
                        " LIMIT 300").fetchall()
    return rows_to_dicts(rows)


@router.post("/wanted")
def add_wanted(body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    kind = body.get("kind")
    if kind not in WANTED_KINDS:
        raise HTTPException(400, "kind must be episode, movie, advert or music")
    title = optional_text(body.get("title"), "title")
    if not title:
        raise HTTPException(400, "title required")
    ref = optional_text(body.get("ref"), "ref", limit=2000)
    if ref and not ref.startswith(("http://", "https://")):
        raise HTTPException(400, "ref must be a URL (a specific page or file for pitv_content to use)")
    row = (kind, title, *(optional_int(body.get(k), k) for k in ("year", "season", "episode", "show_id")),
           "url" if ref else "auto", ref, optional_text(body.get("genre"), "genre"),
           optional_text(body.get("artist"), "artist"), now_ts())
    with tx(conn):
        cur = conn.execute("INSERT INTO wanted(kind, title, year, season, episode, show_id, provider, ref, genre, artist, created_at)"
                           " VALUES (?,?,?,?,?,?,?,?,?,?,?)", row)
    return row_to_dict(conn.execute("SELECT * FROM wanted WHERE id = ?", (cur.lastrowid,)).fetchone())


@router.post("/wanted/{wid}/retry")
def retry_wanted(wid: int, conn: sqlite3.Connection = Depends(admin_conn)):
    with tx(conn):
        conn.execute("UPDATE wanted SET status = 'queued', attempts = 0, message = NULL, progress = 0 WHERE id = ?", (wid,))
    return {"ok": True}


@router.delete("/wanted/{wid}")
def delete_wanted(wid: int, conn: sqlite3.Connection = Depends(admin_conn)):
    with tx(conn):
        conn.execute("DELETE FROM wanted WHERE id = ?", (wid,))
    return {"ok": True}


@router.post("/wanted/scan-gaps")
def scan_gaps(conn: sqlite3.Connection = Depends(admin_conn)):
    return {"added": queue_gaps(conn)}
