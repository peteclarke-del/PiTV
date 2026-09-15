"""Pick per-file mpv decode settings for the Raspberry Pi 4."""

from __future__ import annotations

from functools import cache
from typing import Any

from ..db import DEFAULT_SETTINGS

PI_HW_CODECS = {"h264", "hevc"}  # what the Pi 4's V4L2 decoder handles; everything else is software


def decode_options(media: dict[str, Any] | None, on_pi: bool, settings: dict[str, Any]) -> dict[str, Any]:
    """Return mpv properties to set before loading a file.

    On the Pi 4, H.264 and HEVC go through the V4L2 hardware decoder (zero-copy drm-prime
    first, v4l2m2m-copy as fallback). Everything else (MPEG-2, VC-1, DivX) is software.
    Deinterlacing is only switched on for files the library index reports as interlaced."""
    media = media or {}
    if not on_pi:
        hw = "auto-safe"
    elif media.get("vcodec") in PI_HW_CODECS:
        hw = settings.get("pi_hwdec") or DEFAULT_SETTINGS["pi_hwdec"]
    else:
        hw = "no"
    return {"hwdec": hw, "deinterlace": bool(media.get("interlaced"))}


@cache
def board_model() -> str:
    """The board name from the device tree, or "" where there is none (a desktop). Read once:
    the board does not change while the process runs."""
    try:
        with open("/proc/device-tree/model", "rb") as f:
            return f.read().decode("utf-8", errors="replace").rstrip("\x00").strip()
    except OSError:
        return ""


def is_raspberry_pi() -> bool:
    return "Raspberry Pi" in board_model()
