"""Pick per-file mpv decode settings for the Raspberry Pi 4."""

from __future__ import annotations

from functools import cache
from typing import Any

from ..db import DEFAULT_SETTINGS

PI_HW_CODECS = {"h264", "hevc"}  # what the Pi 4's V4L2 decoder handles; everything else is software
# The tallest picture of each codec the Pi 4 plays without dropping frames. H.264 and HEVC are
# the hardware decoder's own limits. The older codecs are decoded in software, which a Pi 4
# does comfortably at standard definition and not above it.
PI_PLAYS_UP_TO = {"h264": 1080, "hevc": 2160, "mpeg4": 576, "mpeg2video": 576, "mpeg1video": 576}


def pi_can_play(media: dict[str, Any]) -> bool:
    """Whether the Pi 4 can play this file as it is, so it is copied into the cache and not
    re-encoded.

    The test is what the Pi can play, not what suits the screen. A 1080p film on a 576 line
    tube was once re-encoded for being too tall, and a day of seven channels then held ninety
    hours of programme to transcode, which no Pi can do in a night; the Pi decodes that film in
    hardware and scales it as it plays. Interlaced material in a software codec is the
    exception: decoding and deinterlacing it together is more than the Pi has to spare, so it
    is re-encoded progressive, once. An unknown height is given the benefit of the doubt."""
    limit = PI_PLAYS_UP_TO.get(media.get("vcodec") or "")
    if limit is None or (media.get("height") or 0) > limit:
        return False
    return media.get("vcodec") in PI_HW_CODECS or not media.get("interlaced")


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
