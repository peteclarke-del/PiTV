"""Channel idents: a fifteen second film for each channel, made from its name and colour.

An ident is ordinary library material (kind `ident`): pitv_content indexes a folder of them and
the scheduler places them where a channel's pattern asks. This module only makes the files, in
one of two layouts. `<out>/ch<number>/<name> ident.mp4` is the layout pitv_content reads a channel
from, so an idents source pointed at `<out>` ties each film to its channel with no more said.
Flat, `<out>/<name> ident.mp4`, is for a folder kept with other material (the `idents` folder of
the adverts share): the path says nothing there, and PiTV gives the film to the channel its
title begins with on import (`db.assign_ident_channels`).

The picture is drawn frame by frame with Pillow and piped to ffmpeg: the four bars of the PiTV
mark sweep in, the wordmark resolves over them, and the channel's name rises under it in the
channel's colour, all on a field of that colour. The sound is a short synthesised sting, four
notes over a held chord, in a key of the channel's own so that no two sound alike.

A voiceover is never invented. Where `<voices>/<number>.wav` (or .mp3, .flac, .ogg, .m4a, or the
channel's short name in place of the number) exists, it is laid over the sting from the moment
the channel's name appears, with the music ducked under it: a recording of a person saying "This
is PiTV One" is what it is for.
"""

from __future__ import annotations

import logging
import math
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import display
from .db import all_settings, rows_to_dicts

log = logging.getLogger("pitv.idents")

SECONDS = 15
FPS = 25
SAMPLE_RATE = 44100
BARS = ("#e63946", "#f4a261", "#2a9d8f", "#457b9d")     # the mark's four bars, as in the web interface
VOICE_AT = 7.0                                          # when the channel's name appears, and a voice starts
VOICE_SUFFIXES = (".wav", ".flac", ".mp3", ".ogg", ".m4a")
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-BoldOblique.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
# A major chord a channel: root frequencies a tone or so apart, so seven channels are seven keys.
ROOTS = (196.00, 220.00, 246.94, 261.63, 293.66, 329.63, 349.23, 392.00)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


def _rgb(colour: str) -> tuple[int, int, int]:
    value = colour.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)) if len(value) == 6 else (70, 120, 160)


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=True))     # type: ignore[return-value]


def _ease(t: float) -> float:
    """0 to 1, slow at both ends."""
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


