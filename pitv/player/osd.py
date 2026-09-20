"""On-screen graphics: channel badge, volume bar, guide, test card. Rendered with Pillow
into BGRA files that mpv composites with `overlay-add`."""

from __future__ import annotations

import random
from datetime import datetime, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeMonoBold.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf",
]

OVERLAY_BADGE, OVERLAY_VOLUME, OVERLAY_GUIDE, OVERLAY_MESSAGE, OVERLAY_STATIC = 1, 2, 3, 4, 5

# Bounds for the admin's overscan margin and text scale: outside them the layout arithmetic
# yields empty or negative boxes and every overlay fails to render.
MARGIN_RANGE = (0.0, 0.2)
TEXT_SCALE_RANGE = (0.5, 3.0)

Rendered = tuple[str, int, int, int, int]   # (file, width, height, x, y) for overlay-add
STATIC_FRAMES = 8                       # frames of snow kept for a channel change, where memory allows
STATIC_BUDGET_BYTES = 32 * 1024 * 1024  # a 1080p screen keeps three frames, a standard definition one all eight


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    return max(bounds[0], min(bounds[1], value))


@lru_cache(maxsize=16)   # a few sizes per layout; relayouts reuse them rather than reopening the font
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for cand in FONT_CANDIDATES:
        if Path(cand).exists():
            try:
                return ImageFont.truetype(cand, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _hex(colour: str | None) -> tuple[int, int, int]:
    try:
        c = (colour or "#ffffff").lstrip("#")
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return (255, 255, 255)


def _fit(d: ImageDraw.ImageDraw, text: str, font, max_px: int) -> str:
    """Truncate text with an ellipsis so it fits within max_px."""
    if d.textlength(text, font=font) <= max_px:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if d.textlength(text[:mid] + "…", font=font) <= max_px:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…"


def _wrap(d: ImageDraw.ImageDraw, text: str, font, max_px: int, max_lines: int) -> list[str]:
    """Word-wrap text to max_px; the last line takes an ellipsis if there is more."""
    lines: list[str] = []
    words = text.split()
    while words and len(lines) < max_lines:
        line = words.pop(0)
        while words and d.textlength(f"{line} {words[0]}", font=font) <= max_px:
            line = f"{line} {words.pop(0)}"
        lines.append(line)
    if words:
        lines[-1] = _fit(d, f"{lines[-1]} {' '.join(words)}", font, max_px)
    return [_fit(d, line, font, max_px) for line in lines]


def _hhmm(ts: float | None, tz: tzinfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M") if ts else "--:--"


class Renderer:
    """Draws overlays sized to the output. `margin` keeps a fraction of every edge clear
    (a CRT loses ~5-8% to overscan); `text_scale` enlarges type for small 4:3 sets. Times are
    shown in `tz`, the zone the schedule is built in."""

    def __init__(self, run_dir: Path, tz: tzinfo, width: int = 1280, height: int = 720,
                 margin: float = 0.07, text_scale: float = 1.0) -> None:
        self.run_dir = run_dir
        self.tz = tz
        self.width, self.height = width, height
        self.margin_frac = _clamp(margin, MARGIN_RANGE)
        self.text_scale = _clamp(text_scale, TEXT_SCALE_RANGE)
        self._layout()

    def _layout(self) -> None:
        self.mx, self.my = int(self.width * self.margin_frac), int(self.height * self.margin_frac)
        self.scale = (self.height / 720) * self.text_scale
        self.f_big = _font(int(44 * self.scale))
        self.f_med = _font(int(28 * self.scale))
        self.f_small = _font(int(22 * self.scale))
        self._static: Rendered | None = None

    def resize(self, width: int, height: int) -> None:
        if (width, height) != (self.width, self.height) and width > 0 and height > 0:
            self.width, self.height = width, height
            self._layout()

    def configure(self, margin: float, text_scale: float, tz: tzinfo) -> None:
        self.tz = tz
        margin, text_scale = _clamp(margin, MARGIN_RANGE), _clamp(text_scale, TEXT_SCALE_RANGE)
        if (margin, text_scale) != (self.margin_frac, self.text_scale):
            self.margin_frac, self.text_scale = margin, text_scale
            self._layout()

    @property
    def safe_width(self) -> int:
        return self.width - 2 * self.mx

    def _save(self, img: Image.Image, name: str) -> tuple[str, int, int]:
        path = self.run_dir / f"{name}.bgra"
        path.write_bytes(img.tobytes("raw", "BGRA"))
        return str(path), img.width, img.height

    # --- static ---------------------------------------------------------------------------

    def static(self) -> tuple[Rendered, int, int]:
        """Full-screen analogue snow for a channel change, as (overlay, frames, bytes per frame).

        A detuned set is never one still picture: the grain boils, a dark hum bar rolls down the
        tube, the line structure shows and the picture tears sideways where sync slips. Several
        frames are drawn once per screen size into one file, and the player steps mpv's overlay
        through them by offset, so a channel change costs no rendering. The frame count is what
        `STATIC_BUDGET_BYTES` allows at this screen size, never fewer than three; they sit in
        the run directory, which is memory on the Pi."""
        frame_bytes = self.width * self.height * 4
        frames = max(3, min(STATIC_FRAMES, STATIC_BUDGET_BYTES // frame_bytes))
        if self._static is None or not Path(self._static[0]).exists():
            rng = random.Random(self.width * 10007 + self.height)      # the same snow for the same screen
            grain = (max(1, self.width // 3), max(1, self.height // 3))
            lines = Image.new("L", grain, 255)
            draw = ImageDraw.Draw(lines)
            for y in range(0, grain[1], 2):
                draw.line([(0, y), (grain[0], y)], fill=205)          # every other line a little darker
            path = self.run_dir / "static.bgra"
            with path.open("wb") as out:
                for n in range(frames):
                    snow = Image.effect_noise(grain, rng.uniform(70, 100)).convert("L")
                    snow = ImageChops.multiply(snow, lines)
                    # The hum bar: a soft dark band, a fifth of the screen tall, rolling downwards.
                    bar_h = max(4, grain[1] // 5)
                    top = int((n / frames) * (grain[1] + bar_h)) - bar_h
                    shade = Image.new("L", grain, 255)
                    band = Image.linear_gradient("L").resize((grain[0], bar_h)).point(lambda v: 150 + abs(v - 128) * 105 // 128)
                    shade.paste(band, (0, top))
                    snow = ImageChops.multiply(snow, shade)
                    # Tearing: two or three thin bands pulled sideways, in a new place each frame.
                    for _ in range(rng.randint(2, 3)):
                        y, h = rng.randrange(grain[1]), rng.randint(2, max(3, grain[1] // 40))
                        strip = snow.crop((0, y, grain[0], min(grain[1], y + h)))
                        snow.paste(ImageChops.offset(strip, rng.randint(grain[0] // 12, grain[0] // 4), 0), (0, y))
                    full = snow.resize((self.width, self.height), Image.Resampling.NEAREST)
                    out.write(Image.merge("RGBA", (full, full, full, Image.new("L", full.size, 255))).tobytes("raw", "BGRA"))
            self._static = (str(path), self.width, self.height, 0, 0)
        return self._static, frames, frame_bytes

    # --- badge ---------------------------------------------------------------------------

    def badge(self, channel: dict[str, Any], now: dict[str, Any] | None, nxt: dict[str, Any] | None,
              position: float | None, behind_live: bool) -> Rendered:
        """`position` is the fraction of the programme already shown, for the progress bar."""
        s = self.scale
        w = self.safe_width if self.width < 1100 * s else min(int(self.width * 0.62), self.safe_width)
        h = int(190 * s)
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=int(12 * s), fill=(0, 0, 0, 200))
        col = _hex(channel.get("colour"))
        d.rounded_rectangle((int(14 * s), int(14 * s), int(120 * s), int(80 * s)), radius=int(8 * s), fill=col + (255,))
        num = str(channel.get("number", "?"))
        bbox = d.textbbox((0, 0), num, font=self.f_big)
        d.text((int(67 * s) - (bbox[2] - bbox[0]) // 2, int(18 * s)), num, font=self.f_big, fill=(0, 0, 0, 255))
        text_x = int(135 * s)
        text_w = w - text_x - int(16 * s)
        name_w = text_w
        if behind_live:
            label = "PAUSED/BEHIND"
            label_w = int(d.textlength(label, font=self.f_small))
            d.text((w - int(16 * s) - label_w, int(14 * s)), label, font=self.f_small, fill=(255, 0, 0, 255))
            name_w -= label_w + int(12 * s)   # the name is cut short rather than drawn under the label
        d.text((text_x, int(14 * s)), _fit(d, channel.get("name", ""), self.f_med, name_w), font=self.f_med, fill=(255, 255, 255, 255))
        if now:
            title = now.get("title", "")
            sub = now.get("subtitle", "")
            line = f"{_hhmm(now.get('start_ts'), self.tz)}–{_hhmm(now.get('end_ts'), self.tz)}  {title}"
            d.text((text_x, int(50 * s)), _fit(d, line, self.f_med, text_w), font=self.f_med, fill=(255, 255, 0, 255))
            if sub:
                d.text((text_x, int(84 * s)), _fit(d, sub, self.f_small, text_w), font=self.f_small, fill=(0, 255, 255, 255))
            if position is not None:
                frac = max(0.0, min(1.0, position))
                x0, x1, y = text_x, w - int(20 * s), int(116 * s)
                d.rectangle((x0, y, x1, y + int(8 * s)), fill=(70, 70, 70, 255))
                d.rectangle((x0, y, x0 + int((x1 - x0) * frac), y + int(8 * s)), fill=col + (255,))
        else:
            d.text((text_x, int(50 * s)), "No programme scheduled", font=self.f_med, fill=(255, 80, 80, 255))
        if nxt:
            d.text((text_x, int(140 * s)), _fit(d, f"Next {_hhmm(nxt.get('start_ts'), self.tz)}  {nxt.get('title', '')}", self.f_small, text_w),
                   font=self.f_small, fill=(180, 180, 180, 255))
        path, iw, ih = self._save(img, "badge")
        return path, iw, ih, self.mx, self.height - h - self.my

    # --- volume ---------------------------------------------------------------------------

    def volume(self, volume: int, muted: bool) -> Rendered:
        s = self.scale
        w, h = min(int(420 * s), self.safe_width), int(56 * s)
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=int(10 * s), fill=(0, 0, 0, 200))
        label = "MUTE" if muted else f"VOL {volume}"
        d.text((int(14 * s), int(12 * s)), label, font=self.f_med, fill=(255, 255, 0, 255))
        x0, x1, y = int(150 * s), w - int(16 * s), int(22 * s)
        d.rectangle((x0, y, x1, y + int(12 * s)), fill=(70, 70, 70, 255))
        if not muted:
            d.rectangle((x0, y, x0 + int((x1 - x0) * volume / 100), y + int(12 * s)), fill=(0, 255, 0, 255))
        path, iw, ih = self._save(img, "volume")
        return path, iw, ih, (self.width - w) // 2, self.height - h - self.my

    # --- message ---------------------------------------------------------------------------

    def message(self, text: str, sub: str = "") -> Rendered:
        """A caption box over the test card, wrapped to the overscan-safe width; it sits above
        the channel badge so the two do not collide."""
        s = self.scale
        w = min(int(self.width * 0.8), self.safe_width)
        pad = int(20 * s)
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        head = _wrap(probe, text, self.f_med, w - 2 * pad, 3)
        tail = _wrap(probe, sub, self.f_small, w - 2 * pad, 3) if sub else []
        lh_med, lh_small = (int(f.getbbox("Ag")[3] * 1.2) for f in (self.f_med, self.f_small))
        h = 2 * pad + len(head) * lh_med + (int(8 * s) + len(tail) * lh_small if tail else 0)
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=int(12 * s), fill=(0, 0, 0, 210))
        y = pad
        for line in head:
            d.text((pad, y), line, font=self.f_med, fill=(255, 255, 255, 255))
            y += lh_med
        y += int(8 * s)
        for line in tail:
            d.text((pad, y), line, font=self.f_small, fill=(0, 255, 255, 255))
            y += lh_small
        path, iw, ih = self._save(img, "message")
        return path, iw, ih, (self.width - w) // 2, max(self.my, int(self.height * 0.55) - h)

    # --- guide -----------------------------------------------------------------------------

    def guide(self, channels: list[dict[str, Any]], rows: dict[int, list[dict[str, Any]]],
              highlight: int, cursor: int, now: int) -> Rendered:
        """Teletext style guide. rows[channel_id] = [now, next, next+1, ...]. The highlighted
        channel takes four lines and the others two each; when they do not all fit (six
        channels on a 576-line set) the list scrolls to keep the highlight in view."""
        s = self.scale
        w = self.safe_width
        line_h = int(34 * s)
        header_h = int(52 * s)
        top_pad, bottom_pad = int(8 * s), int(30 * s)
        per_channel, highlight_lines = 2, 4
        max_h = self.height - 2 * self.my
        hl_idx = next((i for i, ch in enumerate(channels) if ch["id"] == highlight), 0)
        fit = max(0, (max_h - header_h - top_pad - bottom_pad) // line_h - highlight_lines) // per_channel
        first = max(0, min(hl_idx - fit // 2, len(channels) - 1 - fit))
        shown = channels[first:first + fit + 1]
        total_lines = sum(highlight_lines if ch["id"] == highlight else per_channel for ch in shown)
        h = min(header_h + top_pad + total_lines * line_h + bottom_pad, max_h)
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, w - 1, h - 1), fill=(0, 0, 0, 225))
        d.rectangle((0, 0, w - 1, header_h), fill=(0, 0, 255, 255))
        d.text((int(16 * s), int(10 * s)), "P100  PiTV GUIDE", font=self.f_med, fill=(255, 255, 0, 255))
        clock = datetime.fromtimestamp(now, self.tz).strftime("%a %d %b  %H:%M")
        cb = d.textbbox((0, 0), clock, font=self.f_med)
        d.text((w - (cb[2] - cb[0]) - int(16 * s), int(10 * s)), clock, font=self.f_med, fill=(255, 255, 255, 255))
        y = header_h + top_pad
        narrow = w < 1100 * s
        x_num, x_name, x_prog = int(16 * s), int(70 * s), int(190 * s) if narrow else int(300 * s)
        prog_w = w - x_prog - int(12 * s)
        for ch in shown:
            is_hl = ch["id"] == highlight
            items = rows.get(ch["id"], [])
            col = _hex(ch.get("colour"))
            if is_hl:
                d.rectangle((0, y - int(4 * s), w - 1, y + highlight_lines * line_h - int(6 * s)), fill=(30, 30, 30, 255))
            d.text((x_num, y), str(ch["number"]), font=self.f_med, fill=col + (255,))
            d.text((x_name, y), str(ch.get("short_name") or ch.get("name"))[:6 if narrow else 12], font=self.f_med, fill=(255, 255, 255, 255))
            if not items:
                d.text((x_prog, y), "No programmes", font=self.f_med, fill=(255, 80, 80, 255))
                y += (highlight_lines if is_hl else per_channel) * line_h
                continue
            if is_hl:
                idx = max(0, min(cursor, len(items) - 1))
                item = items[idx]
                marker = "▶ " if idx == 0 else "  "
                d.text((x_prog - int(30 * s), y), marker, font=self.f_med, fill=(0, 255, 0, 255))
                when = f"{_hhmm(item.get('start_ts'), self.tz)}–{_hhmm(item.get('end_ts'), self.tz)}"
                d.text((x_prog, y), _fit(d, f"{when}  {item.get('title', '')}", self.f_med, prog_w),
                       font=self.f_med, fill=(255, 255, 0, 255))
                d.text((x_prog, y + line_h), _fit(d, item.get("subtitle") or "", self.f_small, prog_w), font=self.f_small, fill=(0, 255, 255, 255))
                # One line of plot: the fourth line of the block carries the key help.
                plot = " ".join((item.get("plot") or "").split())
                if plot:
                    d.text((x_prog, y + 2 * line_h - int(6 * s)), _fit(d, plot, self.f_small, prog_w),
                           font=self.f_small, fill=(255, 255, 255, 255))
                nav = f"{idx + 1}/{len(items)}  ◀ ▶ programmes   ▲ ▼ channel   OK tune"
                d.text((x_num, y + 3 * line_h - int(6 * s)), nav, font=self.f_small, fill=(150, 150, 150, 255))
                y += highlight_lines * line_h
            else:
                now_item = items[0]
                d.text((x_prog, y), _fit(d, f"{_hhmm(now_item.get('start_ts'), self.tz)}  {now_item.get('title', '')}", self.f_med, prog_w),
                       font=self.f_med, fill=(255, 255, 255, 255))
                if len(items) > 1:
                    nx = items[1]
                    d.text((x_prog, y + line_h), _fit(d, f"{_hhmm(nx.get('start_ts'), self.tz)}  {nx.get('title', '')}", self.f_small, prog_w),
                           font=self.f_small, fill=(160, 160, 160, 255))
                y += per_channel * line_h
        path, iw, ih = self._save(img, "guide")
        return path, iw, ih, self.mx, self.height - h - self.my


def make_testcard(path: Path, width: int = 1024, height: int = 768, label: str = "PiTV") -> Path:
    """A 4:3 colour-bars test card with the PiTV wordmark, used at boot and whenever nothing is
    playing. It carries no caption: the player lays its message over it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), (0, 0, 0))
    d = ImageDraw.Draw(img)
    bars = [(192, 192, 192), (192, 192, 0), (0, 192, 192), (0, 192, 0), (192, 0, 192), (192, 0, 0), (0, 0, 192)]
    bw = width / len(bars)
    top = int(height * 0.66)
    for i, c in enumerate(bars):
        d.rectangle((int(i * bw), 0, int((i + 1) * bw), top), fill=c)
    small = [(0, 0, 192), (19, 19, 19), (192, 0, 192), (19, 19, 19), (0, 192, 192), (19, 19, 19), (192, 192, 192)]
    for i, c in enumerate(small):
        d.rectangle((int(i * bw), top, int((i + 1) * bw), int(height * 0.75)), fill=c)
    d.rectangle((0, int(height * 0.75), width, height), fill=(19, 19, 19))
    f = _font(int(height * 0.16))
    bbox = d.textbbox((0, 0), label, font=f)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    cx, cy = width // 2, int(height * 0.5)
    d.rectangle((cx - tw // 2 - 40, cy - th // 2 - 30, cx + tw // 2 + 40, cy + th // 2 + 50), fill=(0, 0, 0))
    d.text((cx - tw // 2, cy - th // 2 - bbox[1]), label, font=f, fill=(255, 255, 255))
    img.save(path)
    return path
