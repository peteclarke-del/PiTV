"""Minimal systemd sd_notify (no dependency): READY=1, WATCHDOG=1, STATUS=..."""

from __future__ import annotations

import os
import socket

_sock: socket.socket | None = None


def notify(message: str) -> bool:
    global _sock
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        if _sock is None:
            _sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        _sock.sendto(message.encode(), addr)
        return True
    except OSError:
        return False


def ready() -> bool:
    return notify("READY=1")


def watchdog() -> bool:
    return notify("WATCHDOG=1")


def status(text: str) -> bool:
    return notify(f"STATUS={text[:200]}")


def watchdog_interval() -> float | None:
    """Half of WATCHDOG_USEC (systemd's WatchdogSec), or None when not under a watchdog."""
    usec = os.environ.get("WATCHDOG_USEC")
    try:
        return int(usec) / 1_000_000 / 2 if usec else None
    except ValueError:
        return None


def rss_mb(pid: int | None = None) -> float | None:
    try:
        with open(f"/proc/{pid or 'self'}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError):
        return None
    return None
