"""Draw the test card straight onto /dev/fb0 as early in boot as possible.

Runs before the player starts; mpv then takes over the display through DRM. Cost: about a
quarter of a second, no X, no plymouth."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageChops


def fb_geometry() -> tuple[int, int, int, int]:
    size = Path("/sys/class/graphics/fb0/virtual_size").read_text().strip().split(",")
    bpp = int(Path("/sys/class/graphics/fb0/bits_per_pixel").read_text().strip())
    stride = int(Path("/sys/class/graphics/fb0/stride").read_text().strip())
    return int(size[0]), int(size[1]), bpp, stride


def _rgb565(img: Image.Image) -> bytes:
    """Little-endian RGB565, built from band arithmetic: Pillow has no packer for it. The
    bit fields do not overlap, so the saturating add is a plain OR."""
    r, g, b = img.split()
    low = ImageChops.add(g.point(lambda v: (v & 0x1C) << 3), b.point(lambda v: v >> 3))
    high = ImageChops.add(r.point(lambda v: v & 0xF8), g.point(lambda v: v >> 5))
    return Image.merge("LA", (low, high)).tobytes()


def main(argv: list[str]) -> int:
    """Always exits 0: a missing or odd framebuffer must not fail the boot, only lose the card."""
    image_path = Path(argv[1]) if len(argv) > 1 else Path("/var/lib/pitv/testcard.png")
    try:
        w, h, bpp, stride = fb_geometry()
    except (OSError, ValueError, IndexError) as exc:
        print(f"no framebuffer: {exc}", file=sys.stderr)
        return 0
    if bpp not in (16, 32):
        print(f"unsupported framebuffer depth {bpp}", file=sys.stderr)
        return 0
    try:
        if not image_path.exists():
            from .player.osd import make_testcard
            make_testcard(image_path)
        with Image.open(image_path) as src:
            img = src.convert("RGB").resize((w, h))
        raw, row = (img.convert("RGBA").tobytes("raw", "BGRA"), w * 4) if bpp == 32 else (_rgb565(img), w * 2)
        with open("/dev/fb0", "wb") as fb:
            if stride == row:
                fb.write(raw)
            else:
                for y in range(h):
                    fb.seek(y * stride)
                    fb.write(raw[y * row:(y + 1) * row])
    except OSError as exc:
        print(f"could not draw the test card: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
