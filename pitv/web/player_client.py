"""Talk to the player daemon over its Unix control socket (newline-delimited JSON)."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

MAX_REPLY = 1 << 20   # a state reply is a few KB


class PlayerClient:
    def __init__(self, path: Path) -> None:
        self.path = path

    def call(self, cmd: str, timeout: float = 2.0, **kwargs: Any) -> dict[str, Any]:
        msg = json.dumps({"cmd": cmd, **kwargs}) + "\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(str(self.path))
                s.sendall(msg.encode())
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk or len(buf) + len(chunk) > MAX_REPLY:
                        break
                    buf += chunk
            return json.loads(buf.decode() or "{}")
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"player unavailable: {exc}", "offline": True}

    def state(self) -> dict[str, Any]:
        return self.call("state")

    def key(self, key: str) -> dict[str, Any]:
        return self.call("key", key=key)
