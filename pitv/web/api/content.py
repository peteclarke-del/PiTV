"""Manifest/report endpoints used by pitv_content (see pitv/content.py)."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Body, Depends, Request

from ...content import apply_report, manifest
from .deps import admin_conn

router = APIRouter(prefix="/api/content", dependencies=[Depends(admin_conn)])


@router.get("/manifest")
def get_manifest(conn: sqlite3.Connection = Depends(admin_conn), days: int = 1):
    return manifest(conn, days=max(1, min(days, 7)))


@router.post("/report")
def post_report(request: Request, body: dict[str, Any] = Body(...), conn: sqlite3.Connection = Depends(admin_conn)):
    result = apply_report(conn, body)
    if result["wanted_done"]:
        # New files: scan the acquired folders so they become schedulable.
        from .admin import _scan_job
        _scan_job(request, None, "Scan after pitv_content report")
    request.app.state.bus.publish_threadsafe("library", {"changed": True})
    return {"ok": True, **result}


@router.post("/make-room")
def make_room(request: Request, body: dict[str, Any] = Body(default={}), conn: sqlite3.Connection = Depends(admin_conn)):
    """Evict least-recently-used cache files so `bytes` fit (called by pitv_content before a big job)."""
    from pathlib import Path
    from ...db import all_settings
    from ...player.cache import MediaCache
    settings = all_settings(conn)
    cache_dir = settings.get("cache_dir") or ""
    if not cache_dir:
        return {"ok": False, "error": "no cache_dir configured"}
    cache = MediaCache(Path(cache_dir), int(float(settings.get("cache_max_gb", 0)) * 1024 ** 3))
    from ...content import manifest as _manifest
    cache.protect({Path(i["target"]).name for i in _manifest(conn, days=1)["items"]})
    free = cache.make_room(int(body.get("bytes", 0)))
    return {"ok": True, "free_bytes": free}


@router.post("/readiness")
def readiness(body: dict[str, Any] = Body(default={}), conn: sqlite3.Connection = Depends(admin_conn)):
    """Check that everything scheduled through tomorrow is playable; substitute what is missing."""
    from ...readiness import check
    return check(conn, days=int(body.get("days", 1)), substitute=bool(body.get("substitute", True)))


def _cmd(args: list[str], timeout: float = 5) -> tuple[int, str]:
    import subprocess
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return out.returncode, (out.stdout or out.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


@router.get("/tool")
def tool_status(conn: sqlite3.Connection = Depends(admin_conn)):
    """State of the pitv_content support app: its status file, service/timer state, next run."""
    import json
    from pathlib import Path
    from ...db import all_settings
    settings = all_settings(conn)
    cache = settings.get("cache_dir") or ""
    status_path = Path(cache) / "pitv_content.status.json" if cache else None
    status = None
    if status_path and status_path.exists():
        try:
            status = json.loads(status_path.read_text())
        except (OSError, ValueError) as exc:
            status = {"error": f"unreadable status file: {exc}"}
    rc, active = _cmd(["systemctl", "is-active", "pitv-content.service"])
    rc2, timer = _cmd(["systemctl", "is-active", "pitv-content.timer"])
    _, timers = _cmd(["systemctl", "list-timers", "pitv-content.timer", "--no-pager", "--no-legend"])
    rc3, ver = _cmd(["/opt/pitv-content/.venv/bin/pitv-content", "--version"])
    running_marker = Path(cache) / ".pitv_content.running" if cache else None
    return {
        "installed": rc3 == 0 or (status is not None),
        "version": ver if rc3 == 0 else None,
        "service": active or "unknown", "timer": timer or "unknown", "timers": timers,
        "running_marker": bool(running_marker and running_marker.exists()),
        "status_file": str(status_path) if status_path else None, "status": status,
        "log_file": str(Path(cache) / "logs" / "pitv-content.log") if cache else None,
        "reports": [dict(r) for r in conn.execute("SELECT id, started_at, finished_at, status, summary FROM run_log"
                                                  " WHERE kind = 'content' ORDER BY id DESC LIMIT 10")],
    }


@router.post("/tool/run")
def tool_run():
    """Start a pitv_content run now (systemd; the installer grants this via sudoers)."""
    rc, out = _cmd(["sudo", "-n", "systemctl", "start", "pitv-content.service"], timeout=10)
    if rc != 0:
        return {"ok": False, "error": out or "could not start pitv-content.service"}
    return {"ok": True}


@router.get("/tool/log")
def tool_log(conn: sqlite3.Connection = Depends(admin_conn), lines: int = 300, q: str = "", level: str = ""):
    from pathlib import Path
    from ...db import all_settings
    from ...logsetup import tail
    cache = all_settings(conn).get("cache_dir") or ""
    p = Path(cache) / "logs" / "pitv-content.log"
    entries = tail(p, max(10, min(lines, 5000)), q, level.upper()) if cache else []
    return {"name": "pitv-content", "path": str(p) if cache else None, "exists": p.exists() if cache else False, "lines": entries}


# --- proxy to pitv_content's own local API (settings, run/cancel, providers, catalogue, jobs, log) ---

_PROXY_ALLOWED = {"status", "settings", "run", "cancel", "log", "providers", "catalogue", "jobs"}


def _tool_request(base: str, method: str, path: str, query: str = "", body: dict[str, Any] | None = None,
                  timeout: float = 15) -> tuple[int, Any]:
    import json
    import urllib.error
    import urllib.request
    url = f"{base.rstrip('/')}/api/{path}" + (f"?{query}" if query else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"error": raw[:500] or exc.reason}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return 503, {"error": f"pitv_content API unavailable at {base}: {exc}", "offline": True}


@router.api_route("/tool/api/{path:path}", methods=["GET", "PUT", "POST"])
async def tool_proxy(path: str, request: Request):
    """Forward to pitv_content's local API so its settings, providers, catalogue, jobs and
    log are controllable from this admin. Only known paths; JSON only; loopback by default."""
    from fastapi.responses import JSONResponse
    from ...db import all_settings, connect
    from ..auth import require_admin
    conn = connect(request.app.state.cfg.db_path)
    try:
        require_admin(request, conn)
        base = all_settings(conn).get("content_tool_url") or "http://127.0.0.1:8081"
    finally:
        conn.close()
    head = path.split("/", 1)[0]
    if head not in _PROXY_ALLOWED:
        return JSONResponse(status_code=404, content={"error": "unknown pitv_content endpoint"})
    body = None
    if request.method in ("PUT", "POST"):
        try:
            body = await request.json()
        except ValueError:
            body = {}
    status, payload = _tool_request(base, request.method, path, request.url.query, body)
    return JSONResponse(status_code=status if status < 500 else (503 if payload.get("offline") else status), content=payload)
