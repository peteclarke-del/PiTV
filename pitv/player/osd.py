"""On-screen graphics: channel badge, volume bar, guide, test card. Rendered with Pillow
into BGRA files that mpv composites with `overlay-add`."""

from __future__ import annotations

import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeMonoBold.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf",
]
TELETEXT = {  # the seven teletext colours plus black
    "black": (0, 0, 0), "red": (255, 0, 0), "green": (0, 255, 0), "yellow": (255, 255, 0),
    "blue": (0, 0, 255), "magenta": (255, 0, 255), "cyan": (0, 255, 255), "white": (255, 255, 255),
}

OVERLAY_BADGE, OVERLAY_VOLUME, OVERLAY_GUIDE, OVERLAY_MESSAGE, OVERLAY_STATIC = 1, 2, 3, 4, 5


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


def _hhmm(ts: int | float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "--:--"


class Renderer:
    """Draws overlays sized to the output. `margin` keeps a fraction of every edge clear
    (a CRT loses ~5-8% to overscan); `text_scale` enlarges type for small 4:3 sets."""

    def __init__(self, run_dir: Path, width: int = 1280, height: int = 720,
                 margin: float = 0.07, text_scale: float = 1.0) -> None:
        self.run_dir = run_dir
        self.width, self.height = width, height
        self.margin_frac, self.text_scale = margin, text_scale
        self.mx, self.my = int(width * margin), int(height * margin)
        self.scale = (height / 720) * text_scale
        self.f_big = _font(int(44 * self.scale))
        self.f_med = _font(int(28 * self.scale))
        self.f_small = _font(int(22 * self.scale))

    def resize(self, width: int, height: int) -> None:
        if (width, height) != (self.width, self.height) and width > 0 and height > 0:
            self.__init__(self.run_dir, width, height, self.margin_frac, self.text_scale)

    def configure(self, margin: float, text_scale: float) -> None:
        if (margin, text_scale) != (self.margin_frac, self.text_scale):
            self.__init__(self.run_dir, self.width, self.height, margin, text_scale)

    @property
    def safe_width(self) -> int:
        return self.width - 2 * self.mx

    def _save(self, img: Image.Image, name: str) -> tuple[str, int, int]:
        path = self.run_dir / f"{name}.bgra"
        path.write_bytes(img.tobytes("raw", "BGRA"))
        return str(path), img.width, img.height

    # --- static ---------------------------------------------------------------------------

    def static(self) -> tuple[str, int, int, int, int]:
        """Full-screen analogue 'snow', shown for a moment when changing channel."""
        small = Image.effect_noise((self.width // 3, self.height // 3), 90).convert("L")
        noise = small.resize((self.width, self.height), Image.NEAREST)
        img = Image.merge("RGBA", (noise, noise, noise, Image.new("L", noise.size, 255)))
        path, w, h = self._save(img, "static")
        return path, w, h, 0, 0

    # --- badge ---------------------------------------------------------------------------

    def badge(self, channel: dict[str, Any], now: dict[str, Any] | None, nxt: dict[str, Any] | None,
              position: float | None, behind_live: bool) -> tuple[str, int, int, int, int]:
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
        d.text((text_x, int(14 * s)), _fit(d, channel.get("name", ""), self.f_med, text_w), font=self.f_med, fill=(255, 255, 255, 255))
        if now:
            title = now.get("title", "")
            sub = now.get("subtitle", "")
            line = f"{_hhmm(now.get('start_ts'))}–{_hhmm(now.get('end_ts'))}  {title}"
            d.text((text_x, int(50 * s)), _fit(d, line, self.f_med, text_w), font=self.f_med, fill=(255, 255, 0, 255))
            if sub:
                d.text((text_x, int(84 * s)), _fit(d, sub, self.f_small, text_w), font=self.f_small, fill=(0, 255, 255, 255))
            # progress bar
            if now.get("start_ts") and now.get("end_ts") and position is not None:
                frac = max(0.0, min(1.0, position))
                x0, x1, y = int(135 * s), w - int(20 * s), int(116 * s)
                d.rectangle((x0, y, x1, y + int(8 * s)), fill=(70, 70, 70, 255))
                d.rectangle((x0, y, x0 + int((x1 - x0) * frac), y + int(8 * s)), fill=col + (255,))
        else:
            d.text((int(135 * s), int(50 * s)), "No programme scheduled", font=self.f_med, fill=(255, 80, 80, 255))
        if nxt:
            d.text((text_x, int(140 * s)), _fit(d, f"Next {_hhmm(nxt.get('start_ts'))}  {nxt.get('title', '')}", self.f_small, text_w),
                   font=self.f_small, fill=(180, 180, 180, 255))
        if behind_live:
            d.text((w - int(170 * s), int(14 * s)), "PAUSED/BEHIND", font=self.f_small, fill=(255, 0, 0, 255))
        path, iw, ih = self._save(img, "badge")
        return path, iw, ih, self.mx, self.height - h - self.my

    # --- volume ---------------------------------------------------------------------------

    def volume(self, volume: int, muted: bool) -> tuple[str, int, int, int, int]:
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

    def message(self, text: str, sub: str = "") -> tuple[str, int, int, int, int]:
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
              highlight: int, cursor: int, now_ts: int, clock: str) -> tuple[str, int, int, int, int]:
        """Teletext style guide. rows[channel_id] = [now, next, next+1, ...]."""
        s = self.scale
        w = self.safe_width
        line_h = int(34 * s)
        header_h = int(52 * s)
        per_channel = 2  # lines per non-highlighted channel
        highlight_lines = 4
        total_lines = sum(highlight_lines if ch["id"] == highlight else per_channel for ch in channels)
        h = header_h + total_lines * line_h + int(30 * s)
        h = min(h, self.height - 2 * self.my)
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle((0, 0, w - 1, h - 1), fill=(0, 0, 0, 225))
        # header
        d.rectangle((0, 0, w - 1, header_h), fill=(0, 0, 255, 255))
        d.text((int(16 * s), int(10 * s)), "P100  PiTV GUIDE", font=self.f_med, fill=(255, 255, 0, 255))
        cb = d.textbbox((0, 0), clock, font=self.f_med)
        d.text((w - (cb[2] - cb[0]) - int(16 * s), int(10 * s)), clock, font=self.f_med, fill=(255, 255, 255, 255))
        y = header_h + int(8 * s)
        narrow = w < 1100 * s
        x_num, x_name, x_prog = int(16 * s), int(70 * s), int(190 * s) if narrow else int(300 * s)
        prog_w = w - x_prog - int(12 * s)
        for ch in channels:
            is_hl = ch["id"] == highlight
            items = rows.get(ch["id"], [])
            col = _hex(ch.get("colour"))
            if is_hl:
                d.rectangle((0, y - int(4 * s), w - 1, y + highlight_lines * line_h - int(6 * s)), fill=(30, 30, 30, 255))
            d.text((x_num, y), str(ch["number"]), font=self.f_med, fill=col + (255,))
            d.text((x_name, y), str(ch.get("short_name") or ch.get("name"))[:6 if narrow else 12], font=self.f_med, fill=(255, 255, 255, 255))
            if not items:
                d.text((x_prog, y), "No programmes", font=self.f_med, fill=(255, 80, 80, 255))
                y += per_channel * line_h
                continue
            if is_hl:
                idx = max(0, min(cursor, len(items) - 1))
                item = items[idx]
                marker = "▶ " if idx == 0 else "  "
                d.text((x_prog - int(30 * s), y), marker, font=self.f_med, fill=(0, 255, 0, 255))
                d.text((x_prog, y), _fit(d, f"{_hhmm(item.get('start_ts'))}–{_hhmm(item.get('end_ts'))}  {item.get('title', '')}", self.f_med, prog_w),
                       font=self.f_med, fill=(255, 255, 0, 255))
                d.text((x_prog, y + line_h), _fit(d, item.get("subtitle") or "", self.f_small, prog_w), font=self.f_small, fill=(0, 255, 255, 255))
                plot = (item.get("plot") or "").strip()
                if plot:
                    approx = max(20, int(prog_w / max(1, d.textlength("n", font=self.f_small))))
                    wrapped = textwrap.wrap(plot, width=approx)[:2]
                    for i, line in enumerate(wrapped):
                        d.text((x_prog, y + (2 + i) * line_h - int(6 * s)), _fit(d, line, self.f_small, prog_w), font=self.f_small, fill=(255, 255, 255, 255))
                nav = f"{idx + 1}/{len(items)}  ◀ ▶ programmes   ▲ ▼ channel   OK tune"
                d.text((x_num, y + 3 * line_h - int(6 * s)), nav, font=self.f_small, fill=(150, 150, 150, 255))
                y += highlight_lines * line_h
            else:
                now_item = items[0]
                d.text((x_prog, y), _fit(d, f"{_hhmm(now_item.get('start_ts'))}  {now_item.get('title', '')}", self.f_med, prog_w),
                       font=self.f_med, fill=(255, 255, 255, 255))
                if len(items) > 1:
                    nx = items[1]
                    d.text((x_prog, y + line_h), _fit(d, f"{_hhmm(nx.get('start_ts'))}  {nx.get('title', '')}", self.f_small, prog_w),
                           font=self.f_small, fill=(160, 160, 160, 255))
                y += per_channel * line_h
            if y > h - line_h:
                break
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
