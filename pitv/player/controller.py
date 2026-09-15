"""The player daemon: one mpv, the wall clock, and the schedule.

Every half second the controller asks "what should be on the current channel right now?"
and makes mpv match it. Remote control keys, the web remote and the mpv window all feed a
single action queue that is drained on the main thread, so there are no races over mpv
state or over the player's SQLite connection, which only the main thread uses.

Two clocks: the schedule runs on the wall clock (`clock()`), which NTP may step after a power
cut; timeouts, retries and overlay lifetimes run on the monotonic clock so a step cannot
freeze or fire them.
"""

from __future__ import annotations

import json
import logging
import queue
import signal
import sqlite3
import time
from pathlib import Path
from typing import Any

from .. import db as dbm
from .. import display, sdnotify
from ..config import TEST_SIGNAL, Config
from ..db import all_settings, enabled_channels, now_ts
from ..guide import block_entry, next_programmes, slot_at
from ..logsetup import setup_logging
from ..scheduler.rules import tz_of
from .cache import MediaCache, touch_used
from .control_socket import ControlServer
from .hwdec import decode_options, is_raspberry_pi
from .input import ACTIONS, WINDOW_KEYS, EvdevInput, TerminalInput
from .maintenance import Maintenance
from .mpv_ipc import Mpv, MpvError, default_args
from .osd import (
    OVERLAY_BADGE,
    OVERLAY_GUIDE,
    OVERLAY_MESSAGE,
    OVERLAY_STATIC,
    OVERLAY_VOLUME,
    Rendered,
    Renderer,
    make_testcard,
)

log = logging.getLogger("pitv.player")

TESTCARD = "testcard"      # sentinel in `playing_path` while the test card is on screen
SLOT_RECHECK_SECONDS = 5   # how stale the cached "slot on air" may be before it is re-read
HEARTBEAT_SECONDS = 10     # the web service treats a longer silence as a wedged player
RETRY_SECONDS = 5          # re-attempt after the test card could not be shown
UNPLAYABLE_RETRY_SECONDS = 30   # technical difficulties: look for a late delivery this often
DRIFT_CHECK_SECONDS = 5
DRIFT_TOLERANCE = 6        # seconds off the schedule before playback is re-seeked
CLOCK_JUMP_SECONDS = 60    # a wall-clock step larger than this rejoins live at once
SETTINGS_RELOAD_SECONDS = 60
HEALTH_SECONDS = 300
GUIDE_REFRESH_SECONDS = 30
STATE_POLL_TIMEOUT = 1.0   # mpv property reads for the published state; a hung mpv must not stall it
MAX_PENDING_INPUT = 32     # queued key and web commands beyond this are dropped, not buffered


def validate_control(req: dict[str, Any]) -> tuple[str, Any] | str:
    """Check a control-socket request; returns (cmd, argument) or an error message. The
    socket is reachable by the web service, whose remote endpoint is public on the LAN, so
    only remote-control actions and sane numbers get through."""
    cmd = req.get("cmd")
    if cmd in ("state", "schedule-changed", "settings-changed"):
        return cmd, None
    if cmd == "key":
        key = str(req.get("key", "")).lower()
        return (cmd, key) if key in ACTIONS else f"unknown key {key!r}"
    if cmd in ("channel", "volume"):
        try:
            value = int(req.get("number" if cmd == "channel" else "volume"))
        except (TypeError, ValueError, OverflowError):   # OverflowError: JSON Infinity
            return f"{cmd} needs a number"
        if cmd == "channel" and not 1 <= value <= 999:
            return "channel number out of range"
        return cmd, max(0, min(100, value)) if cmd == "volume" else value
    return f"unknown command {cmd!r}"


def _slot_position(slot: dict[str, Any], now: int) -> float:
    """Seconds into the slot's file at schedule time `now`."""
    return max(0.0, now - slot["start_ts"] + float(slot.get("offset") or 0))


