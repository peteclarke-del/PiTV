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
# Catalogue imports and schedule builds run in the player, the web service and the CLI, so
# every process also writes those loggers to a shared per-topic file the admin can show.
TOPIC_LOGS = {"catalogue": ("pitv.catalogue",), "schedule": ("pitv.scheduler", "pitv.readiness"),
              "stream": ("pitv.stream",)}
TAIL_BYTES = 1_000_000   # how much of a log `tail` reads: a few thousand lines


def log_dir(cfg: Config) -> Path:
    d = cfg.data_dir / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d



def _replace_handlers(logger: logging.Logger, *handlers: logging.Handler) -> None:
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    for h in handlers:
        logger.addHandler(h)


def setup_logging(cfg: Config, name: str, level: int = logging.INFO, console: bool = True) -> Path:
    """Send this process's logging to <data>/logs/<name>.log (and the console), plus the topic
    files for the loggers in TOPIC_LOGS. Returns the process's own log file."""
    folder = log_dir(cfg)
    fmt = logging.Formatter(FORMAT)
    path = folder / f"{name}.log"
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handlers: list[logging.Handler] = [fh]
    if console:
        handlers.append(logging.StreamHandler())
    for h in handlers:
        h.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(level)
    _replace_handlers(root, *handlers)
    logging.getLogger("uvicorn.access").disabled = True
    for topic, loggers in TOPIC_LOGS.items():
        if topic == name:
            continue   # already this process's own file
        th = logging.handlers.RotatingFileHandler(folder / f"{topic}.log", maxBytes=1_000_000, backupCount=2,
                                                  encoding="utf-8")
        th.setFormatter(fmt)
        for lg in loggers:
            _replace_handlers(logging.getLogger(lg), th)
    return path


def tail(path: Path, lines: int, q: str = "", min_level: str = "") -> list[dict]:
    """The last `lines` entries of a log file (a traceback stays with its entry), keeping only
    those at `min_level` or above whose message or logger contains `q`."""
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - TAIL_BYTES))
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
    return out[-lines:] if lines > 0 else []