def _text(label: str, size: int, fill: tuple[int, int, int], shadow: tuple[int, int, int]) -> Image.Image:
    """A line of lettering on a clear ground, with the hard offset shadow of the mark."""
    font = _font(size)
    box = font.getbbox(label)
    off = max(2, size // 14)
    img = Image.new("RGBA", (box[2] - box[0] + off * 3, box[3] - box[1] + off * 3), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.text((off - box[0] + off, off - box[1] + off), label, font=font, fill=(*shadow, 255))
    draw.text((off - box[0], off - box[1]), label, font=font, fill=(*fill, 255))
    return img


def _mark(size: int, colour: tuple[int, int, int]) -> Image.Image:
    """The PiTV wordmark: "Pi" in white and "TV" in the channel's colour, set as one word."""
    font = _font(size)
    box = font.getbbox("PiTV")
    off = max(2, size // 14)
    img = Image.new("RGBA", (box[2] - box[0] + off * 3, box[3] - box[1] + off * 3), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    x, y, split = off - box[0], off - box[1], font.getlength("Pi")
    shadow = (*_mix(colour, (0, 0, 0), 0.35), 255)
    for dx, dy, pi, tv in ((off, off, shadow, shadow), (0, 0, (245, 245, 245, 255), (*_mix(colour, (255, 255, 255), 0.25), 255))):
        draw.text((x + dx, y + dy), "Pi", font=font, fill=pi)
        draw.text((x + dx + split, y + dy), "TV", font=font, fill=tv)
    return img


def _faded(img: Image.Image, alpha: float) -> Image.Image:
    if alpha >= 1:
        return img
    out = img.copy()
    out.putalpha(out.getchannel("A").point(lambda v: int(v * max(0.0, alpha))))
    return out


def frames(channel: dict[str, Any], width: int, height: int) -> Iterator[Image.Image]:
    """The ident's pictures, in order. Everything that does not move is drawn once."""
    colour = _rgb(channel.get("colour") or "")
    dark, deep = _mix(colour, (0, 0, 0), 0.72), _mix(colour, (0, 0, 0), 0.9)
    ground = Image.new("RGB", (width, height))
    paint = ImageDraw.Draw(ground)
    for y in range(height):                                   # a field of the channel's colour, darker to the foot
        paint.line([(0, y), (width, y)], fill=_mix(dark, deep, y / height))
    glow = Image.new("L", (width, height), 0)
    ImageDraw.Draw(glow).ellipse([width * 0.12, height * 0.08, width * 0.88, height * 0.72], fill=120)
    ground.paste(Image.new("RGB", (width, height), _mix(colour, (255, 255, 255), 0.15)), mask=glow.filter(ImageFilter.GaussianBlur(width // 7)))

    mark = _mark(height // 4, colour)
    name = (channel.get("short_name") or channel.get("name") or "").upper()
    title = _text(name, height // 7, _mix(colour, (255, 255, 255), 0.35), (10, 10, 10))
    bar_h, bar_gap = max(4, height // 60), max(2, width // 180)
    bars_w = mark.width
    total = SECONDS * FPS
    for n in range(total):
        t = n / FPS
        frame = ground.copy()
        # Slow bands of light drifting across the field, so the hold is never quite still.
        sheen = Image.new("L", (width, height), 0)
        sx = int((t / SECONDS) * width * 1.6) - width // 3
        ImageDraw.Draw(sheen).polygon([(sx, 0), (sx + width // 5, 0), (sx - width // 8, height), (sx - width // 8 - width // 5, height)], fill=26)
        frame.paste(Image.new("RGB", (width, height), (255, 255, 255)), mask=sheen.filter(ImageFilter.GaussianBlur(width // 30)))
        canvas = frame.convert("RGBA")
        cx, cy = width // 2, int(height * 0.42)
        # 0 to 3 s: the four bars sweep in from the left, one after another, and settle under the mark.
        bar_y = cy + mark.height // 2 + bar_h
        each = (bars_w - bar_gap * 3) // 4
        for i, bar in enumerate(BARS):
            p = _ease((t - 0.3 - i * 0.35) / 1.6)
            if p <= 0:
                continue
            x_end = cx - bars_w // 2 + i * (each + bar_gap)
            x = int(-each + (x_end + each) * p)
            ImageDraw.Draw(canvas).rectangle([x, bar_y, x + each, bar_y + bar_h], fill=(*_rgb(bar), 255))
        # 2.5 to 5.5 s: the mark resolves, drawn large and soft, and tightens to its size.
        p = _ease((t - 2.5) / 3.0)
        if p > 0:
            scale = 1.35 - 0.35 * p
            shown = mark.resize((max(1, int(mark.width * scale)), max(1, int(mark.height * scale))), Image.Resampling.BILINEAR)
            if p < 1:
                shown = shown.filter(ImageFilter.GaussianBlur((1 - p) * 6))
            canvas.alpha_composite(_faded(shown, p), (cx - shown.width // 2, cy - shown.height // 2))
        # 7 s: the channel's name rises into place under the bars.
        p = _ease((t - VOICE_AT) / 1.4)
        if p > 0:
            ty = bar_y + bar_h * 3 + int((1 - p) * height * 0.08)
            canvas.alpha_composite(_faded(title, p), (cx - title.width // 2, ty))
        out = canvas.convert("RGB")
        fade = min(1.0, t / 0.6, (SECONDS - t) / 0.8)               # up from black, and back to it
        yield out if fade >= 1 else Image.blend(Image.new("RGB", out.size, (0, 0, 0)), out, max(0.0, fade))


def sting(path: Path, channel_number: int) -> None:
    """The ident's music as a WAV: a held major chord that swells under four bell notes rising
    to the octave, the last landing as the channel's name appears."""
    root = ROOTS[(channel_number - 1) % len(ROOTS)]
    chord = (root / 2, root, root * 5 / 4, root * 3 / 2)
    bells = ((0.4, root), (1.5, root * 5 / 4), (2.6, root * 3 / 2), (VOICE_AT - 0.1, root * 2))
    total = SECONDS * SAMPLE_RATE
    left, right = [0.0] * total, [0.0] * total
    for i in range(total):
        t = i / SAMPLE_RATE
        swell = _ease(t / 5.0) * min(1.0, (SECONDS - t) / 2.5)
        pad = sum(math.sin(2 * math.pi * f * t) + 0.3 * math.sin(2 * math.pi * f * 2.003 * t) for f in chord) / len(chord)
        tremor = 1 + 0.08 * math.sin(2 * math.pi * 0.45 * t)
        left[i] = right[i] = 0.2 * swell * tremor * pad
    for k, (at, f) in enumerate(bells):
        pan = 0.35 + 0.1 * k
        start = int(at * SAMPLE_RATE)
        for i in range(start, min(total, start + int(4.5 * SAMPLE_RATE))):
            t = (i - start) / SAMPLE_RATE
            env = math.exp(-t * (1.1 if k == len(bells) - 1 else 1.8)) * min(1.0, t * 400)
            tone = math.sin(2 * math.pi * f * t) + 0.45 * math.sin(2 * math.pi * f * 2.0 * t) * math.exp(-t * 3) \
                + 0.2 * math.sin(2 * math.pi * f * 3.01 * t) * math.exp(-t * 5)
            left[i] += 0.3 * env * tone * (1 - pan)
            right[i] += 0.3 * env * tone * pan
    peak = max(1e-9, max(max(abs(v) for v in left), max(abs(v) for v in right)))
    gain = 0.7 * 32767 / peak
    with wave.open(str(path), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(b"".join(struct.pack("<hh", int(a * gain), int(b * gain)) for a, b in zip(left, right, strict=True)))


def voice_for(channel: dict[str, Any], voices: Path | None) -> Path | None:
    """The owner's recording for this channel, by its number or its short name."""
    if voices is None or not voices.is_dir():
        return None
    for stem in (str(channel["number"]), str(channel.get("short_name") or "").lower(), str(channel.get("short_name") or "")):
        for suffix in VOICE_SUFFIXES:
            if stem and (voices / f"{stem}{suffix}").is_file():
                return voices / f"{stem}{suffix}"
    return None


def make(channel: dict[str, Any], out_dir: Path, width: int, height: int, voices: Path | None = None,
         *, flat: bool = False) -> Path:
    """Render one channel's ident and return the file. It is encoded in a temporary folder, because
    `+faststart` rewrites the file and a network mount may not allow the seek, then copied beside
    its place and moved in when whole, so an index run never meets half a film."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is needed to make idents and is not installed")
    folder = out_dir if flat else out_dir / f"ch{int(channel['number'])}"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{channel['name']} ident.mp4"
    voice = voice_for(channel, voices)
    with tempfile.TemporaryDirectory(prefix="pitv-ident-") as tmp:
        music = Path(tmp) / "sting.wav"
        sting(music, int(channel["number"]))
        encoded = Path(tmp) / "ident.mp4"
        part = folder / f".{target.name}.part"
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(FPS), "-i", "-",
               "-i", str(music)]
        if voice is not None:
            # The voice from the moment the name appears; the music ducks under it and comes back.
            mix = (f"[2:a]aresample={SAMPLE_RATE},adelay={int(VOICE_AT * 1000)}:all=1,volume=1.6,asplit[v][key];"
                   "[1:a][key]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=600[m];"
                   "[m][v]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.9[a]")
            cmd += ["-i", str(voice), "-filter_complex", mix, "-map", "0:v", "-map", "[a]"]
        else:
            cmd += ["-map", "0:v", "-map", "1:a"]
        cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", "17", "-pix_fmt", "yuv420p", "-profile:v", "high",
                "-c:a", "aac", "-b:a", "192k", "-t", str(SECONDS), "-movflags", "+faststart", "-f", "mp4", str(encoded)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            assert proc.stdin is not None
            for frame in frames(channel, width, height):
                proc.stdin.write(frame.tobytes())
            proc.stdin.close()
            error = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            if proc.wait() != 0:
                raise RuntimeError(f"ffmpeg could not encode {target.name}: {error.strip()[:400]}")
        except BaseException:
            proc.kill()
            raise
        finally:
            if proc.stderr:
                proc.stderr.close()
        try:
            shutil.copyfile(encoded, part)
            part.replace(target)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
    log.info("made %s%s", target, " with a voiceover" if voice else "")
    return target


def make_all(conn: sqlite3.Connection, out_dir: Path, voices: Path | None = None,
             numbers: set[int] | None = None, *, flat: bool = False) -> list[Path]:
    """An ident for every enabled channel (or those in `numbers`), at the screen's own frame."""
    profile = display.profile(all_settings(conn))
    channels = rows_to_dicts(conn.execute("SELECT * FROM channels WHERE enabled = 1 ORDER BY number"))
    return [make(c, out_dir, profile.width, profile.height, voices, flat=flat) for c in channels
            if numbers is None or int(c["number"]) in numbers]