class Player:
    def __init__(self, cfg: Config, channel: int | None = None, keyboard: bool = False, now_override: str | None = None) -> None:
        self.cfg = cfg
        cfg.ensure_dirs()
        self.conn = dbm.connect(cfg.db_path)
        dbm.init_db(self.conn)
        self.settings = all_settings(self.conn)
        self.tz = tz_of(self.conn)
        self.on_pi = is_raspberry_pi() and not cfg.windowed
        self.offset = 0
        if now_override:
            # A local wall-clock time on this machine, as typed on the command line.
            target = time.mktime(time.strptime(now_override, "%Y-%m-%dT%H:%M"))
            self.offset = int(target - time.time())
        self.actions: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.channels: list[dict[str, Any]] = []
        self.channel: dict[str, Any] | None = None
        self.slot: dict[str, Any] | None = None
        self.playing_slot_id: int | None = None
        self.playing_path: str | None = None
        self.playing_where: str | None = None   # where the current file came from: cache or nas
        self.playing_entry: int | None = None   # mpv's playlist entry id for it, to spot stale events
        self.failed_slot_id: int | None = None
        self.retry_at = 0.0                     # monotonic
        self.paused = False
        self.behind_live = False
        self.standby = False
        self.volume = 80
        self.muted = False
        self.guide_open = False
        self.guide_highlight = 0
        self.guide_cursor = 0
        self.guide_rows: dict[int, list[dict[str, Any]]] = {}
        self.guide_loaded_at = 0.0              # monotonic; 0 forces a reload
        self.osd_expiry: dict[int, float] = {}  # overlay id -> monotonic expiry
        self.last_key: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.history_id: int | None = None
        self.stream_info: dict[str, Any] = {}
        self.stopping = False
        self.exit_code = 0
        self._closed = False
        self._last_drift_check = 0.0
        self._stream_info_at = 0.0
        self._started_at = time.monotonic()
        # (channel id, slot, read at, valid until) in schedule time; see `_slot_on_air`.
        self._slot_cache: tuple[int, dict[str, Any] | None, int, int] = (0, None, 0, 0)
        self._last_heartbeat = 0.0
        self._state_cache = ""
        self.initial_channel = channel or 1
        self.explicit_channel = channel is not None
        self.state_file = cfg.data_dir / "player_state.json"

        self.cache = MediaCache.from_settings(self.settings)
        self.renderer = Renderer(cfg.run_dir, self.tz)
        self.mpv = Mpv(cfg.mpv_binary, cfg.mpv_socket, self._mpv_args(), on_event=self._on_mpv_event)
        self.control = ControlServer(cfg.player_socket, self._handle_control, self.state)
        self.evdev = EvdevInput(self._on_key, self.settings["keymap"])
        self.tty = TerminalInput(self._on_key) if keyboard else None
        self.maintenance = Maintenance(cfg.db_path, self.clock, self._schedule_changed, self.cache)

    # --- helpers ---------------------------------------------------------------------------

    def clock(self) -> int:
        """Schedule time: the wall clock, shifted by `--now` when testing."""
        return int(time.time()) + self.offset

    def _mpv_args(self) -> list[str]:
        screen = display.profile(self.settings)
        args = default_args(windowed=not self.on_pi, geometry=display.preview_geometry(self.settings), title=screen.label)
        dev = self.settings["audio_device"] or "auto"
        if dev != "auto":
            args.append(f"--audio-device={dev}")
        if self.on_pi:
            args.append(f"--drm-mode={screen.drm_mode}")
            if self.settings["drm_connector"]:
                args.append(f"--drm-connector={self.settings['drm_connector']}")
            if self.settings["display_aspect"]:
                # 720x576 into a 4:3 set: pixels are not square.
                args.append(f"--monitoraspect={self.settings['display_aspect']}")
        return args + self.cfg.mpv_extra_args

    def _setup_mpv(self) -> None:
        """What a fresh mpv needs from us: window keys on the desktop, its warnings in our log,
        and the viewer's volume."""
        if not self.on_pi:   # the desktop window takes keys; on the Pi evdev does
            for key, action in WINDOW_KEYS.items():
                self.mpv.keybind(key, f"pitv {action}")
        try:
            self.mpv.command("request_log_messages", "warn")
            self.mpv.set("volume", self.volume)
            self.mpv.set("mute", self.muted)
        except MpvError as exc:
            log.warning("mpv setup: %s", exc)

    def _restart_mpv(self, args: list[str]) -> None:
        """Relaunch mpv with new arguments (a new screen, output or audio device) and rejoin
        what is on air; the player itself keeps running."""
        log.info("screen or output settings changed; restarting mpv")
        self.mpv.stop()
        self.mpv.args = args
        self.playing_path = None
        self.playing_slot_id = None
        self.osd_expiry.clear()
        try:
            self.mpv.start()
            self._setup_mpv()
        except MpvError as exc:
            self._fail(f"mpv did not restart: {exc}")
            return
        self.play_live()

    def _load_channels(self) -> None:
        self.channels = enabled_channels(self.conn)
        if self.channel is not None:
            # Pick up a rename or colour change for the channel on air; a channel that was
            # disabled keeps playing until the viewer changes channel.
            self.channel = next((c for c in self.channels if c["id"] == self.channel["id"]), self.channel)

    def _card_picture(self) -> tuple[Path, dict[str, Any]]:
        """What plays under a card: the shipped test signal, looped; the still test card only if
        the clip is missing from the install."""
        if TEST_SIGNAL.is_file():
            return TEST_SIGNAL, {"loop-file": "inf"}
        p = self.cfg.data_dir / "testcard.png"
        if not p.exists():
            make_testcard(p)
        return p, {}

    def _slot_on_air(self, channel_id: int, now: int) -> dict[str, Any] | None:
        """`slot_at` for the tick loop: re-read only at the slot boundary, after a schedule
        change, or every few seconds as a backstop, not twice a second. A read taken at a later
        schedule time than `now` (the clock stepped back) does not count."""
        cid, slot, read_at, until = self._slot_cache
        if cid == channel_id and read_at <= now < until:
            return slot
        slot = slot_at(self.conn, channel_id, now)
        until = now + SLOT_RECHECK_SECONDS
        if slot:
            until = min(slot["end_ts"], until)
        self._slot_cache = (channel_id, slot, now, until)
        return slot

    def _forget_slot(self) -> None:
        self._slot_cache = (0, None, 0, 0)

    def current_programme(self, channel_id: int, ts: int) -> dict[str, Any] | None:
        """The programme 'on' now: during an ad break, the one that follows; on a music channel,
        the whole block with the current video as its subtitle."""
        slot = slot_at(self.conn, channel_id, ts)
        if slot and slot["kind"] == "programme":
            entry = block_entry(self.conn, slot, ts)
            if entry:
                entry["subtitle"] = entry["video_title"]
                return entry
            return slot
        nxt = next_programmes(self.conn, channel_id, ts + 1, 1)
        return nxt[0] if nxt else None

    # --- lifecycle -------------------------------------------------------------------------

    def run(self) -> int:
        setup_logging(self.cfg, "player")
        log.info("player starting: pi=%s windowed=%s mpv=%s data=%s", self.on_pi, self.cfg.windowed,
                 self.cfg.mpv_binary, self.cfg.data_dir)
        self._load_channels()
        if not self.channels:
            log.error("no enabled channels")
            return 1
        self._restore_state()
        try:
            self.mpv.start()
        except MpvError as exc:
            log.error("cannot start mpv: %s", exc)
            return 1
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        try:
            self._setup_mpv()
            self.control.start()
            self.evdev.start()
            if self.tty:
                self.tty.start()
            # Ready before the clock wait: the service is up and showing the test card, and a
            # wait longer than systemd's start timeout would otherwise have the unit killed and
            # restarted forever on a Pi with no network.
            sdnotify.ready()
            self._wait_for_clock()
            if not self.stopping:
                self.maintenance.start()
                self.tune(self._first_channel(), show_badge=True)
                self._main_loop()
        finally:
            self.shutdown()
        return self.exit_code

    def _first_channel(self) -> int:
        """The channel to start on: the one asked for or last watched, else the first enabled
        one, so a channel disabled since the last run does not leave a blank screen."""
        if any(c["number"] == self.initial_channel for c in self.channels):
            return self.initial_channel
        log.warning("channel %s is not enabled; starting on channel %s", self.initial_channel,
                    self.channels[0]["number"])
        return self.channels[0]["number"]

    def _on_signal(self, signum: int, _frame: Any) -> None:
        log.info("signal %d; shutting down", signum)
        self.stopping = True

    def _fail(self, reason: str) -> None:
        """Leave the main loop with a failure status so systemd logs a failed run and restarts it."""
        if self.stopping:
            return   # already on the way out; mpv going away at shutdown is expected
        log.error("%s; exiting for systemd to restart the player", reason)
        self.exit_code = 1
        self.stopping = True

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stopping = True
        self._end_history()
        self._save_state()
        for part in (self.maintenance, self.evdev, self.control, self.tty):
            try:
                if part is not None:
                    part.stop()
            except Exception:  # keep shutting the rest down
                log.exception("stopping %s failed", type(part).__name__)
        self.mpv.stop()
        self.conn.close()

    def _restore_state(self) -> None:
        """Volume, mute and last channel survive a restart (a missing file is the first run)."""
        try:
            data = json.loads(self.state_file.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("player state file unreadable (%s): %s", self.state_file, exc)
            return
        try:
            self.volume = max(0, min(100, int(data.get("volume", self.volume))))
            self.muted = bool(data.get("muted", False))
            if not self.explicit_channel and data.get("channel"):
                self.initial_channel = int(data["channel"])
        except (TypeError, ValueError, AttributeError) as exc:
            log.warning("player state file ignored (%s): %s", self.state_file, exc)

    def _save_state(self) -> None:
        """Written atomically: a power cut mid-write must not leave a half file for the next boot."""
        tmp = self.state_file.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps({"volume": self.volume, "muted": self.muted,
                                       "channel": self.channel["number"] if self.channel else None}))
            tmp.replace(self.state_file)
        except OSError as exc:
            log.warning("could not save player state: %s", exc)

    # --- main loop ----------------------------------------------------------------------------

    def _wait_for_clock(self) -> None:
        """No RTC on the Pi: after a power cut the clock is wrong until NTP steps it. Show the
        test card and wait (bounded) for systemd-timesyncd so we resume from the real 'now'."""
        if self.offset or not self.on_pi:
            return
        limit = int(self.settings["clock_wait_seconds"])
        marker = Path("/run/systemd/timesync/synchronized")
        if marker.exists():
            return
        log.warning("clock not yet synchronised; waiting up to %ss before tuning", limit)
        sdnotify.status("waiting for time synchronisation")
        self._show_testcard("Setting the clock", "waiting for time synchronisation")
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline and not self.stopping:
            sdnotify.watchdog()
            if marker.exists():
                log.info("clock synchronised")
                return
            time.sleep(2)
        if not self.stopping:
            log.error("clock still not synchronised after %ss; carrying on with the current time", limit)

    def _main_loop(self) -> None:
        start = time.monotonic()
        last_settings = last_health = start
        last_publish = 0.0
        last_wall, last_mono = time.time(), start
        while not self.stopping:
            try:
                action, arg = self.actions.get(timeout=0.5)
            except queue.Empty:
                action = None
            if action:
                try:
                    self.do(action, arg)
                except Exception:  # one bad action must not stop the television
                    log.exception("action %s failed", action)
            try:
                self.tick()
            except Exception:  # the next tick retries
                log.exception("tick failed")
            sdnotify.watchdog()
            wall, mono = time.time(), time.monotonic()
            step = (wall - last_wall) - (mono - last_mono)
            last_wall, last_mono = wall, mono
            if abs(step) > CLOCK_JUMP_SECONDS:
                # The wall clock stepped (NTP after a power cut, or a manual change): the
                # schedule position is stale, so rejoin live immediately.
                log.warning("clock jumped by %.0fs; re-tuning to the live position", step)
                self._rejoin_live()
            if mono - last_settings > SETTINGS_RELOAD_SECONDS:
                last_settings = mono
                self._reload_settings()
            if mono - last_health > HEALTH_SECONDS:
                last_health = mono
                self._health_check()
            if mono - last_publish >= 1.0:
                last_publish = mono
                self._publish()

    def _health_check(self) -> None:
        """Log memory use; a runaway process exits so systemd restarts it cleanly (playback
        resumes at the live position within seconds)."""
        mine = sdnotify.rss_mb()
        mpv_rss = sdnotify.rss_mb(self.mpv.proc.pid) if self.mpv.proc else None
        limit = float(self.settings["memory_limit_mb"])
        log.info("health: player %.0f MB, mpv %s MB, uptime %.0f min", mine or 0,
                 f"{mpv_rss:.0f}" if mpv_rss else "?", (time.monotonic() - self._started_at) / 60)
        sdnotify.status(f"ch{self.channel['number'] if self.channel else '?'} {self.slot['title'] if self.slot else ''}"
                        f" | {mine or 0:.0f} MB")
        if mine and mine > limit:
            self._fail(f"player memory {mine:.0f} MB exceeds {limit:.0f} MB")
        elif mpv_rss and mpv_rss > limit * 1.5:
            self._fail(f"mpv memory {mpv_rss:.0f} MB is excessive")

    def _reload_settings(self) -> None:
        """Pick up admin changes to settings and channels: at once when the web service says
        so, and once a minute regardless. A change to mpv's arguments (the screen, output or
        audio device) relaunches mpv; the cache directory is read at start only."""
        try:
            self.settings = all_settings(self.conn)
            self.tz = tz_of(self.conn)
            self.evdev.set_keymap(self.settings["keymap"])
            self._load_channels()
        except sqlite3.Error as exc:
            log.warning("settings reload failed: %s", exc)
            return
        args = self._mpv_args()
        if args != self.mpv.args:
            self._restart_mpv(args)

    def tick(self) -> None:
        if not self.mpv.running():
            self._fail("mpv has gone")
            return
        mono = time.monotonic()
        for oid, expiry in list(self.osd_expiry.items()):
            if mono > expiry:
                self.mpv.overlay_remove(oid)
                del self.osd_expiry[oid]
        if self.channel is None or self.standby:
            return
        if not (self.paused or self.behind_live):
            now = self.clock()
            slot = self._slot_on_air(self.channel["id"], now)
            sid = slot["id"] if slot else None
            if sid != self.playing_slot_id:
                if sid != self.failed_slot_id or mono >= self.retry_at:
                    self.play_live()
            elif self.playing_path is None:
                if mono >= self.retry_at:
                    self.play_live()
            elif slot and self.playing_path != TESTCARD and mono - self._last_drift_check > DRIFT_CHECK_SECONDS:
                self._last_drift_check = mono
                self._correct_drift(slot, now)
        if self.guide_open and mono - self.guide_loaded_at > GUIDE_REFRESH_SECONDS:
            self._render_guide()
        if self._stream_info_at and mono >= self._stream_info_at:
            self._stream_info_at = 0.0
            self._log_stream_info()

    def _correct_drift(self, slot: dict[str, Any], now: int) -> None:
        pos = self.mpv.get("time-pos")
        if pos is None:
            return
        expected = _slot_position(slot, now)
        if abs(pos - expected) > DRIFT_TOLERANCE:
            log.info("drift %.1fs; re-seeking", pos - expected)
            try:
                self.mpv.command("seek", expected, "absolute")
            except MpvError as exc:
                log.warning("re-seek failed: %s", exc)

    # --- playback -----------------------------------------------------------------------------

    def _unpause(self) -> None:
        self.paused = False
        self.behind_live = False
        try:
            self.mpv.set("pause", False)
        except MpvError as exc:
            log.warning("unpause failed: %s", exc)

    def _rejoin_live(self) -> None:
        """Drop any pause or time shift and play what the schedule says is on now."""
        self._unpause()
        self.playing_slot_id = None
        self._forget_slot()
        if self.channel:
            self.play_live()

    def tune(self, number: int, show_badge: bool = True) -> None:
        channel = next((c for c in self.channels if c["number"] == number), None)
        if channel is None:
            log.info("no enabled channel %s", number)
            return
        self._end_history()
        switching = self.channel is not None and self.channel["id"] != channel["id"]
        self.channel = channel
        self.failed_slot_id = None
        self._forget_slot()
        self._unpause()
        if switching and self.settings["channel_switch_static"]:
            self._sync_osd_size()
            self._overlay(OVERLAY_STATIC, self.renderer.static(), ttl=0.35)
        self.play_live()
        self._save_state()
        if show_badge:
            self.show_badge()
        self._publish(force=True)

    def play_live(self) -> None:
        """Play what the schedule says is on now, from the cache, else (with `nas_fallback`) the
        NAS original, else the technical difficulties card. Every fallback is logged."""
        if self.channel is None:
            return
        now = self.clock()
        slot = slot_at(self.conn, self.channel["id"], now)
        self.slot = slot
        self.retry_at = time.monotonic() + RETRY_SECONDS
        if slot is None:
            self._show_testcard("No programme scheduled", self.channel["name"])
            self.playing_slot_id = None
            return
        if slot["kind"] == "ident" and not slot.get("media_id"):
            self._placeholder_ident(slot)
            self.playing_slot_id = slot["id"]
            return
        if slot["kind"] == "filler" or not slot.get("media_id"):
            self._show_testcard(slot.get("title") or "Programmes will continue shortly", "")
            self.playing_slot_id = slot["id"]
            return
        media = self._slot_media(slot)
        path, where = self.cache.locate(media, bool(self.settings["nas_fallback"]))
        if path is None:
            self._technical_difficulties(slot, where)
            return
        if where == "nas":
            log.error("not in the cache, playing the NAS original: ch%s '%s' (media %s)",
                      self.channel["number"], slot.get("title"), media["id"])
        self._load(slot, media, path, where, _slot_position(slot, now))

    @staticmethod
    def _slot_media(slot: dict[str, Any]) -> dict[str, Any]:
        return {"id": slot["media_id"], "path": slot["media_path"], "cache_path": slot.get("cache_path"),
                "origin": slot.get("origin") or "nas", "vcodec": slot.get("vcodec"), "interlaced": slot.get("interlaced"),
                "cache_vcodec": slot.get("cache_vcodec"), "cache_interlaced": slot.get("cache_interlaced")}

    @staticmethod
    def _decode_props(media: dict[str, Any], where: str) -> dict[str, Any]:
        """Decode by the file actually played: a transcoded cache copy is H.264 and progressive
        even when the NAS original was MPEG-2 and interlaced. When pitv_content did not report
        the copy's properties, the original's are the best guess (a plain copy keeps them)."""
        if where == "cache" and media.get("cache_vcodec"):
            return {**media, "vcodec": media["cache_vcodec"],
                    "interlaced": media["cache_interlaced"] if media.get("cache_interlaced") is not None else media.get("interlaced")}
        return media

    def _load(self, slot: dict[str, Any], media: dict[str, Any], path: str, where: str, offset: float) -> None:
        opts = decode_options(self._decode_props(media, where), self.on_pi, self.settings)
        try:
            self.playing_entry = self.mpv.loadfile(path, start=offset, options=opts)
        except MpvError as exc:
            log.error("mpv refused %s: %s", path, exc)
            self._technical_difficulties(slot, f"mpv refused the file: {exc}")
            return
        self.playing_slot_id = slot["id"]
        self.playing_path = path
        self.playing_where = where
        self.failed_slot_id = None
        self.stream_info = {}
        self.last_error = None if where == "cache" else "playing from the NAS: not in the cache"
        if where == "cache":
            touch_used(path)
        self.mpv.overlay_remove(OVERLAY_MESSAGE)
        self._start_history(slot)
        log.info("ch%s %s '%s' start=+%.0fs from=%s hwdec=%s deint=%s file=%s", self.channel["number"], slot["kind"],
                 slot["title"], offset, where, opts["hwdec"], opts["deinterlace"], path)
        self._stream_info_at = time.monotonic() + 2.0  # log codec/stream details once mpv has opened the file

    def _technical_difficulties(self, slot: dict[str, Any], reason: str) -> None:
        """Nothing playable for the slot on air. Show the card, log why, and try again shortly in
        case pitv_content delivers the file late."""
        self.failed_slot_id = slot["id"]
        self.playing_slot_id = None
        self.retry_at = time.monotonic() + UNPLAYABLE_RETRY_SECONDS
        self.last_error = f"'{slot.get('title')}' not playable: {reason}"
        log.error("TECHNICAL DIFFICULTIES ch%s '%s' (media %s): %s", self.channel["number"] if self.channel else "?",
                  slot.get("title"), slot.get("media_id"), reason)
        self._show_testcard("We are experiencing technical difficulties",
                            "Normal service will be resumed as soon as possible")

    def _log_stream_info(self) -> None:
        """One line per programme with what mpv actually ended up doing."""
        get = self.mpv.get
        vp = get("video-params") or {}
        info = {
            "vcodec": get("current-tracks/video/codec") or get("video-codec"),
            "acodec": get("audio-codec-name"), "vcodec_desc": get("video-codec"),
            "hwdec": get("hwdec-current"), "size": f"{vp.get('w')}x{vp.get('h')}",
            "fps": get("container-fps"), "aspect": vp.get("aspect"),
            "interlaced": get("deinterlace"), "vo": get("current-vo"),
            "ao": get("current-ao"), "cache_s": get("demuxer-cache-duration") or 0,
            "dropped": get("frame-drop-count"), "pos": get("time-pos"),
        }
        self.stream_info = info
        log.info("stream: %s", " ".join(f"{k}={v}" for k, v in info.items()))

    def _play_test_signal(self) -> None:
        if self.playing_path != TESTCARD:
            self._end_history()
            path, options = self._card_picture()
            self.playing_entry = self.mpv.loadfile(str(path), options=options)
            self.playing_path = TESTCARD
            self.stream_info = {}

    def _placeholder_ident(self, slot: dict[str, Any]) -> None:
        """An ident slot with no file: the channel has no idents, so the test signal runs under
        the channel badge for the slot's length."""
        try:
            self._play_test_signal()
            self.mpv.overlay_remove(OVERLAY_MESSAGE)
            self.show_badge(ttl=max(1.0, float(slot["end_ts"] - self.clock())))
        except MpvError as exc:
            self.playing_path = None   # tick retries after RETRY_SECONDS
            log.warning("could not show the stand-in ident: %s", exc)

    def _show_testcard(self, text: str, sub: str) -> None:
        """The test card with a caption. Whatever was playing has stopped, so its history
        entry is closed here rather than when the next programme starts."""
        try:
            self._play_test_signal()
            self._overlay(OVERLAY_MESSAGE, self.renderer.message(text, sub), ttl=None)
        except MpvError as exc:
            self.playing_path = None   # tick retries after RETRY_SECONDS
            log.warning("could not show the test card: %s", exc)

    def _on_mpv_event(self, ev: dict[str, Any]) -> None:
        """Runs on the mpv reader thread, so it only queues work: whether an end-file event is
        about the file still on air is decided on the main thread (see `_current_entry`)."""
        name = ev.get("event")
        if name == "end-file":
            reason = ev.get("reason")
            if reason == "eof":
                self.actions.put(("eof", ev.get("playlist_entry_id")))
            elif reason == "error":
                self.actions.put(("file-error", (ev.get("playlist_entry_id"), ev.get("file_error"))))
        elif name == "log-message" and ev.get("level") in ("error", "warn", "fatal"):
            log.warning("mpv %s: %s", ev.get("prefix"), (ev.get("text") or "").strip())
        elif name == "client-message":
            args = ev.get("args") or []
            if len(args) >= 2 and args[0] == "pitv":
                self._submit_input(("key", ("window", args[1])))
        elif name == "pitv-ipc-closed":
            self._fail("mpv closed its IPC connection")

    # --- history ----------------------------------------------------------------------------------

    def _start_history(self, slot: dict[str, Any]) -> None:
        """History is bookkeeping: a locked or failing database must not stop playback."""
        self._end_history()
        if slot["kind"] != "programme":
            return
        try:
            with dbm.tx(self.conn):
                cur = self.conn.execute("INSERT INTO history(channel_id, media_id, schedule_id, started_at, title) VALUES (?,?,?,?,?)",
                                        (slot["channel_id"], slot["media_id"], slot["id"], self.clock(), slot["title"]))
            self.history_id = int(cur.lastrowid)
        except sqlite3.Error as exc:
            log.warning("could not record history for '%s': %s", slot["title"], exc)

    def _end_history(self) -> None:
        if self.history_id is None:
            return
        history_id, self.history_id = self.history_id, None
        try:
            with dbm.tx(self.conn):
                self.conn.execute("UPDATE history SET ended_at = ? WHERE id = ?", (self.clock(), history_id))
        except sqlite3.Error as exc:
            log.warning("could not close history entry %s: %s", history_id, exc)

    # --- actions --------------------------------------------------------------------------------------

    def _submit_input(self, item: tuple[str, Any]) -> bool:
        """Queue a key or web command unless the main loop is already that far behind: a flood
        from the LAN remote must not grow the queue without bound."""
        if self.actions.qsize() >= MAX_PENDING_INPUT:
            log.warning("input queue full; dropped %s", item[0])
            return False
        self.actions.put(item)
        return True

    def _on_key(self, keyname: str, action: str | None) -> None:
        """Runs on the input thread. Unmapped keys are recorded for the admin's key-assign page
        but not queued."""
        self.last_key = {"key": keyname, "action": action, "ts": now_ts(), "source": "remote"}
        if action:
            self._submit_input(("key", ("remote", action)))

    def do(self, action: str, arg: Any) -> None:
        if action == "key":
            _, act = arg
            if act:
                self.action(act)
        elif action == "eof":
            if not self._current_entry(arg) or self.playing_path in (None, TESTCARD):
                return
            log.info("end of file reached (%s)", self.playing_path)
            self.behind_live = False
            self.paused = False
            slot = slot_at(self.conn, self.channel["id"], self.clock()) if self.channel else None
            if slot is not None and slot["id"] == self.playing_slot_id:
                # The file ended before its slot did (it is shorter than scheduled): hold the
                # continuity card until the next slot rather than reloading past the end.
                self._show_testcard("Programmes will continue shortly", "")
                return
            self.playing_slot_id = None
            self.play_live()
        elif action == "file-error":
            entry, error = arg
            if not self._current_entry(entry):
                return
            if self.playing_path == TESTCARD:
                log.error("mpv cannot show the test card: %s", error)
            else:
                log.error("mpv failed to play %s: %s", self.playing_path, error)
                self._file_error(error)
        elif action == "tune":
            self.tune(int(arg))
        elif action == "volume":
            self._set_volume(int(arg))
        elif action == "settings-changed":
            self._reload_settings()
        elif action == "schedule-changed":
            self.guide_loaded_at = 0.0
            self._forget_slot()
            self.cache.invalidate()

    def _current_entry(self, entry: int | None) -> bool:
        """Whether an end-file event is for the file now loaded. A file that ends just as its
        slot does reports its end after the next file has been loaded; acting on that report
        would stop the new programme. Builds that report no entry ids are taken at their word."""
        return entry is None or self.playing_entry is None or entry == self.playing_entry

    def _file_error(self, error: Any) -> None:
        """mpv could not play the file on air. A cache copy that is corrupt falls back to the
        NAS original when fallback is on; anything else is technical difficulties."""
        slot = self.slot
        if slot is None or slot["id"] != self.playing_slot_id:
            return   # an error for a file that is no longer on air
        media = self._slot_media(slot) if slot.get("media_id") else None
        if (media and media["origin"] == "nas" and self.settings["nas_fallback"]
                and self.playing_where == "cache" and Path(media["path"]).is_file()):
            log.error("cache copy unplayable (%s), falling back to the NAS original: %s", error, media["path"])
            self._load(slot, media, media["path"], "nas", _slot_position(slot, self.clock()))
        else:
            self._technical_difficulties(slot, f"playback error: {error}")

    def action(self, act: str) -> None:
        if act == "quit":
            log.info("quit requested from the keyboard")
            self.stopping = True
            return
        if self.standby or act == "power":
            # Standby is a television's off switch: any key wakes it and is otherwise ignored.
            self._set_standby(not self.standby)
            self._publish(force=True)
            return
        if act.startswith("channel_"):
            self.close_guide()
            self.tune(int(act.split("_")[1]))
            return
        if self.guide_open:
            self._guide_key(act)
            return
        if act == "guide":
            self.open_guide()
        elif act in ("info", "ok"):
            self.show_badge()
        elif act in ("ch_up", "ch_down") or (act in ("up", "down") and self.settings["nav_keys_change_channel"]):
            self._step_channel(1 if act in ("ch_up", "up") else -1)
        elif act in ("vol_up", "vol_down") or (act in ("right", "left") and self.settings["nav_keys_change_volume"]):
            self._set_volume(self.volume + (5 if act in ("vol_up", "right") else -5))
        elif act == "mute":
            self.muted = not self.muted
            self.mpv.set("mute", self.muted)
            self._overlay(OVERLAY_VOLUME, self.renderer.volume(self.volume, self.muted), ttl=2)
            self._save_state()
        elif act == "pause":
            self.paused = not self.paused
            log.info("%s", "paused" if self.paused else "resumed")
            self.mpv.set("pause", self.paused)
            if self.paused:
                self.behind_live = True
            self.show_badge()
        elif act == "restart":
            if self.slot and self.playing_path not in (None, TESTCARD):
                self.behind_live = True
                try:
                    self.mpv.command("seek", float(self.slot.get("offset") or 0), "absolute")
                except MpvError as exc:
                    log.warning("restart seek failed: %s", exc)
                self.show_badge()
        elif act == "back" and self.behind_live:
            self._rejoin_live()
            self.show_badge()
        self._publish(force=True)

    def _set_standby(self, on: bool) -> None:
        """Picture off and sound muted; the schedule keeps running so waking rejoins live."""
        self.standby = on
        log.info("standby %s", "on" if on else "off")
        try:
            if on:
                self.close_guide()
                for oid in list(self.osd_expiry):
                    self.mpv.overlay_remove(oid)
                self.osd_expiry.clear()
                self.mpv.set("mute", True)
                self.mpv.set("vid", "no")
            else:
                self.mpv.set("vid", "auto")
                self.mpv.set("mute", self.muted)
                self._rejoin_live()
                self.show_badge()
        except MpvError as exc:
            log.warning("standby switch failed: %s", exc)

    def _step_channel(self, step: int) -> None:
        if not self.channels or self.channel is None:
            return
        idx = next((i for i, c in enumerate(self.channels) if c["id"] == self.channel["id"]), 0)
        self.tune(self.channels[(idx + step) % len(self.channels)]["number"])

    def _set_volume(self, value: int) -> None:
        self.volume = max(0, min(100, value))
        if self.muted:
            self.muted = False
            self.mpv.set("mute", False)
        self.mpv.set("volume", self.volume)
        self._overlay(OVERLAY_VOLUME, self.renderer.volume(self.volume, self.muted), ttl=2)
        self._save_state()

    # --- OSD ---------------------------------------------------------------------------------------------

    def _sync_osd_size(self) -> None:
        self.renderer.configure(float(self.settings["osd_safe_margin"]), float(self.settings["osd_scale"]), self.tz)
        dims = self.mpv.get("osd-dimensions") or {}
        w, h = dims.get("w"), dims.get("h")
        if not (w and h):
            w, h = self.mpv.get("osd-width"), self.mpv.get("osd-height")
        if w and h:
            self.renderer.resize(int(w), int(h))

    def _overlay(self, oid: int, rendered: Rendered, ttl: float | None) -> None:
        path, w, h, x, y = rendered
        try:
            self.mpv.overlay_add(oid, x, y, path, w, h)
        except MpvError as exc:
            log.warning("overlay %d failed: %s", oid, exc)
            return
        if ttl:
            self.osd_expiry[oid] = time.monotonic() + ttl
        else:
            self.osd_expiry.pop(oid, None)

    def show_badge(self, ttl: float | None = None) -> None:
        if not self.channel:
            return
        self._sync_osd_size()
        now = self.clock()
        cur = self.current_programme(self.channel["id"], now)
        nxt = next_programmes(self.conn, self.channel["id"], cur["end_ts"] if cur else now, 1)
        position = None
        if cur and cur["end_ts"] > cur["start_ts"]:
            position = (now - cur["start_ts"]) / (cur["end_ts"] - cur["start_ts"])
        self._overlay(OVERLAY_BADGE, self.renderer.badge(self.channel, cur, nxt[0] if nxt else None, position, self.behind_live),
                      ttl=ttl or float(self.settings["badge_seconds"]))

    def open_guide(self) -> None:
        if not self.channels:
            return
        self.guide_open = True
        self.guide_highlight = next((i for i, c in enumerate(self.channels) if self.channel and c["id"] == self.channel["id"]), 0)
        self.guide_cursor = 0
        self.mpv.overlay_remove(OVERLAY_BADGE)
        self.osd_expiry.pop(OVERLAY_BADGE, None)
        self._render_guide(reload=True)

    def close_guide(self) -> None:
        if self.guide_open:
            self.guide_open = False
            self.mpv.overlay_remove(OVERLAY_GUIDE)

    def _load_guide_rows(self) -> None:
        now = self.clock()
        rows: dict[int, list[dict[str, Any]]] = {}
        for ch in self.channels:
            cur = self.current_programme(ch["id"], now)
            items = [cur] if cur else []
            items += next_programmes(self.conn, ch["id"], cur["end_ts"] if cur else now, 40)
            rows[ch["id"]] = items
        self.guide_rows = rows
        self.guide_loaded_at = time.monotonic()

    def _highlighted(self) -> dict[str, Any]:
        """The guide's highlighted channel, kept in range if the channel list shrank."""
        self.guide_highlight %= len(self.channels)
        return self.channels[self.guide_highlight]

    def _render_guide(self, reload: bool = False) -> None:
        if not self.channels:
            self.close_guide()
            return
        if reload or time.monotonic() - self.guide_loaded_at > GUIDE_REFRESH_SECONDS:
            self._load_guide_rows()
        self._sync_osd_size()
        rendered = self.renderer.guide(self.channels, self.guide_rows, self._highlighted()["id"],
                                       self.guide_cursor, self.clock())
        self._overlay(OVERLAY_GUIDE, rendered, ttl=None)

    def _guide_key(self, act: str) -> None:
        if not self.channels or act in ("guide", "back"):
            self.close_guide()
        elif act in ("up", "down"):
            self.guide_highlight = (self.guide_highlight + (-1 if act == "up" else 1)) % len(self.channels)
            self.guide_cursor = 0
            self._render_guide()
        elif act == "left":
            self.guide_cursor = max(0, self.guide_cursor - 1)
            self._render_guide()
        elif act == "right":
            last = len(self.guide_rows.get(self._highlighted()["id"], [])) - 1
            self.guide_cursor = max(0, min(last, self.guide_cursor + 1))
            self._render_guide()
        elif act == "ok":
            number = self._highlighted()["number"]
            self.close_guide()
            self.tune(number)
        elif act in ("vol_up", "vol_down", "mute", "pause"):
            self.close_guide()
            self.action(act)

    # --- control socket / state ---------------------------------------------------------------------------

    def _handle_control(self, req: dict[str, Any]) -> dict[str, Any]:
        """Runs on a control socket thread: validates, then hands the work to the main thread."""
        parsed = validate_control(req)
        if isinstance(parsed, str):
            return {"ok": False, "error": parsed}
        cmd, arg = parsed
        if cmd == "state":
            return {"ok": True, **self.state()}
        if cmd in ("settings-changed", "schedule-changed"):
            self.actions.put((cmd, None))   # internal notifications are never dropped
            return {"ok": True}
        if cmd == "key":
            self.last_key = {"key": f"WEB_{arg.upper()}", "action": arg, "ts": now_ts(), "source": "web"}
            item: tuple[str, Any] = ("key", ("web", arg))
        else:
            item = ("tune" if cmd == "channel" else "volume", arg)
        return {"ok": True} if self._submit_input(item) else {"ok": False, "error": "player busy"}

    def _schedule_changed(self) -> None:
        self.actions.put(("schedule-changed", None))

    def state(self) -> dict[str, Any]:
        """Snapshot for the web service. Called from the main loop and from control socket
        threads, so it only reads player attributes and asks mpv (whose client is thread-safe)."""
        slot, channel, path = self.slot, self.channel, self.playing_path
        playing = path not in (None, TESTCARD)
        pos = hwdec = None
        if playing and self.mpv.alive:
            pos = self.mpv.get("time-pos", timeout=STATE_POLL_TIMEOUT)
            hwdec = self.mpv.get("hwdec-current", timeout=STATE_POLL_TIMEOUT)
        return {
            "online": True, "ts": self.clock(), "clock_offset": self.offset,
            "channel": {"id": channel["id"], "number": channel["number"], "name": channel["name"],
                        "colour": channel.get("colour")} if channel else None,
            "slot": {k: slot.get(k) for k in ("id", "kind", "title", "subtitle", "start_ts", "end_ts", "media_id")} if slot else None,
            "position": pos, "paused": self.paused, "behind_live": self.behind_live, "standby": self.standby,
            "volume": self.volume, "muted": self.muted, "guide_open": self.guide_open,
            "playing": playing, "testcard": path == TESTCARD,
            "hwdec": hwdec, "on_pi": self.on_pi, "last_key": self.last_key, "error": self.last_error,
            "cache": self.cache.usage(), "maintenance": dict(self.maintenance.status),
            "stream": self.stream_info, "file": path if playing else None,
            "input_devices": self.evdev.device_names,
        }

    def _publish(self, force: bool = False) -> None:
        try:
            st = self.state()
        except Exception:  # a broken state snapshot must not stop playback
            log.exception("state snapshot failed")
            return
        key = json.dumps({k: v for k, v in st.items() if k not in ("ts", "position", "cache")}, sort_keys=True)
        mono = time.monotonic()
        if force or key != self._state_cache or st["playing"] or mono - self._last_heartbeat >= HEARTBEAT_SECONDS:
            self._state_cache = key
            self._last_heartbeat = mono
            self.control.broadcast(st)


def run_player(cfg: Config, channel: int | None = None, keyboard: bool = False, now_override: str | None = None) -> int:
    return Player(cfg, channel=channel, keyboard=keyboard, now_override=now_override).run()
