"""Admin API for the wanted list, archive.org search and the transcode queue."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ...db import now_ts, row_to_dict, rows_to_dicts, tx
from .deps import admin_conn

router = APIRouter(prefix="/api", dependencies=[Depends(admin_conn)])


def _poke(request: Request) -> None:
    request.app.state.player.call("acquire-poke")


@router.get("/wanted")
def list_wanted(conn: sqlite3.Connection = Depends(admin_conn)):
    rows = conn.execute("SELECT w.*, s.title AS show_title FROM wanted w LEFT JOIN shows s ON s.id = w.show_id"
                        " ORDER BY CASE w.status WHEN 'downloading' THEN 0 WHEN 'transcoding' THEN 0 WHEN 'searching' THEN 0"
                        " WHEN 'queued' THEN 1 WHEN 'failed' THEN 2 ELSE 3 END, w.id DESC LIMIT 300").fetchall()
    return rows_to_dicts(rows)


@router.post("/wanted")
def add_wanted(request: Request, body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    kind = body.get("kind")
    if kind not in ("episode", "movie", "advert"):
        raise HTTPException(400, "kind must be episode, movie or advert")
    title = str(body.get("title", "")).strip()
    if not title:
        raise HTTPException(400, "title required")
    provider = body.get("provider") or "auto"
    if provider not in ("auto", "archive", "url"):
        raise HTTPException(400, "provider must be auto, archive or url")
    if provider == "url" and not str(body.get("ref", "")).startswith(("http://", "https://")):
        raise HTTPException(400, "a URL is required for the url provider")
    with tx(conn):
        cur = conn.execute("INSERT INTO wanted(kind, title, year, season, episode, show_id, provider, ref, created_at)"
                           " VALUES (?,?,?,?,?,?,?,?,?)",
                           (kind, title, body.get("year"), body.get("season"), body.get("episode"), body.get("show_id"),
                            provider, body.get("ref"), now_ts()))
    _poke(request)
    return row_to_dict(conn.execute("SELECT * FROM wanted WHERE id = ?", (cur.lastrowid,)).fetchone())


@router.post("/wanted/{wid}/retry")
def retry_wanted(wid: int, request: Request, conn: sqlite3.Connection = Depends(admin_conn)):
    with tx(conn):
        conn.execute("UPDATE wanted SET status = 'queued', attempts = 0, message = NULL, progress = 0 WHERE id = ?", (wid,))
    _poke(request)
    return {"ok": True}


@router.delete("/wanted/{wid}")
def delete_wanted(wid: int, conn: sqlite3.Connection = Depends(admin_conn)):
    with tx(conn):
        conn.execute("DELETE FROM wanted WHERE id = ?", (wid,))
    return {"ok": True}


@router.post("/wanted/scan-gaps")
def scan_gaps(request: Request, conn: sqlite3.Connection = Depends(admin_conn)):
    from ...acquire.worker import AcquisitionWorker
    cfg = request.app.state.cfg
    w = AcquisitionWorker(cfg.db_path, now_ts, False, cfg.ffprobe_binary, lambda: None)
    return {"added": w.queue_gaps(conn)}


@router.get("/archive/search")
def archive_search(q: str, year: int | None = None):
    from ...acquire import providers
    try:
        return [c.__dict__ for c in providers.archive_search(q, year)]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"archive.org search failed: {exc}") from exc


@router.get("/archive/files")
def archive_files(identifier: str):
    from ...acquire import providers
    try:
        return providers.archive_files(identifier)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"archive.org metadata failed: {exc}") from exc


@router.get("/transcode")
def list_transcode(conn: sqlite3.Connection = Depends(admin_conn)):
    rows = conn.execute("SELECT t.*, m.title, m.vcodec, m.height, m.duration, m.transcoded_path FROM transcode_queue t"
                        " JOIN media m ON m.id = t.media_id ORDER BY t.id DESC LIMIT 300").fetchall()
    return rows_to_dicts(rows)


@router.post("/transcode")
def add_transcode(request: Request, body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    ids = body.get("media_ids") or ([body["media_id"]] if body.get("media_id") else [])
    if body.get("all_software"):
        ids = [r["id"] for r in conn.execute("SELECT id FROM media WHERE hwdec = 0 AND missing = 0 AND kind IN ('episode','movie')"
                                             " AND transcoded_path IS NULL")]
    added = 0
    with tx(conn):
        for mid in ids:
            cur = conn.execute("INSERT OR IGNORE INTO transcode_queue(media_id, created_at) VALUES (?, ?)", (int(mid), now_ts()))
            added += cur.rowcount
    _poke(request)
    return {"added": added}


@router.delete("/transcode/{tid}")
def delete_transcode(tid: int, conn: sqlite3.Connection = Depends(admin_conn)):
    with tx(conn):
        conn.execute("DELETE FROM transcode_queue WHERE id = ?", (tid,))
    return {"ok": True}
