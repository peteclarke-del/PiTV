"""ffprobe wrapper with a database cache keyed on path, size and mtime."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..db import now_ts
from ..player.hwdec import PI_HW_CODECS

_INTERLACED_FIELD_ORDERS = {"tt", "bb", "tb", "bt"}


@dataclass
class ProbeResult:
    duration: float | None
    vcodec: str | None
    acodec: str | None
    width: int | None
    height: int | None
    interlaced: bool

    @property
    def hwdec(self) -> bool:
        return (self.vcodec or "") in PI_HW_CODECS


def ffprobe(path: Path, binary: str = "ffprobe", timeout: int = 60) -> ProbeResult | None:
    cmd = [binary, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
           str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return None

    duration = None
    fmt = data.get("format") or {}
    if fmt.get("duration"):
        try:
            duration = float(fmt["duration"])
        except ValueError:
            duration = None
    vcodec = acodec = None
    width = height = None
    interlaced = False
    for s in data.get("streams") or []:
        if s.get("codec_type") == "video" and vcodec is None and s.get("disposition", {}).get("attached_pic", 0) == 0:
            vcodec = s.get("codec_name")
            width, height = s.get("width"), s.get("height")
            interlaced = s.get("field_order") in _INTERLACED_FIELD_ORDERS
            if duration is None and s.get("duration"):
                try:
                    duration = float(s["duration"])
                except ValueError:
                    pass
        elif s.get("codec_type") == "audio" and acodec is None:
            acodec = s.get("codec_name")
    return ProbeResult(duration, vcodec, acodec, width, height, interlaced)


def probe_cached(conn: sqlite3.Connection, path: Path, size: int, mtime: int,
                 binary: str = "ffprobe") -> ProbeResult | None:
    row = conn.execute("SELECT * FROM probe_cache WHERE path = ?", (str(path),)).fetchone()
    if row and row["size"] == size and row["mtime"] == mtime:
        return ProbeResult(row["duration"], row["vcodec"], row["acodec"], row["width"],
                           row["height"], bool(row["interlaced"]))
    result = ffprobe(path, binary)
    if result is None:
        return None
    conn.execute(
        "INSERT INTO probe_cache(path, size, mtime, duration, vcodec, acodec, width, height,"
        " interlaced, probed_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,"
        " duration=excluded.duration, vcodec=excluded.vcodec, acodec=excluded.acodec,"
        " width=excluded.width, height=excluded.height, interlaced=excluded.interlaced,"
        " probed_at=excluded.probed_at",
        (str(path), size, mtime, result.duration, result.vcodec, result.acodec, result.width,
         result.height, int(result.interlaced), now_ts()))
    return result
