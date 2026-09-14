"""Rotating log files under <data>/logs, readable from the admin Logs page on any platform
(journald is used as well on the Pi, but files work everywhere and survive a reboot)."""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

from .config import Config

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_LINE = re.compile(r"^(?P<ts>\S+ \S+) (?P<level>[A-Z]+)\s+(?P<logger>\S+): (?P<msg>.*)$")
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


def log_dir(cfg: Config) -> Path:
    d = cfg.data_dir / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def setup_logging(cfg: Config, name: str, level: int = logging.INFO, console: bool = True) -> Path:
    path = log_dir(cfg) / f"{name}.log"
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(fh)
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(ch)
    logging.getLogger("uvicorn.access").disabled = True
    return path


def tail(path: Path, lines: int, q: str = "", min_level: str = "") -> list[dict]:
    if not path.exists():
        return []
    # Read the last ~1 MB at most; plenty for a few thousand lines.
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 1_000_000))
        raw = f.read().decode("utf-8", errors="replace")
    out: list[dict] = []
    min_num = LEVELS.get(min_level, 0)
    current: dict | None = None
    for line in raw.splitlines():
        m = _LINE.match(line)
        if m:
            current = {"ts": m.group("ts"), "level": m.group("level"), "logger": m.group("logger"), "msg": m.group("msg")}
            out.append(current)
        elif current is not None:
            current["msg"] += "\n" + line  # traceback continuation
    if min_num:
        out = [e for e in out if LEVELS.get(e["level"], 0) >= min_num]
    if q:
        ql = q.lower()
        out = [e for e in out if ql in e["msg"].lower() or ql in e["logger"].lower()]
    return out[-lines:]
