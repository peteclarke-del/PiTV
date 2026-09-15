"""Admin API for the wanted list (requests to pitv_content)."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ...db import now_ts, row_to_dict, rows_to_dicts, tx
from ...wanted import queue_gaps
from .deps import admin_conn

router = APIRouter(prefix="/api", dependencies=[Depends(admin_conn)])


@router.get("/wanted")
def list_wanted(conn: sqlite3.Connection = Depends(admin_conn)):
    rows = conn.execute("SELECT w.*, s.title AS show_title FROM wanted w LEFT JOIN shows s ON s.id = w.show_id"
                        " ORDER BY CASE w.status WHEN 'queued' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END, w.id DESC"
                        " LIMIT 300").fetchall()
    return rows_to_dicts(rows)


@router.post("/wanted")
def add_wanted(body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    kind = body.get("kind")
    if kind not in ("episode", "movie", "advert", "music"):
        raise HTTPException(400, "kind must be episode, movie, advert or music")
    title = str(body.get("title", "")).strip()
    if not title:
        raise HTTPException(400, "title required")
    ref = (body.get("ref") or "").strip() or None
    if ref and not ref.startswith(("http://", "https://")):
        raise HTTPException(400, "ref must be a URL (a specific page or file for pitv_content to use)")
    with tx(conn):
        cur = conn.execute("INSERT INTO wanted(kind, title, year, season, episode, show_id, provider, ref, genre, artist, created_at)"
                           " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (kind, title, body.get("year"), body.get("season"), body.get("episode"), body.get("show_id"),
                            "url" if ref else "auto", ref, (body.get("genre") or None), (body.get("artist") or None), now_ts()))
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
