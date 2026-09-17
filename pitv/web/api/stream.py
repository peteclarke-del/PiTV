"""The channel streams over HTTP: `/channel/3.m3u8` in VLC, `/channel/3` in a browser.

Public, like the guide and the remote: anyone on the network who can watch the television can
watch the stream. The admin's switch and limits are in Settings, Player."""

from __future__ import annotations

import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

from .deps import admin_conn

router = APIRouter()
NO_CACHE = {"Cache-Control": "no-store"}
VIEWER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _viewer_client(request: Request, viewer: str | None) -> str | None:
    address = request.client.host if request.client else None
    return f"{address}#{viewer}" if address and viewer and VIEWER_RE.fullmatch(viewer) else address


def _viewer_playlist(text: str, viewer: str | None) -> str:
    """Keep every segment request tied to the browser tab that opened the playlist."""
    if not viewer or not VIEWER_RE.fullmatch(viewer):
        return text
    return "".join(line if line.startswith("#") or not line.strip()
                   else f"{line.rstrip()}?viewer={viewer}\n"
                   for line in text.splitlines(keepends=True))


@router.get("/channel/{number}.m3u8", include_in_schema=False)
def playlist(number: int, request: Request, viewer: str | None = None):
    """The channel's live playlist, once its first segments are ready."""
    text = request.app.state.streams.playlist(number, client=_viewer_client(request, viewer))
    if text is None:
        raise HTTPException(503, "no stream for that channel (streaming may be off, or too many are running)")
    return Response(_viewer_playlist(text, viewer), media_type="application/vnd.apple.mpegurl", headers=NO_CACHE)


@router.delete("/channel/{number}.m3u8", status_code=204, include_in_schema=False)
def close_playlist(number: int, request: Request, viewer: str | None = None):
    """Let the browser release its encoder immediately when it changes channel."""
    request.app.state.streams.close(number, client=_viewer_client(request, viewer))
    return Response(status_code=204, headers=NO_CACHE)


@router.get("/channel/{number}/{name}", include_in_schema=False)
def segment(number: int, name: str, request: Request, viewer: str | None = None):
    path = request.app.state.streams.segment(number, name, client=_viewer_client(request, viewer))
    if path is None:
        raise HTTPException(404, "no such segment")
    # Segment numbers restart when a channel stream is stopped and later opened again. Keeping
    # an old s00000.ts in the browser cache can therefore splice a different programme into a
    # new MediaSource after a channel change and leave the video permanently stalled.
    return FileResponse(path, media_type="video/mp2t", headers=NO_CACHE)


@router.get("/api/streams")
def streams(request: Request, conn: sqlite3.Connection = Depends(admin_conn)):
    """What is being streamed now and who is watching, for the admin's System page."""
    return request.app.state.streams.status()
