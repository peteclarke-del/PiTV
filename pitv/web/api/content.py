"""Manifest/report endpoints used by pitv_content, and the admin Content tab's view of the
tool: status file, service state, log, and a proxy to its own local API (see pitv/content.py)."""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ... import tool_client
from ...content import apply_report, manifest, protect_manifest
from ...db import DEFAULT_SETTINGS, all_settings, get_setting
from ...logsetup import tail
from ...player.cache import MediaCache
from ...readiness import check as readiness_check
from .deps import admin_conn, run_cmd
from .services import CONTENT_RUN, CONTENT_TIMER, systemd_state

router = APIRouter(prefix="/api/content", dependencies=[Depends(admin_conn)])



@router.get("/manifest")
def get_manifest(conn: sqlite3.Connection = Depends(admin_conn), days: int = 1):
    return manifest(conn, days=max(1, min(days, 7)))


@router.post("/report")
def post_report(request: Request, body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    result = apply_report(conn, body)
    request.app.state.bus.publish_threadsafe("library", {"changed": True})
    request.app.state.bus.publish_threadsafe("schedule", {"changed": True})
    request.app.state.player.call("schedule-changed")   # slots may have been resized or bound
    return {"ok": True, **result}


@router.post("/make-room")
def make_room(body: dict[str, Any] = Body(default={}), conn: sqlite3.Connection = Depends(admin_conn)):
    """Evict least-recently-used cache files so `bytes` fit (called by pitv_content before a big job)."""
    cache = MediaCache.from_settings(all_settings(conn))
    if not cache.enabled:
        return {"ok": False, "error": "no cache_dir configured"}
    try:
        needed = max(0, int(body.get("bytes", 0)))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "bytes must be a number") from exc
    protect_manifest(conn, cache)
    return {"ok": True, "free_bytes": cache.make_room(needed)}


@router.post("/readiness")
def readiness(request: Request, body: dict[str, Any] = Body(default={}), conn: sqlite3.Connection = Depends(admin_conn)):
    """Check that everything scheduled through tomorrow is playable; substitute what is missing."""
    try:
        days = max(1, min(int(body.get("days", 1)), 7))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "days must be a number") from exc
    result = readiness_check(conn, days=days, substitute=bool(body.get("substitute", True)))
    if result["substituted"]:
        request.app.state.player.call("schedule-changed")
    return result


@router.get("/tool")
def tool_status(conn: sqlite3.Connection = Depends(admin_conn)):
    """State of the pitv_content support app: its status file, service/timer state, next run."""
    cache = MediaCache.from_settings(all_settings(conn))
    status = None
    if cache.status_file and cache.status_file.exists():
        try:
            status = json.loads(cache.status_file.read_text())
        except (OSError, ValueError) as exc:
            status = {"error": f"unreadable status file: {exc}"}
    units = systemd_state([CONTENT_RUN, CONTENT_TIMER])

    def state(unit: str) -> str:
        props = units.get(unit) or {}
        return "not installed" if props.get("LoadState") == "not-found" else props.get("ActiveState") or "unknown"

    loaded = any((units.get(u) or {}).get("LoadState") == "loaded" for u in (CONTENT_RUN, CONTENT_TIMER))
    return {
        "installed": loaded or status is not None,
        "version": (status or {}).get("tool"),
        "service": state(CONTENT_RUN), "timer": state(CONTENT_TIMER),
        "next_run_ts": (status or {}).get("next_timer_ts"),
        "running_marker": cache.content_tool_running(),
        "status_file": str(cache.status_file) if cache.status_file else None, "status": status,
        "log_file": str(cache.log_file) if cache.log_file else None,
        "reports": [dict(r) for r in conn.execute("SELECT id, started_at, finished_at, status, summary FROM run_log"
                                                  " WHERE kind = 'content' ORDER BY id DESC LIMIT 10")],
    }


@router.post("/tool/run")
def tool_run(conn: sqlite3.Connection = Depends(admin_conn)):
    """Start a pitv_content cache run now: through its API when it is up, else systemd."""
    base = get_setting(conn, "content_tool_url") or DEFAULT_SETTINGS["content_tool_url"]
    status, payload = tool_client.request(base, "POST", "run", body={"mode": "cache"}, timeout=10)
    if status < 500 and not payload.get("offline"):
        if status == 409:
            return {"ok": False, "error": payload.get("error") or "a run is already active"}
        return {"ok": status < 400, **{k: v for k, v in payload.items() if k != "ok"}}
    rc, _, err = run_cmd(["sudo", "-n", "systemctl", "start", CONTENT_RUN], timeout=10)
    if rc != 0:
        return {"ok": False, "error": err or f"could not start {CONTENT_RUN}"}
    return {"ok": True, "via": "systemd"}


@router.get("/tool/log")
def tool_log(conn: sqlite3.Connection = Depends(admin_conn), lines: int = 300, q: str = "", level: str = ""):
    """Tail of pitv_content's log file on the cache drive; same shape as /api/logs/{name}."""
    p = MediaCache.from_settings(all_settings(conn)).log_file
    entries = tail(p, max(10, min(lines, 5000)), q, level.upper()) if p else []
    return {"name": "pitv-content", "path": str(p) if p else None, "exists": bool(p and p.exists()), "lines": entries}


# --- proxy to pitv_content's own local API (settings, run/cancel, providers, catalogue, jobs, log) ---

_PROXY_ALLOWED = {"status", "system", "settings", "run", "cancel", "log", "providers", "catalogue", "jobs"}


def _proxy_path(path: str) -> str | None:
    """The forwarded path, re-encoded segment by segment, or None when it is not one of the
    known pitv_content endpoints or tries to climb out of them ('..', empty segments)."""
    segments = path.split("/")
    if len(path) > 500 or segments[0] not in _PROXY_ALLOWED or any(seg in ("", ".", "..") for seg in segments):
        return None
    return "/".join(urllib.parse.quote(seg, safe="") for seg in segments)


@router.api_route("/tool/api/{path:path}", methods=["GET", "PUT", "POST"])
async def tool_proxy(path: str, request: Request, conn: sqlite3.Connection = Depends(admin_conn)):
    """Forward to pitv_content's local API so its settings, providers, catalogue, jobs and
    log are controllable from this admin. Only known paths; JSON only; loopback by default."""
    base = get_setting(conn, "content_tool_url") or DEFAULT_SETTINGS["content_tool_url"]
    target = _proxy_path(path)
    if target is None:
        return JSONResponse(status_code=404, content={"error": "unknown pitv_content endpoint"})
    head = path.split("/", 1)[0]
    body = None
    if request.method in ("PUT", "POST"):
        try:
            body = await request.json()
        except ValueError:
            body = {}
    # urllib blocks; keep it off the event loop so the SSE stream and other requests carry on.
    status, payload = await run_in_threadpool(tool_client.request, base, request.method, target,
                                              request.url.query.replace("#", "%23"), body)
    offline = isinstance(payload, dict) and payload.get("offline")
    if head == "log" and isinstance(payload, dict) and not offline:
        # Same shape as /api/logs/{name} so the Logs page can show either.
        payload.setdefault("path", None)
        payload.setdefault("exists", True)
    return JSONResponse(status_code=503 if offline else status, content=payload)


def tool_catalogue(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """pitv_content's catalogue (titles it can fetch), or [] when it is not running."""
    base = all_settings(conn).get("content_tool_url") or "http://127.0.0.1:8081"
    status, payload = tool_client.request(base, "GET", "catalogue", timeout=3)
    if status != 200 or not isinstance(payload, list):
        return []
    return [c for c in payload if isinstance(c, dict)]
