"""Draw the test card straight onto /dev/fb0 as early in boot as possible.

Runs before the player starts; mpv then takes over the display through DRM. Cost: about a
quarter of a second, no X, no plymouth."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image


def fb_geometry() -> tuple[int, int, int, int]:
    size = Path("/sys/class/graphics/fb0/virtual_size").read_text().strip().split(",")
    bpp = int(Path("/sys/class/graphics/fb0/bits_per_pixel").read_text().strip())
    stride = int(Path("/sys/class/graphics/fb0/stride").read_text().strip())
    return int(size[0]), int(size[1]), bpp, stride


def main(argv: list[str]) -> int:
    image_path = Path(argv[1]) if len(argv) > 1 else Path("/var/lib/pitv/testcard.png")
    if not image_path.exists():
        from .player.osd import make_testcard
        make_testcard(image_path)
    try:
        w, h, bpp, stride = fb_geometry()
    except (OSError, ValueError) as exc:
        print(f"no framebuffer: {exc}", file=sys.stderr)
        return 0
    img = Image.open(image_path).convert("RGB").resize((w, h))
    if bpp == 32:
        raw = img.convert("RGBA").tobytes("raw", "BGRA")
        row = w * 4
    elif bpp == 16:
        raw = img.convert("RGB").tobytes("raw", "BGR;16")
        row = w * 2
    else:
        print(f"unsupported framebuffer depth {bpp}", file=sys.stderr)
        return 0
    with open("/dev/fb0", "wb") as fb:
        for y in range(h):
            fb.seek(y * stride)
            fb.write(raw[y * row:(y + 1) * row])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
