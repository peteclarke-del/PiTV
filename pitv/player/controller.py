"""The player daemon: one mpv, the wall clock, and the schedule.

Every half second the controller asks "what should be on the current channel right now?"
and makes mpv match it. Remote control keys, the web remote and the mpv window all feed a
single action queue that is drained on the main thread, so there are no races over mpv.
"""

from __future__ import annotations

import json
import logging
import queue
import signal
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import db as dbm
from .. import sdnotify
from ..config import Config
from ..db import all_settings, enabled_channels, now_ts
from ..guide import block_entry, next_programmes, slot_at
from ..logsetup import setup_logging
from ..readiness import mark_missing
from ..scheduler.build import rebuild_from
from .cache import MediaCache
from .control_socket import ControlServer
from .hwdec import decode_options, is_raspberry_pi
from .input import ACTIONS, EvdevInput, TerminalInput
from .maintenance import Maintenance
from .mpv_ipc import Mpv, MpvError, default_args
from .osd import (OVERLAY_BADGE, OVERLAY_GUIDE, OVERLAY_MESSAGE, OVERLAY_STATIC, OVERLAY_VOLUME, Renderer,
                  make_testcard)

log = logging.getLogger("pitv.player")

TESTCARD = "testcard"   # sentinel in `playing_path` while the test card is on screen
SLOT_RECHECK_SECONDS = 5   # how stale the cached "slot on air" may be before it is re-read
HEARTBEAT_SECONDS = 10     # the web service treats a longer silence as a wedged player
SUBSTITUTION_MEMORY = 86400

WINDOW_KEYS = {"UP": "up", "DOWN": "down", "LEFT": "left", "RIGHT": "right", "ENTER": "ok", "ESC": "back",
               "g": "guide", "i": "info", "SPACE": "pause", "m": "mute", "+": "vol_up", "=": "vol_up",
               "-": "vol_down", "]": "ch_up", "[": "ch_down", "r": "restart", "p": "power", "q": "quit"}
for _n in range(1, 10):
    WINDOW_KEYS[str(_n)] = f"channel_{_n}"


def validate_control(req: dict[str, Any]) -> tuple[str, Any] | str:
    """Check a control-socket request; returns (cmd, argument) or an error message. The
    socket is reachable by the web service, whose remote endpoint is public on the LAN, so
    only remote-control actions and sane numbers get through."""
    cmd = req.get("cmd")
    if cmd in ("state", "schedule-changed", "quit"):
        return cmd, None
    if cmd == "key":
        key = str(req.get("key", "")).lower()
        return (cmd, key) if key in ACTIONS else f"unknown key {key!r}"
    if cmd in ("channel", "volume"):
        try:
            value = int(req.get("number" if cmd == "channel" else "volume"))
        except (TypeError, ValueError):
            return f"{cmd} needs a number"
        if cmd == "channel" and not 1 <= value <= 999:
            return "channel number out of range"
        return cmd, max(0, min(100, value)) if cmd == "volume" else value
    return f"unknown command {cmd!r}"


