"""Remote control and keyboard input.

The OSMC RF remote (and any USB/Bluetooth remote or keyboard) appears to Linux as an
ordinary input device, so everything comes in through evdev. Key codes are mapped to PiTV
actions through the `keymap` setting; unknown keys are reported so the admin UI can offer
a "press a button to assign it" flow. IR remotes set up with the kernel's gpio-ir driver
and ir-keytable take the same path.
"""

from __future__ import annotations

import logging
import select
import sys
import termios
import threading
import time
import tty
from typing import Any, Callable

log = logging.getLogger("pitv.input")

ACTIONS = ["up", "down", "left", "right", "ok", "back", "guide", "info", "pause", "mute",
           "vol_up", "vol_down", "ch_up", "ch_down", "restart", "power"] + [f"channel_{n}" for n in range(1, 10)]

# Defaults cover the OSMC RF remote (Home, Info, arrows, OK, Back, Menu, Play/Pause, Stop,
# Vol +/-), CEC remotes and a plain keyboard. Editable in the admin UI.
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
}
for _n in range(1, 10):
    DEFAULT_KEYMAP[f"channel_{_n}"] = [f"KEY_{_n}", f"KEY_KP{_n}", f"KEY_NUMERIC_{_n}"]

_TERMINAL_KEYS = {
    "\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left", "\r": "ok", "\n": "ok",
    "\x1b": "back", "g": "guide", "i": "info", " ": "pause", "m": "mute", "+": "vol_up", "=": "vol_up",
    "-": "vol_down", "]": "ch_up", "[": "ch_down", "r": "restart", "q": "quit",
}
for _n in range(1, 10):
    _TERMINAL_KEYS[str(_n)] = f"channel_{_n}"


def build_lookup(keymap: dict[str, list[str]] | None) -> dict[str, str]:
    """Invert {action: [keys]} into {key: action}."""
    km = dict(DEFAULT_KEYMAP)
    if keymap:
        for action, keys in keymap.items():
            if action in ACTIONS and isinstance(keys, list):
                km[action] = [str(k).upper() for k in keys]
    lookup: dict[str, str] = {}
    for action, keys in km.items():
        for k in keys:
            lookup.setdefault(k.upper(), action)
    return lookup


class EvdevInput:
    """Reads every keyboard-like input device; hot-plugs new ones (the RF dongle may appear late)."""

    def __init__(self, on_key: Callable[[str, str | None], None], keymap: dict[str, list[str]] | None,
                 repeat_delay: float = 0.35) -> None:
        self.on_key = on_key
        self.lookup = build_lookup(keymap)
        self.repeat_delay = repeat_delay
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.devices: dict[str, Any] = {}
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
        self._thread = threading.Thread(target=self._loop, name="pitv-evdev", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _rescan(self) -> None:
        import evdev
        from evdev import ecodes
        for path in evdev.list_devices():
            if path in self.devices:
                continue
            try:
                dev = evdev.InputDevice(path)
                caps = dev.capabilities()
                keys = caps.get(ecodes.EV_KEY, [])
                if not keys or not any(k in keys for k in (ecodes.KEY_ENTER, ecodes.KEY_UP, ecodes.KEY_VOLUMEUP,
                                                           ecodes.KEY_PLAYPAUSE, ecodes.KEY_OK, ecodes.KEY_1)):
                    dev.close()
                    continue
                self.devices[path] = dev
                log.info("input device: %s (%s)", dev.name, path)
            except OSError:
                continue

    def _loop(self) -> None:
        from evdev import categorize, ecodes
        last_scan = 0.0
        held: dict[tuple[str, int], float] = {}
        while not self._stop.is_set():
            if time.time() - last_scan > 3:
                self._rescan()
                last_scan = time.time()
            if not self.devices:
                time.sleep(1)
                continue
            fds = {dev.fd: (path, dev) for path, dev in self.devices.items()}
            try:
                r, _, _ = select.select(list(fds), [], [], 1.0)
            except (OSError, ValueError):
                time.sleep(0.5)
                continue
            for fd in r:
                path, dev = fds[fd]
                try:
                    for event in dev.read():
                        if event.type != ecodes.EV_KEY:
                            continue
                        key = categorize(event)
                        names = key.keycode if isinstance(key.keycode, list) else [key.keycode]
                        if event.value == 1:  # press
                            held[(path, event.code)] = time.time()
                            self._dispatch(names)
                        elif event.value == 2:  # autorepeat
                            t0 = held.get((path, event.code), 0)
                            if time.time() - t0 >= self.repeat_delay:
                                action = self._action_for(names)
                                if action in ("vol_up", "vol_down", "up", "down", "left", "right"):
                                    self._dispatch(names)
                        elif event.value == 0:
                            held.pop((path, event.code), None)
                except OSError:
                    log.info("input device gone: %s", path)
                    self.devices.pop(path, None)

    def _action_for(self, names: list[str]) -> str | None:
        for n in names:
            if n.upper() in self.lookup:
                return self.lookup[n.upper()]
        return None

    def _dispatch(self, names: list[str]) -> None:
        action = self._action_for(names)
        self.on_key(names[0], action)


class TerminalInput:
    """Desktop development: read single keys from the terminal running the player."""

    def __init__(self, on_key: Callable[[str, str | None], None]) -> None:
        self.on_key = on_key
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._old: list | None = None

    def start(self) -> None:
        if not sys.stdin.isatty():
            return
        self._old = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())
        self._thread = threading.Thread(target=self._loop, name="pitv-tty", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._old is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old)

    def _loop(self) -> None:
        while not self._stop.is_set():
            r, _, _ = select.select([sys.stdin], [], [], 0.5)
            if not r:
                continue
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                r, _, _ = select.select([sys.stdin], [], [], 0.05)
                if r:
                    ch += sys.stdin.read(2)
            action = _TERMINAL_KEYS.get(ch)
            self.on_key(repr(ch), action)
