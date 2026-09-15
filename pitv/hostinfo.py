"""Host details in the shape both applications report (docs/CONTENT_CONTRACT.md, section 7).

PiTV builds its own here; pitv_content serves the same document at `GET /api/system`, so the
admin shows the two side by side whether or not they share a machine. Every reading is
optional: a field that cannot be read on this platform is None rather than an error."""

from __future__ import annotations

import os
import platform
import socket
from pathlib import Path
from typing import Any

from .player.hwdec import is_raspberry_pi


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _model() -> str:
    """The board name on a Pi (from the device tree), else the OS and architecture."""
    model = (_read("/proc/device-tree/model") or "").rstrip("\x00")
    return model or f"{platform.system()} {platform.machine()}"


def _uptime() -> int | None:
    raw = _read("/proc/uptime")
    try:
        return int(float(raw.split()[0])) if raw else None
    except ValueError:
        return None


def _temperature() -> float | None:
    raw = _read("/sys/class/thermal/thermal_zone0/temp")
    try:
        return round(int(raw) / 1000, 1) if raw else None
    except ValueError:
        return None


def _memory() -> dict[str, int] | None:
    fields = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        key, _, value = line.partition(":")
        if key in ("MemTotal", "MemAvailable") and value.split():
            fields[key] = int(value.split()[0]) * 1024
    if len(fields) < 2:
        return None
    return {"total": fields["MemTotal"], "available": fields["MemAvailable"]}


def host_info(version: str, tools: dict[str, str | None]) -> dict[str, Any]:
    return {
        "version": version, "python": platform.python_version(), "tools": tools,
        "hostname": socket.gethostname(), "model": _model(), "pi": is_raspberry_pi(),
        "uptime_s": _uptime(), "load": [round(x, 2) for x in os.getloadavg()] if hasattr(os, "getloadavg") else None,
        "temperature_c": _temperature(), "memory": _memory(),
    }
