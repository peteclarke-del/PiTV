"""Public API: now & next, schedule/guide, remote control, auth and the event stream.

Everything here is reachable by anyone on the LAN. It shows what is on and drives the
player like a remote control; anything that names files, devices or the system is
admin-only and lives in the other routers (or is filtered out of the player state here).
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from ... import db as dbm
from ...db import all_settings, enabled_channels, get_setting, now_ts, set_setting, tx
from ...guide import (
    SLOT_QUERY,
    block_entry,
    collapse_blocks,
    feature_seconds,
    next_programmes,
    slot_at,
)
from ...player.input import ACTIONS
from ...scheduler.rules import broadcast_day_for, day_bounds, tz_of
from ...scheduler.slots import parse_day
from .. import auth
from ..events import format_sse
from .deps import get_conn, player_public, slot_public

router = APIRouter()

MAX_SCHEDULE_WINDOW = 14 * 86400
REMOTE_KEYS = frozenset(ACTIONS)          # what a remote control can send; nothing else reaches the player
PUBLIC_EVENTS = frozenset({"player", "schedule", "library"})


@router.get("/api/now")
def api_now(request: Request, conn: sqlite3.Connection = Depends(get_conn), next: int = 3):
    now = now_ts()
    n = max(0, min(next, 10))     # 0: the remote only wants what is on now
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
        nxt = [slot_public(s) for s in next_programmes(conn, ch["id"], after, n)] if n else []
        if current and cur["kind"] != "programme":
            # During an ad break, show the programme that follows as "now".
            prog = conn.execute(SLOT_QUERY + " WHERE s.channel_id = ? AND s.start_ts <= ? AND s.kind = 'programme'"
                                " ORDER BY s.start_ts DESC LIMIT 1", (ch["id"], now)).fetchone()
            current["break"] = True
            current["previous_programme"] = slot_public(prog) if prog else None
        out.append({"channel": ch, "now": current, "next": nxt})
    player = player_public(request.app.state.player_state, auth.has_admin(request, conn))
    return {"ts": now, "channels": out, "player": player}


@router.get("/api/schedule")
def api_schedule(conn: sqlite3.Connection = Depends(get_conn), start: int | None = None,
                 end: int | None = None, channel: int | None = None, ads: int = 0,
                 bands: int = 0, replay: int = 1):
    now = now_ts()
    start = start if start is not None else now - 3600
    end = end if end is not None else start + 6 * 3600
    end = min(end, start + MAX_SCHEDULE_WINDOW)
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
    if not bands:
        slots = collapse_blocks(slots, feature_seconds(conn))
    return {"ts": now, "start": start, "end": end, "channels": enabled_channels(conn), "slots": slots}


@router.get("/api/schedule/day/{day}")
def api_schedule_day(day: str, conn: sqlite3.Connection = Depends(get_conn), ads: int = 0, bands: int = 0):
    settings = all_settings(conn)
    tz = tz_of(conn)
    try:
        d = parse_day(day)
    except ValueError as exc:
        raise HTTPException(400, "day must be YYYY-MM-DD") from exc
    day_start, day_end, next_start = day_bounds(d, settings, tz)
    q = SLOT_QUERY + " WHERE s.day = ?"
    if not ads:
        q += " AND s.kind IN ('programme', 'filler')"
    q += " ORDER BY s.channel_id, s.start_ts"
    slots = [slot_public(r) for r in conn.execute(q, (day,))]
    if not bands:
        slots = collapse_blocks(slots, feature_seconds(conn))
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
def api_player(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return player_public(request.app.state.player.state(), auth.has_admin(request, conn))


@router.post("/api/player/key")
def api_player_key(request: Request, body: dict[str, Any] = Body(...)):
    key = str(body.get("key", "")).lower()
    if key not in REMOTE_KEYS:
        raise HTTPException(400, "key must be a remote control action")
    return request.app.state.player.key(key)


@router.post("/api/player/channel")
def api_player_channel(request: Request, body: dict[str, Any] = Body(...)):
    try:
        number = int(body["number"])
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, "number required") from exc
    if not 1 <= number <= 999:
        raise HTTPException(400, "number out of range")
    return request.app.state.player.call("channel", number=number)


@router.post("/api/player/volume")
def api_player_volume(request: Request, body: dict[str, Any] = Body(...)):
    try:
        volume = max(0, min(100, int(body["volume"])))
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, "volume required") from exc
    return request.app.state.player.call("volume", volume=volume)


# --- auth ------------------------------------------------------------------------------------

def _check_new_password(password: str) -> None:
    if len(password) < 6 or len(password) > 200:
        raise HTTPException(400, "password must be 6 to 200 characters")


@router.get("/api/auth")
def api_auth_status(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return {"password_set": auth.password_is_set(conn), "admin": auth.has_admin(request, conn)}


@router.post("/api/auth/setup")
def api_auth_setup(request: Request, response: Response, body: dict[str, Any] = Body(...),
                   conn: sqlite3.Connection = Depends(get_conn)):
    if auth.password_is_set(conn):
        raise HTTPException(409, "password already set")
    password = str(body.get("password", ""))
    _check_new_password(password)
    hashed = auth.hash_password(password)    # before the transaction: PBKDF2 takes a second on a Pi
    with tx(conn):
        # Checked again under the write lock: two first-run requests must not both win.
        if auth.password_is_set(conn):
            raise HTTPException(409, "password already set")
        set_setting(conn, "admin_password_hash", hashed)
    auth.set_session_cookie(request, response, conn)
    return {"ok": True}


@router.post("/api/auth/login")
def api_auth_login(request: Request, response: Response, body: dict[str, Any] = Body(...),
                   conn: sqlite3.Connection = Depends(get_conn)):
    ip = auth.client_ip(request)
    if not auth.login_allowed(ip):
        raise HTTPException(429, "too many attempts, try again in a few minutes")
    password = str(body.get("password", ""))
    stored = get_setting(conn, "admin_password_hash")
    if not auth.verify_password(password, stored):
        raise HTTPException(401, "wrong password")
    auth.forget_attempts(ip)
    if auth.needs_rehash(stored):
        with tx(conn):
            set_setting(conn, "admin_password_hash", auth.hash_password(password))
    auth.set_session_cookie(request, response, conn)
    return {"ok": True}


@router.post("/api/auth/logout")
def api_auth_logout(response: Response):
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


@router.post("/api/auth/password")
def api_auth_password(request: Request, response: Response, body: dict[str, Any] = Body(...),
                      conn: sqlite3.Connection = Depends(get_conn)):
    auth.require_admin(request, conn)
    stored = get_setting(conn, "admin_password_hash")
    if stored:
        # Limited like a login: a borrowed session must not be a way to guess the password.
        ip = auth.client_ip(request)
        if not auth.login_allowed(ip):
            raise HTTPException(429, "too many attempts, try again in a few minutes")
        if not auth.verify_password(str(body.get("current", "")), stored):
            raise HTTPException(401, "current password is wrong")
        auth.forget_attempts(ip)
    new = str(body.get("password", ""))
    _check_new_password(new)
    hashed = auth.hash_password(new)
    with tx(conn):
        set_setting(conn, "admin_password_hash", hashed)
        auth.rotate_secret(conn)
    # Every other session is now invalid; the caller keeps working with a fresh cookie.
    auth.set_session_cookie(request, response, conn)
    return {"ok": True}


# --- events -----------------------------------------------------------------------------------

def _viewer_is_admin(request: Request) -> bool:
    # Decided once per stream, on a connection closed before streaming starts (a
    # yield-dependency would hold a database handle open for the life of the stream).
    conn = dbm.connect(request.app.state.cfg.db_path)
    try:
        return auth.has_admin(request, conn)
    finally:
        conn.close()


@router.get("/api/events")
async def api_events(request: Request):
    bus = request.app.state.bus
    if bus.is_full():
        return JSONResponse(status_code=503, content={"detail": "too many open event streams"})
    admin = await run_in_threadpool(_viewer_is_admin, request)

    def visible(msg: dict[str, Any]) -> dict[str, Any] | None:
        if admin:
            return msg
        if msg["event"] not in PUBLIC_EVENTS:
            return None
        if msg["event"] == "player":
            return {**msg, "data": player_public(msg["data"], False)}
        return msg

    async def gen():
        # Subscribing here rather than before the response is built ties the queue's lifetime
        # to the generator: a stream cancelled before its first chunk never held a queue.
        q = bus.subscribe()
        if q is None:
            return    # lost the race for the last place; the client retries later
        try:
            yield format_sse({"event": "hello", "data": {"ts": now_ts(),
                                                          "player": player_public(request.app.state.player_state, admin)}})
            for msg in list(bus.last.values()):
                shown = visible(msg)
                if shown:
                    yield format_sse(shown)
            while not await request.is_disconnected():
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                shown = visible(msg)
                if shown:
                    yield format_sse(shown)
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
