"""The screens PiTV can drive, and what each asks of the player and of pitv_content.

One setting, `display_profile`, names the set: a tube or a flat panel, PAL, NTSC or HD, 4:3 or
16:9. From it follow the frame PiTV plays and pitv_content encodes to, the HDMI mode the
player asks for, the screen shape mpv is told, how much of the edge the graphics keep clear,
and the best source worth fetching.

A standard definition screen (576 or 480 lines) takes sources up to 720p. That is already more
than the screen shows, what PiTV fetches is television and music of the 1960s to the 2000s
with no true HD master behind it, and an H.264 source of 720 lines is one pitv_content files
without re-encoding it, where a 1080p source is a larger download and three times the disk for
a picture nobody can tell apart. An HD screen takes sources up to two rungs above the target on the HD ladder, so the
downscale has detail to work from; at 4K the ceiling is the target itself. The Pi 4 decodes H.264 in hardware only
up to 1080p, so the 4K profile encodes to HEVC, which it decodes to 2160p."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT = "crt_pal"
HD_LADDER = (720, 1080, 1440, 2160)   # the heights better sources come in, above standard definition
FRAME_RATE = {"pal": 25, "ntsc": 30000 / 1001}
BITRATE_KBPS = {480: 3000, 576: 4000, 720: 6000, 1080: 10000, 2160: 25000}


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    width: int          # square-pixel frame PiTV plays and pitv_content encodes to
    height: int
    aspect: str         # the physical screen shape mpv is told
    crt: bool           # a tube hides the picture's edges; a flat panel shows all of it
    system: str         # pal | ntsc | hd: sets the output refresh and the encoded frame rate
    drm_mode: str       # the HDMI mode the player asks for (mpv --drm-mode)

    @property
    def max_source_height(self) -> int:
        if self.height < HD_LADDER[0]:
            return HD_LADDER[0]
        above = [h for h in HD_LADDER if h > self.height]
        return above[1] if len(above) >= 2 else max(self.height, above[0] if above else self.height)

    @property
    def vcodec(self) -> str:
        return "hevc" if self.height > 1080 else "h264"

    @property
    def osd(self) -> dict[str, float]:
        """On-screen graphics: overscan-safe margin and text size for this kind of screen."""
        if self.crt:
            return {"osd_safe_margin": 0.07, "osd_scale": 1.25}
        return {"osd_safe_margin": 0.03, "osd_scale": 1.1 if self.height <= 576 else 1.0}


PROFILES: tuple[Profile, ...] = (
    Profile("crt_pal", "CRT (PAL)", 768, 576, "4:3", True, "pal", "720x576@50"),
    Profile("crt_pal_wide", "CRT (PAL widescreen)", 1024, 576, "16:9", True, "pal", "720x576@50"),
    Profile("crt_ntsc", "CRT (NTSC)", 640, 480, "4:3", True, "ntsc", "720x480@60"),
    Profile("crt_ntsc_wide", "CRT (NTSC widescreen)", 854, 480, "16:9", True, "ntsc", "720x480@60"),
    Profile("lcd_pal", "LCD (PAL)", 768, 576, "4:3", False, "pal", "720x576@50"),
    Profile("lcd_pal_wide", "LCD (PAL widescreen)", 1024, 576, "16:9", False, "pal", "720x576@50"),
    Profile("lcd_ntsc", "LCD (NTSC)", 640, 480, "4:3", False, "ntsc", "720x480@60"),
    Profile("lcd_ntsc_wide", "LCD (NTSC widescreen)", 854, 480, "16:9", False, "ntsc", "720x480@60"),
    Profile("lcd_720p", "LCD (HD 720p widescreen)", 1280, 720, "16:9", False, "hd", "1280x720@50"),
    Profile("lcd_1080p", "LCD (HD 1080p widescreen)", 1920, 1080, "16:9", False, "hd", "1920x1080@50"),
    # 2160p above 30 Hz needs hdmi_enable_4kp60 in config.txt; 25 Hz suits 25 fps material without it.
    Profile("lcd_2160p", "LCD (4K widescreen)", 3840, 2160, "16:9", False, "hd", "3840x2160@25"),
)
BY_ID = {p.id: p for p in PROFILES}
CHOICES = [{"value": p.id, "label": p.label} for p in PROFILES]


def profile(settings: dict[str, Any]) -> Profile:
    return BY_ID.get(settings.get("display_profile") or DEFAULT, BY_ID[DEFAULT])


def content_profile(settings: dict[str, Any]) -> dict[str, Any]:
    """The manifest's `profile` (contract section 2): what pitv_content encodes to, and the best
    source it should start from."""
    p = profile(settings)
    return {"name": p.id, "label": p.label, "width": p.width, "height": p.height, "aspect": p.aspect,
            "max_source_height": p.max_source_height, "vcodec": p.vcodec, "acodec": "aac",
            "max_bitrate_kbps": BITRATE_KBPS[p.height if p.height in BITRATE_KBPS else 2160],
            "frame_rate": FRAME_RATE.get(p.system), "deinterlace": "if_interlaced"}


def implied_settings(profile_id: str) -> dict[str, Any]:
    """Settings a new screen profile brings with it; each can still be adjusted afterwards."""
    p = BY_ID[profile_id]
    return {"display_aspect": p.aspect, **p.osd}


def preview_geometry(settings: dict[str, Any], limit: tuple[int, int] = (1280, 720)) -> str:
    """The desktop preview window: the profile's frame, scaled down to fit a desktop screen."""
    p = profile(settings)
    scale = min(1.0, limit[0] / p.width, limit[1] / p.height)
    return f"{round(p.width * scale)}x{round(p.height * scale)}"
