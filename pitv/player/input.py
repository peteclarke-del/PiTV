"""Remote control and keyboard input.

The OSMC RF remote (and any USB/Bluetooth remote or keyboard) appears to Linux as an
ordinary input device, so everything comes in through evdev. Key codes are mapped to PiTV
actions through the `keymap` setting; unknown keys are reported so the admin UI can offer
a "press a button to assign it" flow. IR remotes set up with the kernel's gpio-ir driver
and ir-keytable take the same path.
"""

from __future__ import annotations

import logging
import os
import select
import sys
import termios
import threading
import time
import tty
from collections.abc import Callable
from typing import Any

log = logging.getLogger("pitv.input")

ACTIONS = ["up", "down", "left", "right", "ok", "back", "guide", "info", "pause", "mute",
           "vol_up", "vol_down", "ch_up", "ch_down", "restart", "power"] + [f"channel_{n}" for n in range(1, 10)]
REPEATABLE = frozenset({"vol_up", "vol_down", "up", "down", "left", "right"})  # act on a held key

# Defaults cover the OSMC RF remote (Home, Info, arrows, OK, Back, Menu, Play/Pause, Stop,
# Vol +/-), CEC remotes and a plain keyboard. Editable in the admin UI. "power" is standby:
# picture off and sound muted until any key is pressed (the channel keeps "broadcasting").
DEFAULT_KEYMAP: dict[str, list[str]] = {
    "up": ["KEY_UP"], "down": ["KEY_DOWN"], "left": ["KEY_LEFT"], "right": ["KEY_RIGHT"],
    "ok": ["KEY_ENTER", "KEY_KPENTER", "KEY_OK", "KEY_SELECT"],
    "back": ["KEY_BACK", "KEY_ESC", "KEY_BACKSPACE", "KEY_EXIT"],
    "guide": ["KEY_CONTEXT_MENU", "KEY_MENU", "KEY_EPG", "KEY_HOMEPAGE", "KEY_HOME", "KEY_COMPOSE", "KEY_G"],
    "info": ["KEY_INFO", "KEY_I"],
    "pause": ["KEY_PLAYPAUSE", "KEY_PLAY", "KEY_PAUSE", "KEY_PLAYCD", "KEY_PAUSECD", "KEY_SPACE"],
    "mute": ["KEY_MUTE", "KEY_STOPCD", "KEY_STOP", "KEY_M"],
    "vol_up": ["KEY_VOLUMEUP", "KEY_EQUAL", "KEY_KPPLUS"],
    "vol_down": ["KEY_VOLUMEDOWN", "KEY_MINUS", "KEY_KPMINUS"],
    "ch_up": ["KEY_CHANNELUP", "KEY_PAGEUP", "KEY_NEXTSONG"],
    "ch_down": ["KEY_CHANNELDOWN", "KEY_PAGEDOWN", "KEY_PREVIOUSSONG"],
    "restart": ["KEY_REWIND", "KEY_R"],
    "power": ["KEY_POWER", "KEY_SLEEP"],
    **{f"channel_{n}": [f"KEY_{n}", f"KEY_KP{n}", f"KEY_NUMERIC_{n}"] for n in range(1, 10)},
}

# Desktop development keys, shared by the terminal and the mpv window; each adds its own
# spelling of the arrows, OK, Back and space. "quit" is local only and never a remote action.
_DESKTOP_KEYS = {
    "g": "guide", "i": "info", "m": "mute", "+": "vol_up", "=": "vol_up", "-": "vol_down",
    "]": "ch_up", "[": "ch_down", "r": "restart", "p": "power", "q": "quit",
    **{str(n): f"channel_{n}" for n in range(1, 10)},
}
TERMINAL_KEYS = {**_DESKTOP_KEYS, "\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left",
                 "\r": "ok", "\n": "ok", "\x1b": "back", " ": "pause"}
WINDOW_KEYS = {**_DESKTOP_KEYS, "UP": "up", "DOWN": "down", "LEFT": "left", "RIGHT": "right",
               "ENTER": "ok", "ESC": "back", "SPACE": "pause"}

RESCAN_SECONDS = 3.0   # how often to look for a newly plugged remote


def build_lookup(keymap: dict[str, list[str]] | None) -> dict[str, str]:
    """Invert {action: [keys]} into {key: action}. An action named in `keymap` replaces its
    default keys, and a key the user assigned wins over a default action holding the same key."""
    custom = {action: [str(k).upper() for k in keys] for action, keys in (keymap or {}).items()
              if action in ACTIONS and isinstance(keys, list)}
    lookup: dict[str, str] = {}
    for action, keys in custom.items():
        for k in keys:
            lookup.setdefault(k, action)
    for action, keys in DEFAULT_KEYMAP.items():
        if action not in custom:
            for k in keys:
                lookup.setdefault(k, action)
    return lookup


def _key_names(ecodes: Any, code: int) -> list[str]:
    """evdev's names for a key code: one string, or a list or tuple for aliased codes; an
    unknown code gets a hex name so it can still be reported and assigned."""
    names = ecodes.keys.get(code)
    if names is None:
        return [f"0x{code:02X}"]
    return [names] if isinstance(names, str) else list(names)


