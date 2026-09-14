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
from ..config import Config
from ..db import all_settings, now_ts, row_to_dict
from ..acquire.worker import AcquisitionWorker
from .cache import MediaCache, PrefetchWorker
from .control_socket import ControlServer
from .hwdec import decode_options, is_raspberry_pi
from .input import EvdevInput, TerminalInput
from .maintenance import Maintenance
from .mpv_ipc import Mpv, MpvError, default_args
from .osd import (OVERLAY_BADGE, OVERLAY_GUIDE, OVERLAY_MESSAGE, OVERLAY_STATIC, OVERLAY_VOLUME, Renderer,
                  make_testcard)

log = logging.getLogger("pitv.player")

SLOT_SQL = ("SELECT s.*, m.path AS media_path, m.transcoded_path, m.vcodec, m.interlaced, m.plot,"
            " m.year AS media_year, m.certificate, m.missing AS media_missing, m.show_id"
            " FROM schedule s LEFT JOIN media m ON m.id = s.media_id")

WINDOW_KEYS = {"UP": "up", "DOWN": "down", "LEFT": "left", "RIGHT": "right", "ENTER": "ok", "ESC": "back",
               "g": "guide", "i": "info", "SPACE": "pause", "m": "mute", "+": "vol_up", "=": "vol_up",
               "-": "vol_down", "]": "ch_up", "[": "ch_down", "r": "restart", "q": "quit"}
for _n in range(1, 10):
    WINDOW_KEYS[str(_n)] = f"channel_{_n}"


