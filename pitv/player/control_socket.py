"""Unix socket server: the web UI (and scripts) drive the player through it.

Protocol: one JSON object per line. Request {"cmd": ..., ...} -> reply {"ok": bool, ...}.
{"cmd": "subscribe"} keeps the connection open and streams the player state whenever it
changes (one JSON object per line).

The socket is group-writable only: the web service runs as the same user/group, and nothing
else on the Pi has a reason to drive the television."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("pitv.control")

SOCKET_MODE = 0o660
MAX_REQUEST = 64 * 1024   # a command is a few dozen bytes; anything larger is not a client


class ControlServer:
    def __init__(self, path: Path, handler: Callable[[dict[str, Any]], dict[str, Any]],
                 state_provider: Callable[[], dict[str, Any]]) -> None:
        self.path = path
        self.handler = handler
        self.state_provider = state_provider
        self._subs: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock: socket.socket | None = None

    def start(self) -> None:
        if self.path.exists():
            self.path.unlink()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(self.path))
        os.chmod(self.path, SOCKET_MODE)
        s.listen(8)
        s.settimeout(1.0)
        self._sock = s
        threading.Thread(target=self._accept_loop, name="pitv-control", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            self._sock.close()
        with self._lock:
            for c in self._subs:
                try:
                    c.close()
                except OSError:
                    pass
            self._subs.clear()

    def broadcast(self, state: dict[str, Any] | None = None) -> None:
        state = state or self.state_provider()
        line = (json.dumps(state) + "\n").encode()
        with self._lock:
            dead = []
            for c in self._subs:
                try:
                    c.sendall(line)
                except OSError:
                    dead.append(c)
            for c in dead:
                self._subs.discard(c)
                try:
                    c.close()
                except OSError:
                    pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set() and self._sock:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

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
        except (OSError, ValueError):
            return None
        return req if isinstance(req, dict) else None

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(10)
        req = self._read_request(conn)
        if req is None:
            conn.close()
            return
        if req.get("cmd") == "subscribe":
            conn.settimeout(None)
            with self._lock:
                self._subs.add(conn)
            try:
                conn.sendall((json.dumps(self.state_provider()) + "\n").encode())
            except OSError:
                with self._lock:
                    self._subs.discard(conn)
                conn.close()
            return
        try:
            reply = self.handler(req)
        except Exception as exc:  # noqa: BLE001
            log.exception("control command failed")
            reply = {"ok": False, "error": repr(exc)}
        try:
            conn.sendall((json.dumps(reply) + "\n").encode())
        except OSError:
            pass  # the client hung up before reading the reply
        finally:
            conn.close()
