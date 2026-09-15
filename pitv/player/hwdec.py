"""Pick per-file mpv decode settings for the Raspberry Pi 4."""

from __future__ import annotations

from typing import Any

PI_HW_CODECS = {"h264", "hevc"}  # what the Pi 4's V4L2 decoder handles; everything else is software


def decode_options(media: dict[str, Any] | None, on_pi: bool, settings: dict[str, Any]) -> dict[str, Any]:
    """Return mpv properties to set before loading a file.

    On the Pi 4, H.264 and HEVC go through the V4L2 hardware decoder (zero-copy drm-prime
    first, v4l2m2m-copy as fallback). Everything else (MPEG-2, VC-1, DivX) is software.
    Deinterlacing is only switched on for files the library index reports as interlaced."""
    if not on_pi:
        return {"hwdec": "auto-safe", "deinterlace": bool(media and media.get("interlaced"))}
    vcodec = (media or {}).get("vcodec") or ""
    if vcodec in PI_HW_CODECS:
        hw = settings.get("pi_hwdec", "drm-prime,v4l2m2m-copy")
    else:
        hw = "no"
    return {"hwdec": hw, "deinterlace": bool(media and media.get("interlaced"))}


def is_raspberry_pi() -> bool:
    try:
        with open("/proc/device-tree/model", "rb") as f:
            return b"Raspberry Pi" in f.read()
    except OSError:
        return False
