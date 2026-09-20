"""Run mpv and talk to it over its JSON IPC socket."""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("pitv.mpv")

MAX_LINE = 4 << 20   # a property reply is small; only a broken peer sends more without a newline


class MpvError(RuntimeError):
    pass


def _option_value(value: Any) -> str:
    """mpv's per-file options are strings; its flags are spelled yes/no."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


class Mpv:
    def __init__(self, binary: str, socket_path: Path, args: list[str],
                 on_event: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.binary = binary
        self.socket_path = socket_path
        self.args = args
        self.on_event = on_event
        self.proc: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._req_id = 0
        # request_id -> (event, reply holder); the reader thread fills the holder and sets the event.
        self._pending: dict[int, tuple[threading.Event, list[dict[str, Any]]]] = {}
        self.alive = False
        # Each connection's reader owns one generation; stop() moves on to the next, so the
        # reader of a connection closed on purpose (a restart) exits without reporting a loss
        # or marking the next connection dead.
        self._generation = 0

    # --- lifecycle ---------------------------------------------------------------------

    def start(self, timeout: float = 15) -> None:
        self.socket_path.unlink(missing_ok=True)
        cmd = [self.binary, f"--input-ipc-server={self.socket_path}", *self.args]
        log.info("starting mpv: %s", " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        while self.sock is None and time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise MpvError(f"mpv exited immediately with code {self.proc.returncode}")
            if self.socket_path.exists():
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    s.connect(str(self.socket_path))
                    self.sock = s
                    break
                except OSError:
                    s.close()
            time.sleep(0.1)
        if self.sock is None:
            self._reap(kill=True)  # do not leave a headless mpv behind
            raise MpvError("mpv IPC socket did not appear")
        self.alive = True
        threading.Thread(target=self._read_loop, args=(self.sock, self._generation), name="mpv-reader", daemon=True).start()

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
        try:
            if self.sock:
                self.command("quit", timeout=1.0)
        except MpvError:
            pass  # already gone, or it quit without answering; _reap tidies up
        self.alive = False
        self._reap(kill=False)
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _reap(self, kill: bool) -> None:
        """Wait for mpv to exit (killing it if asked or if it will not go). An mpv stuck in
        uninterruptible I/O on a dead NAS mount cannot be reaped; systemd's stop timeout
        deals with that, so it is logged rather than raised."""
        if self.proc is None:
            return
        try:
            if not kill:
                try:
                    self.proc.wait(timeout=3)
                    return
                except subprocess.TimeoutExpired:
                    pass
            self.proc.kill()
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.error("mpv (pid %s) did not exit after SIGKILL", self.proc.pid)

    def running(self) -> bool:
        return self.alive and self.proc is not None and self.proc.poll() is None

    # --- protocol ------------------------------------------------------------------------

    def _read_loop(self, sock: socket.socket, generation: int) -> None:
        buf = b""
        while self.alive and sock is not None:
            try:
                chunk = sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            *lines, buf = (buf + chunk).split(b"\n")
            if len(buf) > MAX_LINE:
                log.error("mpv sent %d bytes without a newline; dropping them", len(buf))
                buf = b""
            for line in lines:
                self._dispatch(line)
        # Under the lock so no request can register after the wake-up: `_request` checks
        # `alive` and registers under the same lock.
        with self._lock:
            if generation != self._generation:
                return   # stop() closed this connection on purpose
            self.alive = False
            waiting = list(self._pending.values())
        for ev, _ in waiting:
            ev.set()   # no reply is coming; the caller raises instead of sitting out its timeout
        if self.on_event:
            self.on_event({"event": "pitv-ipc-closed"})

    def _dispatch(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        if "request_id" in msg:
            pending = self._pending.pop(msg["request_id"], None)
            if pending:
                pending[1].append(msg)
                pending[0].set()
        elif "event" in msg and self.on_event:
            try:
                self.on_event(msg)
            except Exception:  # a handler bug must not end the reader thread
                log.exception("mpv event handler failed")

    def _request(self, command: list[Any] | dict[str, Any], timeout: float) -> Any:
        """Send one command (positional list or named-argument map) and wait for its reply."""
        name = command["name"] if isinstance(command, dict) else command[0]
        reply: list[dict[str, Any]] = []
        ev = threading.Event()
        with self._lock:
            sock = self.sock
            if sock is None or not self.alive:
                raise MpvError("mpv not connected")
            self._req_id += 1
            rid = self._req_id
            self._pending[rid] = (ev, reply)
            try:
                sock.sendall((json.dumps({"command": command, "request_id": rid}) + "\n").encode())
            except OSError as exc:
                self._pending.pop(rid, None)
                raise MpvError(f"send failed: {exc}") from exc
        if not ev.wait(timeout):
            self._pending.pop(rid, None)
            raise MpvError(f"mpv did not answer {name!r}")
        if not reply:
            raise MpvError(f"mpv connection closed before answering {name!r}")
        msg = reply[0]
        if msg.get("error") not in (None, "success"):
            raise MpvError(f"{name}: {msg.get('error')}")
        return msg.get("data")

    def command(self, *args: Any, timeout: float = 5.0) -> Any:
        return self._request(list(args), timeout)

    def get(self, prop: str, default: Any = None, timeout: float = 5.0) -> Any:
        try:
            return self._request(["get_property", prop], timeout)
        except MpvError:
            return default

    def set(self, prop: str, value: Any) -> None:
        self.command("set_property", prop, value)

    def loadfile(self, path: str, start: float = 0.0, options: dict[str, Any] | None = None) -> int | None:
        """Replace the current file; returns mpv's playlist entry id for it (None on builds
        that do not report one), which its end-file event will carry. `options` are per-file:
        mpv drops them when the file ends, so decode settings for one programme do not leak
        onto the test card.

        Named arguments, because mpv 0.38 inserted an `index` argument before `options` and a
        positional call cannot be right for both older and newer builds."""
        opts = {"start": f"{max(0.0, start):.3f}"}
        opts.update({k: _option_value(v) for k, v in (options or {}).items()})
        data = self._request({"name": "loadfile", "url": path, "flags": "replace", "options": opts}, 5.0)
        return data.get("playlist_entry_id") if isinstance(data, dict) else None

    def keybind(self, key: str, message: str) -> None:
        try:
            self.command("keybind", key, f"script-message {message}")
        except MpvError as exc:
            log.debug("keybind %s: %s", key, exc)  # only matters for the desktop window

    # --- overlays ---------------------------------------------------------------------------

    def overlay_add(self, overlay_id: int, x: int, y: int, path: str, w: int, h: int, offset: int = 0) -> None:
        """`offset` is where in the file the frame starts, so one file can hold several."""
        self.command("overlay-add", overlay_id, x, y, path, offset, "bgra", w, h, w * 4)

    def overlay_remove(self, overlay_id: int) -> None:
        try:
            self.command("overlay-remove", overlay_id)
        except MpvError:
            pass  # removing an overlay that is not shown is an mpv error and a no-op for us


def default_args(windowed: bool, geometry: str = "768x576", title: str = "CRT (PAL)") -> list[str]:
    common = [
        "--idle=yes", "--force-window=yes", "--keep-open=no", "--no-osc", "--osd-level=0",
        "--input-default-bindings=no", "--input-vo-keyboard=yes", "--cursor-autohide=always",
        "--really-quiet", "--image-display-duration=inf", "--no-terminal",
        "--cache=yes", "--demuxer-max-bytes=64MiB", "--demuxer-readahead-secs=20",
        "--audio-pitch-correction=no", "--volume-max=100", "--sub=no", "--no-config",
    ]
    if windowed:
        # Desktop preview: a window the shape and size of the screen profile's frame (768x576
        # square pixels shows exactly what a 720x576 anamorphic frame looks like on a 4:3 set),
        # same scaler and deinterlacer as the Pi, so transcodes from pitv_content can be judged.
        return common + [f"--geometry={geometry}", f"--title=PiTV preview: {title}", "--hwdec=auto-safe",
                         "--keepaspect-window=yes", "--scale=bilinear", "--cscale=bilinear", "--dscale=bilinear"]
    return common + [
        "--vo=gpu", "--gpu-context=drm", "--fullscreen", "--ao=alsa",
        "--hwdec=drm-prime,v4l2m2m-copy", "--gpu-api=opengl", "--profile=fast",
        "--video-sync=audio", "--scale=bilinear", "--cscale=bilinear", "--dscale=bilinear",
    ]
