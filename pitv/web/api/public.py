"""Public API: now & next, schedule/guide, remote control, auth and the event stream."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from ...db import all_settings, enabled_channels, now_ts, set_setting, tx
from ...guide import SLOT_QUERY, block_entry, collapse_blocks, next_programmes, slot_at
from ...scheduler.rules import broadcast_day_for, day_bounds, tz_of
from .. import auth
from ..events import format_sse
from .deps import get_conn, slot_public

router = APIRouter()


@router.get("/api/now")
def api_now(request: Request, conn: sqlite3.Connection = Depends(get_conn), next: int = 3):
    now = now_ts()
    out = []
    for ch in enabled_channels(conn):
        cur = slot_at(conn, ch["id"], now)
        current = slot_public(cur) if cur else None
        if cur and cur.get("block"):
            # Music channel: the "programme" is the whole block; keep the current video too.
            merged = block_entry(conn, cur, now)
            if merged:
                current = slot_public(merged)
        after = current["end_ts"] if current else now
        nxt = [slot_public(s) for s in next_programmes(conn, ch["id"], after, next)]
        if current and cur["kind"] != "programme":
            # During an ad break, show the programme that follows as "now".
            prog = conn.execute(SLOT_QUERY + " WHERE s.channel_id = ? AND s.start_ts <= ? AND s.kind = 'programme'"
                                " ORDER BY s.start_ts DESC LIMIT 1", (ch["id"], now)).fetchone()
            current["break"] = True
            current["previous_programme"] = slot_public(prog) if prog else None
        out.append({"channel": ch, "now": current, "next": nxt})
    return {"ts": now, "channels": out, "player": request.app.state.player_state}


@router.get("/api/schedule")
def api_schedule(conn: sqlite3.Connection = Depends(get_conn), start: int | None = None,
                 end: int | None = None, channel: int | None = None, ads: int = 0, replay: int = 1):
    now = now_ts()
    start = start if start is not None else now - 3600
    end = end if end is not None else start + 6 * 3600
    q = SLOT_QUERY + " WHERE s.end_ts > ? AND s.start_ts < ?"
    params: list[Any] = [start, end]
    if channel is not None:
        q += " AND s.channel_id = (SELECT id FROM channels WHERE number = ?)"
        params.append(channel)
    if not ads:
        q += " AND s.kind IN ('programme', 'filler')"
    if not replay:
        q += " AND s.replay = 0"
    q += " ORDER BY s.channel_id, s.start_ts"
    slots = [slot_public(r) for r in conn.execute(q, params)]
    if not ads:
        slots = collapse_blocks(slots)
    return {"ts": now, "start": start, "end": end, "channels": enabled_channels(conn), "slots": slots}


@router.get("/api/schedule/day/{day}")
def api_schedule_day(day: str, conn: sqlite3.Connection = Depends(get_conn), ads: int = 0):
    settings = all_settings(conn)
    tz = tz_of(conn)
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(400, "day must be YYYY-MM-DD") from exc
    day_start, day_end, next_start = day_bounds(d, settings, tz)
    q = SLOT_QUERY + " WHERE s.day = ?"
    if not ads:
        q += " AND s.kind IN ('programme', 'filler')"
    q += " ORDER BY s.channel_id, s.start_ts"
    slots = [slot_public(r) for r in conn.execute(q, (day,))]
    if not ads:
        slots = collapse_blocks(slots)
    return {"day": day, "day_start": day_start, "day_end": day_end, "next_day_start": next_start,
            "channels": enabled_channels(conn), "slots": slots}


@router.get("/api/schedule/days")
def api_schedule_days(conn: sqlite3.Connection = Depends(get_conn)):
    settings = all_settings(conn)
    tz = tz_of(conn)
    today = broadcast_day_for(now_ts(), settings, tz)
    rows = conn.execute("SELECT day, COUNT(*) AS n, MIN(start_ts) AS s, MAX(end_ts) AS e FROM schedule"
                        " WHERE replay = 0 GROUP BY day ORDER BY day").fetchall()
    return {"today": today.isoformat(), "days": [dict(r) for r in rows],
            "horizon_end": max((r["e"] for r in rows), default=None)}


# --- remote control -----------------------------------------------------------------------

@router.get("/api/player")
def api_player(request: Request):
    return request.app.state.player.state()


@router.post("/api/player/key")
def api_player_key(request: Request, body: dict[str, Any] = Body(...)):
    key = str(body.get("key", "")).lower()
    if not key:
        raise HTTPException(400, "key required")
    return request.app.state.player.key(key)


@router.post("/api/player/channel")
def api_player_channel(request: Request, body: dict[str, Any] = Body(...)):
    try:
        number = int(body["number"])
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, "number required") from exc
    return request.app.state.player.call("channel", number=number)


@router.post("/api/player/volume")
def api_player_volume(request: Request, body: dict[str, Any] = Body(...)):
    try:
        volume = max(0, min(100, int(body["volume"])))
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, "volume required") from exc
    return request.app.state.player.call("volume", volume=volume)


# --- auth ------------------------------------------------------------------------------------

@router.get("/api/auth")
def api_auth_status(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return {"password_set": auth.password_is_set(conn), "admin": auth.is_admin(request, conn)
            or not auth.password_is_set(conn)}


@router.post("/api/auth/setup")
def api_auth_setup(request: Request, response: Response, body: dict[str, Any] = Body(...),
                   conn: sqlite3.Connection = Depends(get_conn)):
    if auth.password_is_set(conn):
        raise HTTPException(409, "password already set")
    password = str(body.get("password", ""))
    if len(password) < 6:
        raise HTTPException(400, "password must be at least 6 characters")
    with tx(conn):
        set_setting(conn, "admin_password_hash", auth.hash_password(password))
    response.set_cookie(auth.COOKIE, auth.make_session(conn), max_age=auth.SESSION_SECONDS,
                        httponly=True, samesite="lax")
    return {"ok": True}


@router.post("/api/auth/login")
def api_auth_login(request: Request, response: Response, body: dict[str, Any] = Body(...),
                   conn: sqlite3.Connection = Depends(get_conn)):
    ip = request.client.host if request.client else "?"
    if auth.rate_limited(ip):
        raise HTTPException(429, "too many attempts, try again in a few minutes")
    stored = all_settings(conn).get("admin_password_hash")
    if not auth.verify_password(str(body.get("password", "")), stored):
        auth.record_attempt(ip)
        raise HTTPException(401, "wrong password")
    response.set_cookie(auth.COOKIE, auth.make_session(conn), max_age=auth.SESSION_SECONDS,
                        httponly=True, samesite="lax")
    return {"ok": True}


@router.post("/api/auth/logout")
def api_auth_logout(response: Response):
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@router.post("/api/auth/password")
def api_auth_password(request: Request, body: dict[str, Any] = Body(...),
                      conn: sqlite3.Connection = Depends(get_conn)):
    auth.require_admin(request, conn)
    stored = all_settings(conn).get("admin_password_hash")
    if stored and not auth.verify_password(str(body.get("current", "")), stored):
        raise HTTPException(401, "current password is wrong")
    new = str(body.get("password", ""))
    if len(new) < 6:
        raise HTTPException(400, "password must be at least 6 characters")
    with tx(conn):
        set_setting(conn, "admin_password_hash", auth.hash_password(new))
    return {"ok": True}


# --- events -----------------------------------------------------------------------------------

@router.get("/api/events")
async def api_events(request: Request):
    bus = request.app.state.bus
    q = bus.subscribe()

    async def gen():
        try:
            yield format_sse({"event": "hello", "data": {"ts": now_ts(), "player": request.app.state.player_state}})
            for msg in list(bus.last.values()):
                yield format_sse(msg)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    yield format_sse(msg)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
