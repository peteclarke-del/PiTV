"""Live HTTP streams of the channels, so a browser, VLC or a phone can watch what is on
without the television.

One stream per channel, started when someone first asks for it and stopped once nobody has
asked for a while. Each programme is packaged into the channel's rolling playlist by its own
ffmpeg: copied when the file already suits HLS (the cache copies always do), re-encoded only
when it does not, which is why several viewers cost almost nothing. The schedule decides what
plays and from what offset, exactly as it does for the television, so a stream shows the same
programme at the same moment; segments carry a discontinuity marker at each change, which is
what tells a player the picture may change shape or codec.

Segments live under the run directory (tmpfs on the Pi) and are deleted as they roll off the
playlist, so nothing accumulates."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import db as dbm
from . import display
from .config import TEST_SIGNAL, Config
from .db import all_settings, enabled_channels, now_ts
from .guide import slot_at
from .player.cache import MediaCache
from .player.hwdec import is_raspberry_pi

log = logging.getLogger("pitv.stream")

PLAYLIST = "index.m3u8"
SEGMENT_GLOB = "s*.ts"
FFMPEG_LOG = "ffmpeg.log"
BURST_SECONDS = 20   # read this much flat out before pacing, so a viewer waits seconds, not a segment
LIST_SIZE = 6                  # segments kept in the playlist; the rest are deleted
MAX_PROGRAMME_SECONDS = 4 * 3600
CARD_SECONDS = 60              # how long the test signal runs before the schedule is looked at again
TICK_SECONDS = 1.0
@dataclass
class _Channel:
    number: int
    channel_id: int
    dir: Path
    proc: subprocess.Popen | None = None
    seq: int = 0                      # next segment number, so a new programme never overwrites
    last_request: float = field(default_factory=time.monotonic)
    started_ts: int = 0
    playing: str = ""                 # what the current ffmpeg is packaging, for the admin
    error: str = ""
    viewers: dict[str, float] = field(default_factory=dict)   # address -> when it last asked
    until: float = 0.0                # when the current run is due to end, so an early exit shows up


@lru_cache(maxsize=1)
def has_initial_burst() -> bool:
    """Whether this ffmpeg can read the opening seconds flat out (`-readrate_initial_burst`,
    ffmpeg 6.1). Older builds pace from the first frame, so a stream simply starts slower."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-h", "full"], capture_output=True, text=True,
                             timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return "readrate_initial_burst" in out.stdout