class Player:
    def __init__(self, cfg: Config, channel: int | None = None, keyboard: bool = False, now_override: str | None = None) -> None:
        self.cfg = cfg
        cfg.ensure_dirs()
        # Used on the main thread only: input, mpv events and the control socket hand work
        # over through `self.actions`, and the maintenance thread opens its own connection.
        self.conn = dbm.connect(cfg.db_path)
        dbm.init_db(self.conn)
        self.settings = all_settings(self.conn)
        self.on_pi = is_raspberry_pi() and not cfg.windowed
        self.offset = 0
        if now_override:
            target = datetime.strptime(now_override, "%Y-%m-%dT%H:%M")
            self.offset = int(target.timestamp() - time.time())
        self.actions: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.channels: list[dict[str, Any]] = []
        self.channel: dict[str, Any] | None = None
        self.slot: dict[str, Any] | None = None
        self.playing_slot_id: int | None = None
        self.playing_path: str | None = None
        self.failed_slot_id: int | None = None
        self.retry_at = 0.0
        self.paused = False
        self.behind_live = False
        self.standby = False
        self.volume = 80
        self.muted = False
        self.guide_open = False
        self.guide_highlight = 0
        self.guide_cursor = 0
        self.guide_rows: dict[int, list[dict[str, Any]]] = {}
        self.guide_loaded_at = 0.0
        self.osd_expiry: dict[int, float] = {}
        self.last_key: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.history_id: int | None = None
        self.position: float | None = None
        self.hwdec_current: str | None = None
        self.stopping = False
        self._last_drift_check = 0.0
        self._stream_info_at = 0.0
        self._started_at = time.time()
        self._substituted: dict[int, float] = {}   # slot id -> when a live substitution was tried
        self._slot_cache: tuple[int, dict[str, Any] | None, int] = (0, None, 0)  # (channel, slot, valid until)
        self._last_heartbeat = 0.0
        self.stream_info: dict[str, Any] = {}
        self._state_cache: str = ""
        self.keyboard = keyboard
        self.initial_channel = channel or 1
        self.explicit_channel = channel is not None
        self.state_file = cfg.data_dir / "player_state.json"

        self.cache = MediaCache.from_settings(self.settings)
        self.renderer = Renderer(cfg.run_dir)
        self.mpv = Mpv(cfg.mpv_binary, cfg.mpv_socket, self._mpv_args(), on_event=self._on_mpv_event)
        self.control = ControlServer(cfg.player_socket, self._handle_control, self.state)
        self.evdev = EvdevInput(self._on_key, self.settings.get("keymap") or {})
        self.tty = TerminalInput(self._on_key) if keyboard else None
        self.maintenance = Maintenance(cfg.db_path, cfg.ffprobe_binary, self.clock, self._schedule_changed, self.cache)

    # --- helpers ---------------------------------------------------------------------------

    def clock(self) -> int:
        return int(time.time()) + self.offset

    def _mpv_args(self) -> list[str]:
        args = default_args(windowed=not self.on_pi, osd_socket_dir=self.cfg.run_dir)
        dev = self.settings.get("audio_device") or "auto"
        if dev != "auto":
            args.append(f"--audio-device={dev}")
        if self.on_pi:
            conn_name = self.settings.get("drm_connector") or ""
            if conn_name:
                args.append(f"--drm-connector={conn_name}")
            aspect = self.settings.get("display_aspect") or ""
            if aspect:
                args.append(f"--monitoraspect={aspect}")  # 720x576 into a 4:3 set: pixels are not square
        return args + self.cfg.mpv_extra_args

    def _load_channels(self) -> None:
        self.channels = enabled_channels(self.conn)

    def _testcard(self) -> Path:
        p = self.cfg.data_dir / "testcard.png"
        if not p.exists():
            make_testcard(p)
        return p

    def slot_at(self, channel_id: int, ts: int) -> dict[str, Any] | None:
        return slot_at(self.conn, channel_id, ts)

    def _slot_on_air(self, channel_id: int, now: int) -> dict[str, Any] | None:
        """`slot_at` for the tick loop: re-read only at the slot boundary, after a schedule
        change, or every few seconds as a backstop, not twice a second."""
        cid, slot, until = self._slot_cache
        if cid == channel_id and now < until:
            return slot
        slot = self.slot_at(channel_id, now)
        until = min(slot["end_ts"] if slot else now + SLOT_RECHECK_SECONDS, now + SLOT_RECHECK_SECONDS)
        self._slot_cache = (channel_id, slot, until)
        return slot

    def _forget_slot(self) -> None:
        self._slot_cache = (0, None, 0)

    def next_programmes(self, channel_id: int, after: int, n: int = 12) -> list[dict[str, Any]]:
        return next_programmes(self.conn, channel_id, after, n)

    def current_programme(self, channel_id: int, ts: int) -> dict[str, Any] | None:
        """The programme 'on' now: during an ad break, the one that follows; on a music channel,
        the whole block with the current video as its subtitle."""
        slot = self.slot_at(channel_id, ts)
        if slot and slot["kind"] == "programme":
            entry = block_entry(self.conn, slot, ts)
            if entry:
                entry["subtitle"] = entry["video_title"]
                return entry
            return slot
        nxt = self.next_programmes(channel_id, ts + 1, 1)
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
        for key, action in WINDOW_KEYS.items():
            self.mpv.keybind(key, f"pitv {action}")
        try:
            self.mpv.command("request_log_messages", "warn")  # surface mpv's own warnings/errors in our log
            self.mpv.set("volume", self.volume)
            self.mpv.set("mute", self.muted)
        except MpvError as exc:
            log.warning("mpv setup: %s", exc)
        self.control.start()
        self.evdev.start()
        if self.tty:
            self.tty.start()
        self._wait_for_clock()
        sdnotify.ready()
        self.maintenance.start()
        signal.signal(signal.SIGTERM, lambda *_: self.actions.put(("quit", None)))
        signal.signal(signal.SIGINT, lambda *_: self.actions.put(("quit", None)))
        self.tune(self.initial_channel, show_badge=True)
        try:
            self._main_loop()
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        self._end_history()
        self._save_state()
        for part in (self.maintenance, self.evdev, self.control, self.tty):
            try:
                if part is not None:
                    part.stop()
            except Exception:  # noqa: BLE001 - keep shutting the rest down
                log.exception("stopping %s failed", type(part).__name__)
        self.mpv.stop()

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
        limit = int(self.settings.get("clock_wait_seconds", 120))
        marker = Path("/run/systemd/timesync/synchronized")
        if marker.exists():
            return
        log.warning("clock not yet synchronised; waiting up to %ss before tuning", limit)
        self._show_testcard("Setting the clock", "waiting for time synchronisation")
        deadline = time.time() + limit
        while time.time() < deadline and not self.stopping:
            sdnotify.watchdog()
            if marker.exists():
                log.info("clock synchronised")
                return
            time.sleep(2)
        log.error("clock still not synchronised after %ss; carrying on with the current time", limit)

    def _main_loop(self) -> None:
        last_settings = time.time()
        last_publish = 0.0
        last_wall = time.time()
        last_health = time.time()
        while not self.stopping:
            try:
                action, arg = self.actions.get(timeout=0.5)
            except queue.Empty:
                action = None
            if action == "quit":
                return
            if action:
                try:
                    self.do(action, arg)
                except Exception:  # noqa: BLE001
                    log.exception("action %s failed", action)
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("tick failed")
            now = time.time()
            sdnotify.watchdog()
            if abs(now - last_wall) > 60:
                # The wall clock stepped (NTP after a power cut, or a manual change): the
                # schedule position is stale, so rejoin live immediately.
                log.warning("clock jumped by %.0fs; re-tuning to the live position", now - last_wall)
                self.behind_live = False
                self.paused = False
                self.playing_slot_id = None
                try:
                    self.mpv.set("pause", False)
                except MpvError as exc:
                    log.warning("unpause after clock jump failed: %s", exc)
                if self.channel:
                    self.play_live()
            last_wall = now
            if now - last_settings > 60:
                last_settings = now
                self._reload_settings()
            if now - last_health > 300:
                last_health = now
                self._health_check()
            if now - last_publish >= 1.0:
                last_publish = now
                self._publish()

    def _health_check(self) -> None:
        """Log memory use; a runaway process exits so systemd restarts it cleanly (playback
        resumes at the live position within seconds)."""
        mine = sdnotify.rss_mb()
        mpv_rss = sdnotify.rss_mb(self.mpv.proc.pid) if self.mpv.proc else None
        limit = float(self.settings.get("memory_limit_mb", 700))
        log.info("health: player %.0f MB, mpv %s MB, uptime %.0f min", mine or 0,
                 f"{mpv_rss:.0f}" if mpv_rss else "?", (time.time() - self._started_at) / 60)
        sdnotify.status(f"ch{self.channel['number'] if self.channel else '?'} {self.slot['title'] if self.slot else ''}"
                        f" | {mine or 0:.0f} MB")
        if mine and mine > limit:
            log.error("player memory %.0f MB exceeds %.0f MB; restarting", mine, limit)
            self.stopping = True
        elif mpv_rss and mpv_rss > limit * 1.5:
            log.error("mpv memory %.0f MB is excessive; restarting the player", mpv_rss)
            self.stopping = True

    def _reload_settings(self) -> None:
        """Pick up admin changes to settings and channels once a minute (the cache directory
        and mpv arguments are read at start only; those need a player restart)."""
        try:
            self.settings = all_settings(self.conn)
            self.evdev.set_keymap(self.settings.get("keymap") or {})
            self._load_channels()
        except sqlite3.Error as exc:
            log.warning("settings reload failed: %s", exc)

    def tick(self) -> None:
        if not self.mpv.running():
            log.error("mpv has gone; exiting for systemd to restart us")
            self.stopping = True
            return
        now = self.clock()
        for oid, expiry in list(self.osd_expiry.items()):
            if time.time() > expiry:
                self.mpv.overlay_remove(oid)
                del self.osd_expiry[oid]
        if self.channel is None or self.standby:
            return
        if not (self.paused or self.behind_live):
            slot = self._slot_on_air(self.channel["id"], now)
            sid = slot["id"] if slot else None
            if sid != self.playing_slot_id:
                if sid != self.failed_slot_id or time.time() >= self.retry_at:
                    self.play_live()
            elif self.playing_path and time.time() - self._last_drift_check > 5:
                self._last_drift_check = time.time()
                pos = self.mpv.get("time-pos")
                if pos is not None and slot:
                    expected = now - slot["start_ts"] + slot["offset"]
                    if abs(pos - expected) > 6:
                        log.info("drift %.1fs; re-seeking", pos - expected)
                        try:
                            self.mpv.command("seek", expected, "absolute")
                        except MpvError as exc:
                            log.warning("re-seek failed: %s", exc)
            elif self.playing_path is None and time.time() >= self.retry_at:
                self.play_live()
        if self.guide_open and time.time() - self.guide_loaded_at > 30:
            self._render_guide()
        if self._stream_info_at and time.time() >= self._stream_info_at:
            self._stream_info_at = 0.0
            self._log_stream_info()

    # --- playback -----------------------------------------------------------------------------

    def tune(self, number: int, show_badge: bool = True) -> None:
        channel = next((c for c in self.channels if c["number"] == number), None)
        if channel is None:
            return
        self._end_history()
        switching = self.channel is not None and self.channel["id"] != channel["id"]
        self.channel = channel
        self.paused = False
        self.behind_live = False
        self.failed_slot_id = None
        self._forget_slot()
        try:
            self.mpv.set("pause", False)
        except MpvError as exc:
            log.warning("unpause on tune failed: %s", exc)
        if switching and self.settings.get("channel_switch_static", True):
            self._sync_osd_size()
            self._overlay(OVERLAY_STATIC, self.renderer.static(), ttl=0.35)
        self.play_live()
        self._save_state()
        if show_badge:
            self.show_badge()
        self._publish(force=True)

    def play_live(self) -> None:
        assert self.channel is not None
        now = self.clock()
        slot = self.slot_at(self.channel["id"], now)
        self.slot = slot
        self.retry_at = time.time() + 5
        if slot is None:
            self._show_testcard("No programme scheduled", f"{self.channel['name']}")
            self.playing_slot_id = None
            return
        if slot["kind"] == "filler" or not slot.get("media_id") or slot.get("media_missing"):
            self._show_testcard(slot.get("title") or "Programmes will continue shortly", "")
            self.playing_slot_id = slot["id"]
            return
        media = {"id": slot["media_id"], "path": slot["media_path"], "transcoded_path": slot.get("transcoded_path"),
                 "vcodec": slot.get("vcodec"), "interlaced": slot.get("interlaced")}
        path = self.cache.resolve(media)
        if path is None:
            self.failed_slot_id = slot["id"]
            self.playing_slot_id = None
            self.playing_path = None
            self.last_error = f"file not available: {slot['media_path']}"
            log.error("%s (channel %s, '%s')", self.last_error, self.channel["number"], slot.get("title"))
            if self._substitute_live(slot):
                return
            self._show_testcard("Waiting for the file server", slot.get("title") or "")
            return
        offset = max(0.0, now - slot["start_ts"] + float(slot.get("offset") or 0))
        opts = decode_options(media, self.on_pi, self.settings)
        try:
            for k, v in opts.items():
                self.mpv.set(k, v)
            self.mpv.loadfile(path, start=offset)
        except MpvError as exc:
            self.last_error = f"mpv: {exc}"
            log.warning(self.last_error)
            self.failed_slot_id = slot["id"]
            self.playing_slot_id = None
            self.playing_path = None
            return
        self.playing_slot_id = slot["id"]
        self.playing_path = path
        self.failed_slot_id = None
        self.last_error = None
        self.mpv.overlay_remove(OVERLAY_MESSAGE)
        self._start_history(slot)
        origin = "cache" if self.cache.cached_path(media["id"], media["path"] or "") else (
            "transcoded" if path == media.get("transcoded_path") else "source")
        log.info("ch%s %s '%s' start=+%.0fs origin=%s hwdec=%s deint=%s file=%s", self.channel["number"], slot["kind"],
                 slot["title"], offset, origin, opts.get("hwdec"), opts.get("deinterlace"), path)
        self._stream_info_at = time.time() + 2.0  # log codec/stream details once mpv has opened the file

    def _log_stream_info(self) -> None:
        """One line per programme with what mpv actually ended up doing."""
        try:
            vp = self.mpv.get("video-params") or {}
            info = {
                "vcodec": self.mpv.get("current-tracks/video/codec") or self.mpv.get("video-codec"),
                "acodec": self.mpv.get("audio-codec-name"), "vcodec_desc": self.mpv.get("video-codec"),
                "hwdec": self.mpv.get("hwdec-current"), "size": f"{vp.get('w')}x{vp.get('h')}",
                "fps": self.mpv.get("container-fps"), "aspect": vp.get("aspect"),
                "interlaced": self.mpv.get("deinterlace"), "vo": self.mpv.get("current-vo"),
                "ao": self.mpv.get("current-ao"), "cache_s": (self.mpv.get("demuxer-cache-duration") or 0),
                "dropped": self.mpv.get("frame-drop-count"), "pos": self.mpv.get("time-pos"),
            }
            self.stream_info = info
            log.info("stream: %s", " ".join(f"{k}={v}" for k, v in info.items()))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read stream info: %s", exc)

    def _substitute_live(self, slot: dict[str, Any]) -> bool:
        """The file for the slot on air is gone but its share is mounted: replace it and
        rebalance the rest of the channel's day, then play whatever is now scheduled."""
        src = self.conn.execute("SELECT s.path FROM media m JOIN sources s ON s.id = m.source_id WHERE m.id = ?",
                                (slot["media_id"],)).fetchone()
        if not src or not Path(src["path"]).is_dir():
            return False  # the whole share is down; nothing sensible to substitute with
        cutoff = time.time() - SUBSTITUTION_MEMORY
        self._substituted = {k: v for k, v in self._substituted.items() if v > cutoff}
        if slot["id"] in self._substituted:
            return False  # already tried once for this slot; do not loop on a bad rebuild
        self._substituted[slot["id"]] = time.time()
        try:
            mark_missing(self.conn, {slot["media_id"]}, "File not found when it was due on air")
            result = rebuild_from(self.conn, self.channel["id"], self.clock(), now=self.clock(),
                                  exclude_media_ids={slot["media_id"]})
            log.error("substituted missing '%s' on channel %s and rebalanced the day: %s", slot.get("title"),
                      self.channel["number"], result.get("summary"))
        except Exception:  # noqa: BLE001
            log.exception("live substitution failed")
            return False
        self.failed_slot_id = None
        self._forget_slot()
        self.play_live()
        self._schedule_changed()
        return True

    def _show_testcard(self, text: str, sub: str) -> None:
        try:
            if self.playing_path != TESTCARD:
                self.mpv.loadfile(str(self._testcard()))
                self.playing_path = TESTCARD
            self._overlay(OVERLAY_MESSAGE, self.renderer.message(text, sub), ttl=None)
        except MpvError as exc:
            log.warning("could not show the test card: %s", exc)

    def _on_mpv_event(self, ev: dict[str, Any]) -> None:
        name = ev.get("event")
        if name == "end-file":
            reason = ev.get("reason")
            if reason == "eof" and self.playing_path not in (None, TESTCARD):
                log.info("end of file reached (%s)", self.playing_path)
                self.actions.put(("eof", None))
            elif reason == "error":
                log.error("mpv failed to play %s: %s", self.playing_path, ev.get("file_error"))
                self.actions.put(("file-error", ev.get("file_error")))
        elif name == "log-message" and ev.get("level") in ("error", "warn", "fatal"):
            log.warning("mpv %s: %s", ev.get("prefix"), (ev.get("text") or "").strip())
        elif name == "client-message":
            args = ev.get("args") or []
            if len(args) >= 2 and args[0] == "pitv":
                self.actions.put(("key", ("window", args[1])))
        elif name == "pitv-ipc-closed":
            self.actions.put(("quit", None))

    # --- history ----------------------------------------------------------------------------------

    def _start_history(self, slot: dict[str, Any]) -> None:
        self._end_history()
        if slot["kind"] != "programme":
            return
        with dbm.tx(self.conn):
            cur = self.conn.execute("INSERT INTO history(channel_id, media_id, schedule_id, started_at, title) VALUES (?,?,?,?,?)",
                                    (slot["channel_id"], slot["media_id"], slot["id"], self.clock(), slot["title"]))
        self.history_id = int(cur.lastrowid)

    def _end_history(self) -> None:
        if self.history_id:
            with dbm.tx(self.conn):
                self.conn.execute("UPDATE history SET ended_at = ? WHERE id = ?", (self.clock(), self.history_id))
            self.history_id = None

    # --- actions --------------------------------------------------------------------------------------

    def _on_key(self, keyname: str, action: str | None) -> None:
        self.last_key = {"key": keyname, "action": action, "ts": now_ts(), "source": "remote"}
        self.actions.put(("key", ("remote", action)))

    def do(self, action: str, arg: Any) -> None:
        if action == "key":
            _, act = arg
            if act:
                self.action(act)
        elif action == "eof":
            self.behind_live = False
            self.paused = False
            self.playing_slot_id = None
            self.play_live()
        elif action == "file-error":
            self.last_error = f"playback error: {arg}"
            self.failed_slot_id = self.playing_slot_id
            self.playing_slot_id = None
            self.playing_path = None
            self.retry_at = time.time() + 10
            if self.channel:
                self._show_testcard("Playback problem", str(arg or ""))
        elif action == "tune":
            self.tune(int(arg))
        elif action == "volume":
            self._set_volume(int(arg))
        elif action == "schedule-changed":
            self.guide_loaded_at = 0
            self._forget_slot()
            self.cache.invalidate()

    def action(self, act: str) -> None:
        if act == "quit":
            self.stopping = True
            return
        if self.standby or act == "power":
            # Standby is a television's off switch: any key wakes it and is otherwise ignored.
            self._set_standby(not self.standby)
            self._publish(force=True)
            return
        if act.startswith("channel_"):
            self.guide_open = False
            self.mpv.overlay_remove(OVERLAY_GUIDE)
            self.tune(int(act.split("_")[1]))
            return
        if self.guide_open:
            self._guide_key(act)
            return
        if act == "guide":
            self.open_guide()
        elif act == "info":
            self.show_badge()
        elif act in ("ch_up", "ch_down") or (act in ("up", "down") and self.settings.get("nav_keys_change_channel", True)):
            step = 1 if act in ("ch_up", "up") else -1
            self._step_channel(step)
        elif act in ("vol_up", "vol_down") or (act in ("right", "left") and self.settings.get("nav_keys_change_volume", True)):
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
        elif act == "ok":
            self.show_badge()
        elif act == "back":
            if self.behind_live:
                self.behind_live = False
                self.paused = False
                self.mpv.set("pause", False)
                self.playing_slot_id = None
                self.play_live()
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
                self.playing_slot_id = None
                self._forget_slot()
                self.play_live()
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
        self.renderer.configure(float(self.settings.get("osd_safe_margin", 0.07)), float(self.settings.get("osd_scale", 1.0)))
        dims = self.mpv.get("osd-dimensions") or {}
        w, h = dims.get("w"), dims.get("h")
        if not (w and h):
            w, h = self.mpv.get("osd-width"), self.mpv.get("osd-height")
        if w and h:
            self.renderer.resize(int(w), int(h))

    def _overlay(self, oid: int, rendered: tuple[str, int, int, int, int], ttl: float | None) -> None:
        path, w, h, x, y = rendered
        try:
            self.mpv.overlay_add(oid, x, y, path, w, h)
        except MpvError as exc:
            log.debug("overlay failed: %s", exc)
            return
        if ttl:
            self.osd_expiry[oid] = time.time() + ttl
        else:
            self.osd_expiry.pop(oid, None)

    def show_badge(self) -> None:
        if not self.channel:
            return
        self._sync_osd_size()
        now = self.clock()
        cur = self.current_programme(self.channel["id"], now)
        nxt = self.next_programmes(self.channel["id"], cur["end_ts"] if cur else now, 1)
        position = None
        if cur and cur["end_ts"] > cur["start_ts"]:
            position = (now - cur["start_ts"]) / (cur["end_ts"] - cur["start_ts"])
        self._overlay(OVERLAY_BADGE, self.renderer.badge(self.channel, cur, nxt[0] if nxt else None, position, self.behind_live),
                      ttl=float(self.settings.get("badge_seconds", 5)))

    def open_guide(self) -> None:
        self.guide_open = True
        self.guide_highlight = next((i for i, c in enumerate(self.channels) if self.channel and c["id"] == self.channel["id"]), 0)
        self.guide_cursor = 0
        self.mpv.overlay_remove(OVERLAY_BADGE)
        self.osd_expiry.pop(OVERLAY_BADGE, None)
        self._render_guide(reload=True)

    def close_guide(self) -> None:
        self.guide_open = False
        self.mpv.overlay_remove(OVERLAY_GUIDE)

    def _load_guide_rows(self) -> None:
        now = self.clock()
        rows: dict[int, list[dict[str, Any]]] = {}
        for ch in self.channels:
            cur = self.current_programme(ch["id"], now)
            items = [cur] if cur else []
            items += self.next_programmes(ch["id"], cur["end_ts"] if cur else now, 40)
            rows[ch["id"]] = items
        self.guide_rows = rows
        self.guide_loaded_at = time.time()

    def _render_guide(self, reload: bool = False) -> None:
        if reload or time.time() - self.guide_loaded_at > 30:
            self._load_guide_rows()
        self._sync_osd_size()
        hl = self.channels[self.guide_highlight]["id"] if self.channels else 0
        clock = datetime.fromtimestamp(self.clock()).strftime("%a %d %b  %H:%M")
        self._overlay(OVERLAY_GUIDE, self.renderer.guide(self.channels, self.guide_rows, hl, self.guide_cursor, self.clock(), clock), ttl=None)

    def _guide_key(self, act: str) -> None:
        if act in ("guide", "back"):
            self.close_guide()
        elif act == "up":
            self.guide_highlight = (self.guide_highlight - 1) % len(self.channels)
            self.guide_cursor = 0
            self._render_guide()
        elif act == "down":
            self.guide_highlight = (self.guide_highlight + 1) % len(self.channels)
            self.guide_cursor = 0
            self._render_guide()
        elif act == "left":
            self.guide_cursor = max(0, self.guide_cursor - 1)
            self._render_guide()
        elif act == "right":
            hl = self.channels[self.guide_highlight]["id"]
            self.guide_cursor = min(len(self.guide_rows.get(hl, [])) - 1, self.guide_cursor + 1)
            self._render_guide()
        elif act == "ok":
            self.close_guide()
            self.tune(self.channels[self.guide_highlight]["number"])
        elif act in ("vol_up", "vol_down", "mute", "pause"):
            self.close_guide()
            self.action(act)

    # --- control socket / state ---------------------------------------------------------------------------

    def _handle_control(self, req: dict[str, Any]) -> dict[str, Any]:
        parsed = validate_control(req)
        if isinstance(parsed, str):
            return {"ok": False, "error": parsed}
        cmd, arg = parsed
        if cmd == "state":
            return {"ok": True, **self.state()}
        if cmd == "key":
            self.last_key = {"key": f"WEB_{arg.upper()}", "action": arg, "ts": now_ts(), "source": "web"}
            self.actions.put(("key", ("web", arg)))
        elif cmd == "channel":
            self.actions.put(("tune", arg))
        elif cmd == "volume":
            self.actions.put(("volume", arg))
        else:
            self.actions.put((cmd, None))
        return {"ok": True}

    def _schedule_changed(self) -> None:
        self.actions.put(("schedule-changed", None))

    def state(self) -> dict[str, Any]:
        now = self.clock()
        slot = self.slot
        pos = self.mpv.get("time-pos") if self.mpv.alive and self.playing_path not in (None, TESTCARD) else None
        self.hwdec_current = self.mpv.get("hwdec-current") if self.mpv.alive else None
        return {
            "online": True, "ts": now, "clock_offset": self.offset,
            "channel": {"id": self.channel["id"], "number": self.channel["number"], "name": self.channel["name"],
                        "colour": self.channel.get("colour")} if self.channel else None,
            "slot": {k: slot.get(k) for k in ("id", "kind", "title", "subtitle", "start_ts", "end_ts", "media_id")} if slot else None,
            "position": pos, "paused": self.paused, "behind_live": self.behind_live, "standby": self.standby,
            "volume": self.volume, "muted": self.muted, "guide_open": self.guide_open,
            "playing": self.playing_path not in (None, TESTCARD), "testcard": self.playing_path == TESTCARD,
            "hwdec": self.hwdec_current, "on_pi": self.on_pi, "last_key": self.last_key, "error": self.last_error,
            "cache": self.cache.usage(), "maintenance": self.maintenance.status,
            "stream": self.stream_info, "file": self.playing_path if self.playing_path != TESTCARD else None,
            "input_devices": self.evdev.device_names,
        }

    def _publish(self, force: bool = False) -> None:
        try:
            st = self.state()
        except Exception:  # noqa: BLE001 - a broken state snapshot must not stop playback
            log.exception("state snapshot failed")
            return
        key = json.dumps({k: v for k, v in st.items() if k not in ("ts", "position", "cache")}, sort_keys=True)
        now = time.time()
        if force or key != self._state_cache or st.get("playing") or now - self._last_heartbeat >= HEARTBEAT_SECONDS:
            self._state_cache = key
            self._last_heartbeat = now
            self.control.broadcast(st)


def run_player(cfg: Config, channel: int | None = None, keyboard: bool = False, now_override: str | None = None) -> int:
    return Player(cfg, channel=channel, keyboard=keyboard, now_override=now_override).run()
