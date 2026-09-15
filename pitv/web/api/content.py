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