def _codec_args(media: dict[str, Any] | None, where: str, profile: dict[str, Any], encoder: str,
                segment_seconds: int, source: Path | None = None) -> list[str]:
    """Encode a browser-safe H.264/AAC rendition at the screen's profile.

    An encode is given a keyframe every segment, because the packager can only cut there: the
    test signal, one frame a second, would otherwise become a single segment minutes long.

    MPEG-TS itself accepts HEVC, MPEG-2 and AC-3, but that does not make them safe in Chrome,
    Firefox or Safari. Even copied H.264 can carry a profile/pixel format a browser's hardware
    decoder refuses. The web rendition therefore never inherits source codecs."""
    # Some old AVI/MPEG files retain broken or non-zero timestamps after an input seek. Reset
    # both tracks for the live rendition so a browser never has to join an HLS stream whose
    # first audio/video timestamps are unrelated.
    fit = (f"setpts=PTS-STARTPTS,scale={profile['width']}:{profile['height']}:force_original_aspect_ratio=decrease,"
           f"pad={profile['width']}:{profile['height']}:-1:-1")
    args = ["-c:v", encoder, "-b:v", f"{profile['max_bitrate_kbps']}k", "-vf", fit,
            "-pix_fmt", "yuv420p", "-force_key_frames", f"expr:gte(t,n_forced*{segment_seconds})"]
    if encoder == "libx264":
        # This is a live rendition, not a cache master. The default x264 preset can consume
        # several cores and fall behind while pitv_content is transcoding; viewers then poll a
        # playlist whose next segment never arrives in time. Fixed bitrate plus veryfast keeps
        # the picture browser-safe while preserving enough CPU for independent channel streams.
        args += ["-preset", "veryfast", "-tune", "zerolatency", "-profile:v", "main"]
    args += ["-af", "aresample=async=1:first_pts=0", "-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    return args


def ffmpeg_command(source: Path, *, start: float, seconds: int, out_dir: Path, seq: int, segment_seconds: int,
                   media: dict[str, Any] | None, where: str, profile: dict[str, Any], encoder: str,
                   loop: bool = False, silent_audio: bool = False, burst: int = 0) -> list[str]:
    """Build one programme's packaging run, resuming only a playlist with existing media."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err"]
    if loop:
        cmd += ["-stream_loop", "-1"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    # Read the file at its own frame rate. Without this a copy runs as fast as the disk allows,
    # so the whole programme is packaged in seconds and every segment is deleted off the end of
    # the playlist long before a viewer asks for it. The first seconds are read flat out where
    # ffmpeg can, so a viewer is not left watching a spinner while the first segment is written.
    cmd += ["-re"]
    if burst and has_initial_burst():
        cmd += ["-readrate_initial_burst", str(burst)]
    cmd += ["-i", str(source)]
    if silent_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    # The first video and audio stream only: films carry cover art, commentary and subtitle tracks
    # that HLS has no use for.
    cmd += ["-map", "0:v:0", "-map", "1:a:0" if silent_audio else "0:a:0?", "-sn", "-dn"]
    if loop:
        cmd += ["-r", "25"]   # the test signal is one frame a second; players dislike so few
    cmd += _codec_args(media, where, profile, encoder, segment_seconds, source)
    hls_flags = "delete_segments+omit_endlist+independent_segments"
    # Only resume a playlist which already contains media. ffmpeg's append_list flag emits an
    # unnecessary leading discontinuity even for a brand-new stream; some native demuxers reject
    # that startup. Reserve append mode for the programme boundaries that actually need it.
    if (out_dir / PLAYLIST).exists() and any(out_dir.glob("s*.ts")):
        hls_flags = f"append_list+{hls_flags}"
    cmd += [
        "-t", str(max(1, seconds)), "-avoid_negative_ts", "make_zero",
        "-f", "hls", "-hls_time", str(segment_seconds), "-hls_list_size", str(LIST_SIZE),
        # append_list adds the boundary discontinuity when a new programme's ffmpeg process
        # resumes this live playlist. Adding discont_start as well produces two adjacent
        # markers, which Chromium can reject as an unparseable stream after a channel change.
        "-hls_flags", hls_flags,
        "-hls_segment_type", "mpegts", "-hls_segment_filename", str(out_dir / "s%05d.ts"),
        "-start_number", str(seq), str(out_dir / PLAYLIST),
    ]
    return cmd


class Streams:
    """Every channel stream the web service is serving. Requests touch a channel; one thread
    keeps the packaging running and stops what nobody is watching."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.root = cfg.run_dir / "streams"
        self._channels: dict[int, _Channel] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- what the web service calls ---------------------------------------------------------

    def _seen(self, ch: _Channel, client: str | None) -> None:
        """Note a viewer. A new address is logged, so the stream log shows who watched what."""
        ch.last_request = time.monotonic()
        if not client:
            return
        if client not in ch.viewers:
            log.info("stream ch%s: %s started watching", ch.number, client.split("#", 1)[0])
        ch.viewers[client] = ch.last_request

    def _make_room(self, number: int, client: str | None, max_streams: int) -> bool:
        """Replace this viewer's least-recent channel when the stream limit is full.

        Called with ``_lock`` held. A browser cannot explicitly close an HLS GET when it
        changes channel, so its previous stream otherwise occupies a slot until the idle sweep.
        A shared encoder belongs to every viewer using it: never stop it merely because one of
        them changes channel. In that case the configured encoder limit is genuinely full and
        the new stream must wait (or the administrator can raise ``stream_max_streams``).
        """
        if len(self._channels) < max_streams:
            return True
        previous = [old for old in self._channels.values()
                    if old.number != number and client and client in old.viewers]
        if not previous:
            return False
        # Only an encoder exclusively owned by this viewer can be reclaimed. Stopping a shared
        # channel here would blank every other browser watching it.
        exclusive = [old for old in previous if set(old.viewers) == {client}]
        if not exclusive:
            return False
        old = min(exclusive, key=lambda item: item.viewers[client])
        log.info("stream ch%s stopped (viewer %s changed to ch%s)", old.number, client, number)
        self._stop_channel(old)
        self._channels.pop(old.number, None)
        return len(self._channels) < max_streams

    def open(self, number: int, client: str | None = None) -> _Channel | None:
        """Start the channel's stream if it is not running, and mark it as wanted. None when
        streaming is off, the channel is not enabled, or too many streams are running."""
        conn = dbm.connect(self.cfg.db_path)
        try:
            settings = all_settings(conn)
            if not settings.get("streaming_enabled", True):
                return None
            channel = next((c for c in enabled_channels(conn) if c["number"] == number), None)
            if channel is None:
                return None
        finally:
            conn.close()
        with self._lock:
            ch = self._channels.get(number)
            if ch is None:
                max_streams = int(settings.get("stream_max_streams", 2))
                if not self._make_room(number, client, max_streams):
                    log.warning("stream for channel %s refused: %s already running", number, len(self._channels))
                    return None
                ch = self._channels[number] = _Channel(number=number, channel_id=channel["id"],
                                                       dir=self.root / f"ch{number}", started_ts=now_ts())
                ch.dir.mkdir(parents=True, exist_ok=True)
                self._clear(ch)
                log.info("stream ch%s started", number)
            self._seen(ch, client)
        self._ensure_thread()
        return ch

    def playlist(self, number: int, client: str | None = None, timeout: float = 15.0) -> str | None:
        """The channel's playlist once it holds a segment or two, so a player never sees an empty
        one. None when the stream could not start.

        ffmpeg writes bare segment names, which a player resolves against the playlist's own
        address: from `/channel/3.m3u8` that is `/channel/s00007.ts`, which is nobody's segment.
        Each name is given the channel's folder on the way out, so both this address and
        `/channel/3/index.m3u8` lead to the same files."""
        ch = self.open(number, client)
        if ch is None:
            return None
        path = ch.dir / PLAYLIST
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.is_file() and len(list(ch.dir.glob(SEGMENT_GLOB))) >= 2:
                break
            time.sleep(0.2)
        try:
            text = path.read_text()
        except OSError:
            return None
        return "".join(line if line.startswith("#") or not line.strip() else f"{number}/{line}"
                       for line in text.splitlines(keepends=True))

    def segment(self, number: int, name: str, client: str | None = None) -> Path | None:
        with self._lock:
            ch = self._channels.get(number)
        if ch is None or not name.endswith(".ts") or "/" in name or ".." in name:
            return None
        self._seen(ch, client)
        path = ch.dir / name
        return path if path.is_file() else None

    def close(self, number: int, client: str | None = None) -> None:
        """Release one browser viewer and stop the encoder when nobody else uses it."""
        if not client:
            return
        with self._lock:
            ch = self._channels.get(number)
            if ch is None or client not in ch.viewers:
                return
            ch.viewers.pop(client, None)
            log.info("stream ch%s: %s changed channel", number, client.split("#", 1)[0])
            if not ch.viewers:
                self._stop_channel(ch)
                self._channels.pop(number, None)
                log.info("stream ch%s stopped (last viewer left)", number)

    def status(self, viewer_timeout: float = 30.0) -> dict[str, Any]:
        """What the admin's System page shows: every running stream, and who is watching it."""
        now, streams, viewers = time.monotonic(), [], []
        with self._lock:
            channels = sorted(self._channels.values(), key=lambda c: c.number)
        for ch in channels:
            watching = [{"channel": ch.number, "address": ip.split("#", 1)[0], "playing": ch.playing,
                         "idle_seconds": round(now - seen)}
                        for ip, seen in sorted(ch.viewers.items()) if now - seen <= viewer_timeout]
            viewers += watching
            streams.append({"channel": ch.number, "playing": ch.playing, "since_ts": ch.started_ts,
                            "viewers": len(watching), "idle_seconds": round(now - ch.last_request),
                            "error": ch.error})
        return {"streams": streams, "viewers": viewers, "total_streams": len(streams), "total_viewers": len(viewers)}

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            for ch in list(self._channels.values()):
                self._stop_channel(ch)
            self._channels.clear()
        shutil.rmtree(self.root, ignore_errors=True)

    # --- the supervisor ---------------------------------------------------------------------

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="pitv-streams", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        conn = dbm.connect(self.cfg.db_path)   # this thread's own connection
        try:
            while not self._stop.wait(TICK_SECONDS):
                if not self._tick(conn):
                    return   # nothing left to supervise; the next request starts the thread again
        finally:
            conn.close()

    def _tick(self, conn: sqlite3.Connection) -> bool:
        settings = all_settings(conn)
        idle_limit = float(settings.get("stream_idle_seconds", 60))
        cache = MediaCache.from_settings(settings)
        with self._lock:
            channels = list(self._channels.values())
        for ch in channels:
            for ip, seen in list(ch.viewers.items()):
                if time.monotonic() - seen > idle_limit:
                    del ch.viewers[ip]
                    log.info("stream ch%s: %s stopped watching", ch.number, ip)
            if time.monotonic() - ch.last_request > idle_limit or not settings.get("streaming_enabled", True):
                log.info("stream ch%s stopped (no requests for %.0fs)", ch.number, idle_limit)
                with self._lock:
                    self._stop_channel(ch)
                    self._channels.pop(ch.number, None)
                continue
            if ch.proc is None or ch.proc.poll() is not None:
                if ch.proc is not None:
                    self._report_exit(ch)
                self._next_programme(ch, conn, settings, cache)
        with self._lock:
            return bool(self._channels)

    def _report_exit(self, ch: _Channel) -> None:
        """Say why a packaging run ended when it ended before its programme did. ffmpeg writes
        its complaint and nothing else (loglevel error), so the log is empty on a clean run."""
        if ch.proc is None or time.monotonic() >= ch.until - 2:
            return
        code = ch.proc.returncode
        try:
            tail = (ch.dir / FFMPEG_LOG).read_text(errors="replace").strip().splitlines()[-2:]
        except OSError:
            tail = []
        ch.error = f"packaging stopped early (exit {code}): {' '.join(tail)}".strip()
        log.error("stream ch%s: %s", ch.number, ch.error)

    def _next_programme(self, ch: _Channel, conn: sqlite3.Connection, settings: dict[str, Any],
                        cache: MediaCache) -> None:
        now = now_ts()
        slot = slot_at(conn, ch.channel_id, now)
        media = None
        if slot and slot.get("media_id"):
            media = dbm.row_to_dict(conn.execute("SELECT * FROM media WHERE id = ?", (slot["media_id"],)).fetchone())
        path, where, start, seconds, label = self._source(slot, media, cache, settings, now)
        profile = display.content_profile(settings)
        encoder = settings.get("stream_encoder") or ("h264_v4l2m2m" if is_raspberry_pi() else "libx264")
        ch.seq = self._next_sequence(ch)
        cmd = ffmpeg_command(path, start=start, seconds=seconds, out_dir=ch.dir, seq=ch.seq,
                             segment_seconds=int(settings.get("stream_segment_seconds", 4)),
                             media=media if where != "card" else None, where=where, profile=profile,
                             encoder=encoder, loop=where == "card", silent_audio=where == "card",
                             burst=BURST_SECONDS)
        try:
            ch.dir.mkdir(parents=True, exist_ok=True)   # a stream that was stopped took its folder with it
            with (ch.dir / FFMPEG_LOG).open("wb") as errors:
                ch.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=errors)
            ch.playing, ch.error = label, ""
            ch.until = time.monotonic() + seconds
            log.info("stream ch%s: %s from %s (%s, %ss)", ch.number, label, where, path.name, seconds)
        except OSError as exc:
            ch.proc = None
            ch.error = f"ffmpeg did not start: {exc}"
            log.error("stream ch%s: %s", ch.number, ch.error)

    def _source(self, slot: dict[str, Any] | None, media: dict[str, Any] | None, cache: MediaCache,
                settings: dict[str, Any], now: int) -> tuple[Path, str, float, int, str]:
        """The file to package next: the programme on air, else the test signal, with where to
        start, how long to run and what to call it."""
        if slot and media:
            path, where = cache.locate(media, bool(settings.get("nas_fallback", True)))
            if path:
                start = max(0.0, now - slot["start_ts"] + float(slot.get("offset") or 0))
                seconds = min(MAX_PROGRAMME_SECONDS, max(1, slot["end_ts"] - now))
                return Path(path), where, start, seconds, slot.get("title") or Path(path).stem
        seconds = CARD_SECONDS if not slot else min(CARD_SECONDS, max(1, slot["end_ts"] - now))
        return TEST_SIGNAL, "card", 0.0, seconds, (slot or {}).get("title") or "Programmes will continue shortly"

    def _next_sequence(self, ch: _Channel) -> int:
        """Carry on from the highest segment written, so appending never overwrites one a player
        is still fetching."""
        numbers = [int(p.stem[1:]) for p in ch.dir.glob(SEGMENT_GLOB) if p.stem[1:].isdigit()]
        return max(numbers) + 1 if numbers else 0

    def _stop_channel(self, ch: _Channel) -> None:
        if ch.proc and ch.proc.poll() is None:
            ch.proc.terminate()
            try:
                ch.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                ch.proc.kill()
        ch.proc = None
        shutil.rmtree(ch.dir, ignore_errors=True)

    def _clear(self, ch: _Channel) -> None:
        for p in list(ch.dir.glob(SEGMENT_GLOB)) + [ch.dir / PLAYLIST]:
            p.unlink(missing_ok=True)