class Player:
    def __init__(self, cfg: Config, channel: int | None = None, keyboard: bool = False, now_override: str | None = None) -> None:
        self.cfg = cfg
        cfg.ensure_dirs()
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
        self._state_cache: str = ""
        self.keyboard = keyboard
        self.initial_channel = channel or 1
        self.explicit_channel = channel is not None
        self.state_file = cfg.data_dir / "player_state.json"

        cache_dir = self.settings.get("cache_dir") or ""
        self.cache = MediaCache(Path(cache_dir) if cache_dir else None,
                                int(float(self.settings.get("cache_max_gb", 200)) * 1024 ** 3),
                                float(self.settings.get("cache_copy_mbps", 0) or 0))
        self.renderer = Renderer(cfg.run_dir)
        self.mpv = Mpv(cfg.mpv_binary, cfg.mpv_socket, self._mpv_args(), on_event=self._on_mpv_event)
        self.control = ControlServer(cfg.player_socket, self._handle_control, self.state)
        self.evdev = EvdevInput(self._on_key, self.settings.get("keymap") or {})
        self.tty = TerminalInput(self._on_key) if keyboard else None
        self.prefetch = PrefetchWorker(self.cache, cfg.db_path, float(self.settings.get("prefetch_hours", 4)),
                                       lambda: self.channel["id"] if self.channel else None, self.clock)
        self.maintenance = Maintenance(cfg.db_path, cfg.ffprobe_binary, self.clock, self._schedule_changed)
        self.acquire = AcquisitionWorker(cfg.db_path, self.clock, self.on_pi, cfg.ffprobe_binary, self._schedule_changed)

    # --- helpers ---------------------------------------------------------------------------

    def clock(self) -> int:
        return int(time.time()) + self.offset

    def _mpv_args(self) -> list[str]:
        args = default_args(self.cfg.windowed or not self.on_pi, self.cfg.run_dir)
        dev = self.settings.get("audio_device") or "auto"
        if dev != "auto":
            args.append(f"--audio-device={dev}")
        return args + self.cfg.mpv_extra_args

    def _load_channels(self) -> None:
        self.channels = [row_to_dict(r) for r in self.conn.execute("SELECT * FROM channels WHERE enabled = 1 ORDER BY number")]

    def _testcard(self) -> Path:
        p = self.cfg.assets_dir / "testcard.png"
        if not p.exists():
            p = self.cfg.data_dir / "testcard.png"
            if not p.exists():
                make_testcard(p)
        return p

    def slot_at(self, channel_id: int, ts: int) -> dict[str, Any] | None:
        row = self.conn.execute(SLOT_SQL + " WHERE s.channel_id = ? AND s.start_ts <= ? AND s.end_ts > ?"
                                " ORDER BY s.start_ts DESC LIMIT 1", (channel_id, ts, ts)).fetchone()
        return dict(row) if row else None

    def next_programmes(self, channel_id: int, after: int, n: int = 12) -> list[dict[str, Any]]:
        rows = self.conn.execute(SLOT_SQL + " WHERE s.channel_id = ? AND s.start_ts > ? AND s.kind = 'programme'"
                                 " ORDER BY s.start_ts LIMIT ?", (channel_id, after, n)).fetchall()
        return [dict(r) for r in rows]

    def current_programme(self, channel_id: int, ts: int) -> dict[str, Any] | None:
        """The programme 'on' now: during an ad break, the one that follows."""
        slot = self.slot_at(channel_id, ts)
        if slot and slot["kind"] == "programme":
            return slot
        nxt = self.next_programmes(channel_id, ts, 1)
        return nxt[0] if nxt else None

    # --- lifecycle -------------------------------------------------------------------------

    def run(self) -> int:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
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
            self.mpv.set("volume", self.volume)
            self.mpv.set("mute", self.muted)
        except MpvError:
            pass
        self.control.start()
        self.evdev.start()
        if self.tty:
            self.tty.start()
        self.prefetch.start()
        self.maintenance.start()
        self.acquire.start()
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
        for part in (self.prefetch, self.maintenance, self.acquire, self.evdev, self.control):
            try:
                part.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.tty:
            self.tty.stop()
        self.mpv.stop()

    def _restore_state(self) -> None:
        try:
            data = json.loads(self.state_file.read_text())
            self.volume = int(data.get("volume", self.volume))
            self.muted = bool(data.get("muted", False))
            if not self.explicit_channel and data.get("channel"):
                self.initial_channel = int(data["channel"])
        except (OSError, ValueError):
            pass

    def _save_state(self) -> None:
        try:
            self.state_file.write_text(json.dumps({"volume": self.volume, "muted": self.muted,
                                                   "channel": self.channel["number"] if self.channel else None}))
        except OSError:
            pass

    # --- main loop ----------------------------------------------------------------------------

    def _main_loop(self) -> None:
        last_settings = time.time()
        last_publish = 0.0
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
            if now - last_settings > 60:
                last_settings = now
                self._reload_settings()
            if now - last_publish >= 1.0:
                last_publish = now
                self._publish()

    def _reload_settings(self) -> None:
        try:
            self.settings = all_settings(self.conn)
            self.evdev.set_keymap(self.settings.get("keymap") or {})
            self._load_channels()
        except sqlite3.Error:
            pass

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
        if self.channel is None:
            return
        if not (self.paused or self.behind_live):
            slot = self.slot_at(self.channel["id"], now)
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
                        except MpvError:
                            pass
            elif self.playing_path is None and time.time() >= self.retry_at:
                self.play_live()
        if self.guide_open and time.time() - self.guide_loaded_at > 30:
            self._render_guide()

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
        try:
            self.mpv.set("pause", False)
        except MpvError:
            pass
        if switching and self.settings.get("channel_switch_static", True):
            self._sync_osd_size()
            self._overlay(OVERLAY_STATIC, self.renderer.static(), ttl=0.35)
        self.play_live()
        self._save_state()
        self.prefetch.poke()
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
            log.warning(self.last_error)
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
        log.info("ch%s %s %s +%.0fs (%s)", self.channel["number"], slot["kind"], slot["title"], offset,
                 "cache" if self.cache.cached_path(media["id"], media["path"] or "") else "source")

    def _show_testcard(self, text: str, sub: str) -> None:
        try:
            if self.playing_path != "testcard":
                self.mpv.loadfile(str(self._testcard()))
                self.playing_path = "testcard"
            self._overlay(OVERLAY_MESSAGE, self.renderer.message(text, sub), ttl=None)
        except MpvError:
            pass

    def _on_mpv_event(self, ev: dict[str, Any]) -> None:
        name = ev.get("event")
        if name == "end-file":
            reason = ev.get("reason")
            if reason == "eof" and self.playing_path not in (None, "testcard"):
                self.actions.put(("eof", None))
            elif reason == "error":
                self.actions.put(("file-error", ev.get("file_error")))
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
            self.prefetch.poke()

    def action(self, act: str) -> None:
        if act == "quit":
            self.stopping = True
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
            self.mpv.set("pause", self.paused)
            if self.paused:
                self.behind_live = True
            self.show_badge()
        elif act == "restart":
            if self.slot and self.playing_path not in (None, "testcard"):
                self.behind_live = True
                try:
                    self.mpv.command("seek", float(self.slot.get("offset") or 0), "absolute")
                except MpvError:
                    pass
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
        nxt = self.next_programmes(self.channel["id"], cur["start_ts"] if cur else now, 1)
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
            items += self.next_programmes(ch["id"], cur["start_ts"] if cur else now, 40)
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
        cmd = req.get("cmd")
        if cmd == "state":
            return {"ok": True, **self.state()}
        if cmd == "key":
            key = str(req.get("key", ""))
            self.last_key = {"key": f"WEB_{key.upper()}", "action": key, "ts": now_ts(), "source": "web"}
            self.actions.put(("key", ("web", key)))
            return {"ok": True}
        if cmd == "channel":
            self.actions.put(("tune", int(req.get("number", 1))))
            return {"ok": True}
        if cmd == "volume":
            self.actions.put(("volume", int(req.get("volume", self.volume))))
            return {"ok": True}
        if cmd == "schedule-changed":
            self.actions.put(("schedule-changed", None))
            return {"ok": True}
        if cmd == "acquire-poke":
            self.acquire.poke()
            return {"ok": True}
        if cmd == "quit":
            self.actions.put(("quit", None))
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    def _schedule_changed(self) -> None:
        self.actions.put(("schedule-changed", None))

    def state(self) -> dict[str, Any]:
        now = self.clock()
        slot = self.slot
        pos = None
        try:
            pos = self.mpv.get("time-pos") if self.mpv.alive and self.playing_path not in (None, "testcard") else None
        except Exception:  # noqa: BLE001
            pos = None
        try:
            self.hwdec_current = self.mpv.get("hwdec-current") if self.mpv.alive else None
        except Exception:  # noqa: BLE001
            pass
        return {
            "online": True, "ts": now, "clock_offset": self.offset,
            "channel": {"id": self.channel["id"], "number": self.channel["number"], "name": self.channel["name"],
                        "colour": self.channel.get("colour")} if self.channel else None,
            "slot": {k: slot.get(k) for k in ("id", "kind", "title", "subtitle", "start_ts", "end_ts", "media_id")} if slot else None,
            "position": pos, "paused": self.paused, "behind_live": self.behind_live,
            "volume": self.volume, "muted": self.muted, "guide_open": self.guide_open,
            "playing": self.playing_path not in (None, "testcard"), "testcard": self.playing_path == "testcard",
            "hwdec": self.hwdec_current, "on_pi": self.on_pi, "last_key": self.last_key, "error": self.last_error,
            "cache": self.cache.usage(), "maintenance": self.maintenance.status, "acquire": self.acquire.status(),
            "input_devices": [getattr(d, "name", "?") for d in self.evdev.devices.values()],
        }

    def _publish(self, force: bool = False) -> None:
        try:
            st = self.state()
        except Exception:  # noqa: BLE001
            return
        key = json.dumps({k: v for k, v in st.items() if k not in ("ts", "position", "cache")}, sort_keys=True)
        if force or key != self._state_cache or st.get("playing"):
            self._state_cache = key
            self.control.broadcast(st)


def run_player(cfg: Config, channel: int | None = None, keyboard: bool = False, now_override: str | None = None) -> int:
    return Player(cfg, channel=channel, keyboard=keyboard, now_override=now_override).run()
