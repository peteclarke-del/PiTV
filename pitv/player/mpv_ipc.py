"""Run mpv and talk to it over its JSON IPC socket."""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("pitv.mpv")


class MpvError(RuntimeError):
    pass


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
        self._pending: dict[int, tuple[threading.Event, list]] = {}
        self._reader: threading.Thread | None = None
        self.alive = False

    # --- lifecycle ---------------------------------------------------------------------

    def start(self, timeout: float = 15) -> None:
        if self.socket_path.exists():
            self.socket_path.unlink()
        cmd = [self.binary, f"--input-ipc-server={self.socket_path}", *self.args]
        log.info("starting mpv: %s", " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise MpvError(f"mpv exited immediately with code {self.proc.returncode}")
            if self.socket_path.exists():
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(str(self.socket_path))
                    self.sock = s
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                time.sleep(0.1)
        if self.sock is None:
            raise MpvError("mpv IPC socket did not appear")
        self.alive = True
        self._reader = threading.Thread(target=self._read_loop, name="mpv-reader", daemon=True)
        self._reader.start()

    def stop(self) -> None:
        self.alive = False
        try:
            if self.sock:
                self.command("quit")
        except Exception:  # noqa: BLE001
            pass
        if self.proc:
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def running(self) -> bool:
        return self.alive and self.proc is not None and self.proc.poll() is None

    # --- protocol ------------------------------------------------------------------------

    def _read_loop(self) -> None:
        buf = b""
        while self.alive and self.sock:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "request_id" in msg:
                    pending = self._pending.pop(msg["request_id"], None)
                    if pending:
                        pending[1].append(msg)
                        pending[0].set()
                elif "event" in msg and self.on_event:
                    try:
                        self.on_event(msg)
                    except Exception:  # noqa: BLE001
                        log.exception("mpv event handler failed")
        self.alive = False
        if self.on_event:
            self.on_event({"event": "pitv-ipc-closed"})

    def command(self, *args: Any, timeout: float = 5.0) -> Any:
        if not self.sock:
            raise MpvError("mpv not connected")
        msgs: list = []
        with self._lock:
            self._req_id += 1
            rid = self._req_id
            ev = threading.Event()
            self._pending[rid] = (ev, msgs)  # the reader thread pops this entry and fills msgs
            payload = json.dumps({"command": list(args), "request_id": rid}) + "\n"
            try:
                self.sock.sendall(payload.encode())
            except OSError as exc:
                self._pending.pop(rid, None)
                raise MpvError(f"send failed: {exc}") from exc
        if not ev.wait(timeout):
            self._pending.pop(rid, None)
            raise MpvError(f"mpv did not answer {args[0]!r}")
        msg = msgs[0] if msgs else {}
        if msg.get("error") not in (None, "success"):
            raise MpvError(f"{args[0]}: {msg.get('error')}")
        return msg.get("data")

    def get(self, prop: str, default: Any = None) -> Any:
        try:
            return self.command("get_property", prop)
        except MpvError:
            return default

    def set(self, prop: str, value: Any) -> None:
        self.command("set_property", prop, value)

    def observe(self, prop: str, observe_id: int) -> None:
        self.command("observe_property", observe_id, prop)

    def loadfile(self, path: str, start: float = 0.0, extra: dict[str, Any] | None = None) -> None:
        opts = {"start": f"{max(0.0, start):.3f}"}
        if extra:
            opts.update(extra)
        # mpv >= 0.38 takes an options dict in argument 4; older builds want "key=value,key=value".
        try:
            self.command("loadfile", path, "replace", opts)
        except MpvError:
            self.command("loadfile", path, "replace", ",".join(f"{k}={v}" for k, v in opts.items()))

    def keybind(self, key: str, message: str) -> None:
        try:
            self.command("keybind", key, f"script-message {message}")
        except MpvError:
            pass

    # --- overlays ---------------------------------------------------------------------------

    def overlay_add(self, overlay_id: int, x: int, y: int, path: str, w: int, h: int) -> None:
        self.command("overlay-add", overlay_id, x, y, path, 0, "bgra", w, h, w * 4)

    def overlay_remove(self, overlay_id: int) -> None:
        try:
            self.command("overlay-remove", overlay_id)
        except MpvError:
            pass


def default_args(windowed: bool, osd_socket_dir: Path) -> list[str]:
    common = [
        "--idle=yes", "--force-window=yes", "--keep-open=no", "--no-osc", "--osd-level=0",
        "--input-default-bindings=no", "--input-vo-keyboard=yes", "--cursor-autohide=always",
        "--really-quiet", "--image-display-duration=inf", "--no-terminal",
        "--cache=yes", "--demuxer-max-bytes=64MiB", "--demuxer-readahead-secs=20",
        "--audio-pitch-correction=no", "--volume-max=100", "--sub=no", "--no-config",
    ]
    if windowed:
        return common + ["--geometry=1024x576", "--title=PiTV", "--hwdec=auto-safe"]
    return common + [
        "--vo=gpu", "--gpu-context=drm", "--fullscreen", "--ao=alsa",
        "--hwdec=drm-prime,v4l2m2m-copy", "--gpu-api=opengl", "--profile=fast",
        "--video-sync=audio",
    ]