class EvdevInput:
    """Reads every keyboard-like input device; hot-plugs new ones (the RF dongle may appear late)."""

    def __init__(self, on_key: Callable[[str, str | None], None], keymap: dict[str, list[str]] | None,
                 repeat_delay: float = 0.35) -> None:
        self.on_key = on_key
        self.lookup = build_lookup(keymap)
        self.repeat_delay = repeat_delay
        self._stop = threading.Event()
        self.devices: dict[str, Any] = {}
        # Devices without remote-control keys, by node, with the node's ctime: they are not
        # reopened every rescan, but a node recreated for a new device is looked at again.
        self._rejected: dict[str, int] = {}
        # Snapshot for other threads (the player state): rebuilt whenever `devices` changes so
        # readers never iterate the dict the input thread is mutating.
        self.device_names: list[str] = []
        self.available = False

    def set_keymap(self, keymap: dict[str, list[str]] | None) -> None:
        self.lookup = build_lookup(keymap)

    def start(self) -> None:
        try:
            import evdev  # noqa: F401
        except ImportError:
            log.warning("python-evdev not installed; remote control input disabled")
            return
        self.available = True
        threading.Thread(target=self._loop, name="pitv-evdev", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        for dev in list(self.devices.values()):
            try:
                dev.close()
            except OSError:
                pass

    def _drop(self, path: str) -> None:
        dev = self.devices.pop(path, None)
        if dev is not None:
            try:
                dev.close()
            except OSError:
                pass
        self.device_names = [d.name for d in self.devices.values()]

    def _rescan(self) -> None:
        import evdev
        from evdev import ecodes
        wanted = (ecodes.KEY_ENTER, ecodes.KEY_UP, ecodes.KEY_VOLUMEUP, ecodes.KEY_PLAYPAUSE,
                  ecodes.KEY_OK, ecodes.KEY_1)
        paths = evdev.list_devices()
        self._rejected = {p: c for p, c in self._rejected.items() if p in paths}
        for path in paths:
            if path in self.devices:
                continue
            try:
                ctime = os.stat(path).st_ctime_ns
                if self._rejected.get(path) == ctime:
                    continue
                dev = evdev.InputDevice(path)
            except OSError:
                continue   # not accessible yet: udev may still be applying permissions
            try:
                keys = dev.capabilities().get(ecodes.EV_KEY, [])
            except OSError:
                keys = []
            if not any(k in keys for k in wanted):
                dev.close()
                self._rejected[path] = ctime
                continue
            self.devices[path] = dev
            self.device_names = [d.name for d in self.devices.values()]
            log.info("input device: %s (%s)", dev.name, path)

    def _loop(self) -> None:
        from evdev import ecodes
        last_scan = float("-inf")
        held: dict[tuple[str, int], float] = {}   # (device, key code) -> when it went down
        while not self._stop.is_set():
            try:
                if time.monotonic() - last_scan > RESCAN_SECONDS:
                    self._rescan()
                    last_scan = time.monotonic()
                if not self.devices:
                    self._stop.wait(1.0)
                    continue
                self._read_ready(ecodes, held)
            except Exception:  # remote input must survive a driver or library surprise
                log.exception("input loop error")
                self._stop.wait(1.0)

    def _read_ready(self, ecodes: Any, held: dict[tuple[str, int], float]) -> None:
        fds = {dev.fd: (path, dev) for path, dev in self.devices.items()}
        try:
            ready, _, _ = select.select(list(fds), [], [], 1.0)
        except (OSError, ValueError):
            self._stop.wait(0.5)   # a device closed under select; the next rescan sorts it out
            return
        for fd in ready:
            path, dev = fds[fd]
            try:
                events = list(dev.read())
            except BlockingIOError:
                continue   # woken with nothing to read
            except OSError:
                if self._stop.is_set():
                    return   # stop() closed the devices under us
                log.info("input device gone: %s", path)
                self._drop(path)
                for k in [k for k in held if k[0] == path]:
                    del held[k]
                continue
            for event in events:
                if event.type != ecodes.EV_KEY:
                    continue
                if event.value == 0:   # release
                    held.pop((path, event.code), None)
                    continue
                names = _key_names(ecodes, event.code)
                action = self._action_for(names)
                now = time.monotonic()
                if event.value == 1:   # press
                    held[(path, event.code)] = now
                    self.on_key(names[0], action)
                elif (event.value == 2 and action in REPEATABLE
                      and now - held.get((path, event.code), 0.0) >= self.repeat_delay):
                    self.on_key(names[0], action)

    def _action_for(self, names: list[str]) -> str | None:
        for n in names:
            action = self.lookup.get(n.upper())
            if action:
                return action
        return None


class TerminalInput:
    """Desktop development: read single keys from the terminal running the player."""

    def __init__(self, on_key: Callable[[str, str | None], None]) -> None:
        self.on_key = on_key
        self._stop = threading.Event()
        self._old: list | None = None

    def start(self) -> None:
        if not sys.stdin.isatty():
            return
        self._old = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())
        threading.Thread(target=self._loop, name="pitv-tty", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._old is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old)

    def _loop(self) -> None:
        # Raw reads from the descriptor: sys.stdin's buffer would swallow the rest of an
        # escape sequence where select() cannot see it.
        fd = sys.stdin.fileno()
        while not self._stop.is_set():
            r, _, _ = select.select([fd], [], [], 0.5)
            if not r:
                continue
            ch = os.read(fd, 1).decode(errors="replace")
            if ch == "\x1b" and select.select([fd], [], [], 0.05)[0]:
                ch += os.read(fd, 2).decode(errors="replace")
            self.on_key(repr(ch), TERMINAL_KEYS.get(ch))
