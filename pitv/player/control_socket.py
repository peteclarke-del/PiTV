"""Unix socket server: the web UI (and scripts) drive the player through it.

Protocol: one JSON object per line. Request {"cmd": ..., ...} -> reply {"ok": bool, ...}.
{"cmd": "subscribe"} keeps the connection open and streams the player state whenever it
changes (one JSON object per line).

The socket is group-writable only: the web service runs as the same user/group, and nothing
else on the Pi has a reason to drive the television. The web service relays requests from a
remote control page that is public on the LAN, so this side is still treated as untrusted
input: request size, connection count and subscriber count are bounded, and a subscriber
that stops reading is dropped rather than allowed to stall the player."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("pitv.control")

SOCKET_MODE = 0o660
MAX_REQUEST = 64 * 1024   # a command is a few dozen bytes; anything larger is not a client
MAX_CLIENTS = 16          # concurrent request connections, each served by its own thread
MAX_SUBSCRIBERS = 8       # the web service holds one; more are stale or misbehaving
REQUEST_TIMEOUT = 10.0    # seconds a client may take to send its request
SEND_TIMEOUT = 2.0        # a subscriber that cannot take a state line this fast is dropped


def _close(conn: socket.socket) -> None:
    try:
        conn.close()
    except OSError:
        pass


class ControlServer:
    def __init__(self, path: Path, handler: Callable[[dict[str, Any]], dict[str, Any]],
                 state_provider: Callable[[], dict[str, Any]]) -> None:
        self.path = path
        self.handler = handler
        self.state_provider = state_provider
        self._subs: list[socket.socket] = []   # oldest first, so the oldest is shed at the cap
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_CLIENTS)
        self._stop = threading.Event()
        self._sock: socket.socket | None = None

    def start(self) -> None:
        self.path.unlink(missing_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(self.path))
        self.path.chmod(SOCKET_MODE)
        s.listen(8)
        s.settimeout(1.0)
        self._sock = s
        threading.Thread(target=self._accept_loop, name="pitv-control", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            _close(self._sock)
        self.path.unlink(missing_ok=True)
        with self._lock:
            for c in self._subs:
                _close(c)
            self._subs.clear()

    def broadcast(self, state: dict[str, Any]) -> None:
        line = (json.dumps(state) + "\n").encode()
        with self._lock:
            live = []
            for c in self._subs:
                try:
                    c.sendall(line)
                    live.append(c)
                except OSError:   # includes the send timeout of a subscriber that stopped reading
                    _close(c)
            self._subs = live

    def _accept_loop(self) -> None:
        while not self._stop.is_set() and self._sock:
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                if self._stop.is_set():
                    break
                # EMFILE and friends are transient; ending the loop would leave the player
                # deaf to the web service until the next restart.
                log.warning("control socket accept failed: %s", exc)
                time.sleep(1.0)
                continue
            if not self._slots.acquire(blocking=False):
                log.warning("control socket: %d connections already open; refusing another", MAX_CLIENTS)
                _close(conn)
                continue
            threading.Thread(target=self._serve, args=(conn,), name="pitv-control-client",
                             daemon=True).start()

    @staticmethod
    def _read_request(conn: socket.socket) -> dict[str, Any] | None:
        """One JSON object terminated by a newline, or None for anything else."""
        buf = b""
        try:
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk or len(buf) + len(chunk) > MAX_REQUEST:
                    return None
                buf += chunk
            req = json.loads(buf.split(b"\n", 1)[0] or b"{}")
        except (OSError, ValueError, RecursionError):   # deep nesting exhausts the parser's stack
            return None
        return req if isinstance(req, dict) else None

    def _serve(self, conn: socket.socket) -> None:
        keep = False
        try:
            conn.settimeout(REQUEST_TIMEOUT)
            req = self._read_request(conn)
            if req is None:
                return
            if req.get("cmd") == "subscribe":
                keep = self._subscribe(conn)
                return
            try:
                reply = self.handler(req)
            except Exception:  # a failed command must not kill the server thread
                log.exception("control command %r failed", req.get("cmd"))
                reply = {"ok": False, "error": "command failed; see the player log"}
            try:
                conn.sendall((json.dumps(reply) + "\n").encode())
            except OSError:
                pass  # the client hung up before reading the reply
        finally:
            if not keep:
                _close(conn)
            self._slots.release()

    def _subscribe(self, conn: socket.socket) -> bool:
        """Send the current state, then add the connection to the broadcast list. The first
        line goes out before the connection is listed so it cannot interleave with a broadcast
        from the main thread. Returns whether the connection is kept."""
        conn.settimeout(SEND_TIMEOUT)
        try:
            first = (json.dumps(self.state_provider()) + "\n").encode()
        except Exception:  # a broken snapshot must not kill the server thread
            log.exception("state snapshot for a new subscriber failed")
            return False
        try:
            conn.sendall(first)
        except OSError:
            return False
        with self._lock:
            if self._stop.is_set():
                return False
            self._subs.append(conn)
            while len(self._subs) > MAX_SUBSCRIBERS:
                _close(self._subs.pop(0))
        return True
