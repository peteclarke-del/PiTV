"""Re-encode to H.264 with the Pi 4's hardware encoder (or libx264 on a desktop)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable

Progress = Callable[[str, float], None]


def transcode(src: Path, dest: Path, duration: float | None, on_pi: bool, max_height: int,
              bitrate_kbps: int, progress: Progress, should_abort: Callable[[], bool] | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part.mp4")
    scale = f"scale=-2:'min(ih,{max_height})'"
    if on_pi:
        video = ["-c:v", "h264_v4l2m2m", "-b:v", f"{bitrate_kbps}k", "-pix_fmt", "yuv420p"]
    else:
        video = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p"]
    cmd = ["nice", "-n", "19", "ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf", f"yadif=deint=interlaced,{scale}",
           *video, "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-movflags", "+faststart",
           "-progress", "pipe:1", "-nostats", str(tmp)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout
    for line in proc.stdout:
        if should_abort and should_abort():
            proc.kill()
            tmp.unlink(missing_ok=True)
            raise InterruptedError("transcode aborted")
        m = re.match(r"out_time_ms=(\d+)", line)
        if m and duration:
            frac = min(1.0, int(m.group(1)) / 1_000_000 / duration)
            progress(f"encoding {frac * 100:.0f}%", frac)
    if proc.wait() != 0:
        err = proc.stderr.read()[-500:] if proc.stderr else ""
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed: {err.strip()}")
    tmp.rename(dest)
    return dest
