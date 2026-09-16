"""The channel streams over HTTP: `/channel/3.m3u8` in VLC, `/channel/3` in a browser.

Public, like the guide and the remote: anyone on the network who can watch the television can
watch the stream. The admin's switch and limits are in Settings, Player."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

from .deps import admin_conn

router = APIRouter()
NO_CACHE = {"Cache-Control": "no-store"}


@router.get("/channel/{number}.m3u8", include_in_schema=False)
def playlist(number: int, request: Request):
    """The channel's live playlist, once its first segments are ready."""
    text = request.app.state.streams.playlist(number, client=request.client.host if request.client else None)
    if text is None:
        raise HTTPException(503, "no stream for that channel (streaming may be off, or too many are running)")
    return Response(text, media_type="application/vnd.apple.mpegurl", headers=NO_CACHE)


@router.get("/channel/{number}/{name}", include_in_schema=False)
def segment(number: int, name: str, request: Request):
    path = request.app.state.streams.segment(number, name, client=request.client.host if request.client else None)
    if path is None:
        raise HTTPException(404, "no such segment")
    # Segments are named once and deleted as they roll off, so they cache until then.
    return FileResponse(path, media_type="video/mp2t", headers={"Cache-Control": "public, max-age=60"})


@router.get("/api/streams")
def streams(request: Request, conn: sqlite3.Connection = Depends(admin_conn)):
    """What is being streamed now and who is watching, for the admin's System page."""
    return request.app.state.streams.status()
