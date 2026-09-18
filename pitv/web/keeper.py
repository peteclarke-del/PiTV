"""Keep the television on: start the player whenever it is found stopped.

On the Pi systemd restarts a player that dies, but not one that was stopped, and on a desktop
there is no systemd at all: close the window and the television stays off until somebody
remembers the command. The web service is the one process that is always up, so it watches
the player's state stream and, when the player has been silent for a while, starts it by
whatever means the machine has: the system unit, the development user unit, or the `pitv play`
command itself, detached, with its output in the data directory.

The same routine answers the admin's Start button, so a phone can turn the television on.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..config import Config
from ..player.hwdec import is_raspberry_pi
from .api.deps import run_cmd
from .api.services import DEV_UNITS, systemd_state

log = logging.getLogger("pitv.keeper")

PLAYER_UNIT = "pitv-player.service"
OFFLINE_GRACE = 20        # seconds the player may be silent before it is taken as stopped
RETRY_AFTER = 60          # seconds between start attempts, so a player that keeps dying is not thrashed
CHECK_EVERY = 5


def start_player(cfg: Config) -> dict[str, Any]:
    """Start the player by whatever this machine has; says how, or why not.

    The system unit first (sudo is allowed for exactly this), then a development user unit,
    then the command itself: the same `pitv play` the units run, detached from the web service
    so it survives a restart of it, windowed off the Pi, logging to the data directory."""
    props = systemd_state([PLAYER_UNIT]).get(PLAYER_UNIT) or {}
    if props.get("LoadState") == "loaded":
        rc, _, err = run_cmd(["sudo", "-n", "systemctl", "start", PLAYER_UNIT], timeout=15)
        return {"ok": rc == 0, "via": "systemd", "error": err if rc else ""}
    dev_unit = DEV_UNITS[PLAYER_UNIT]
    dev = systemd_state([dev_unit], user=True).get(dev_unit) or {}
    if dev.get("LoadState") == "loaded":
        rc, _, err = run_cmd(["systemctl", "--user", "start", dev_unit], timeout=15)
        return {"ok": rc == 0, "via": "systemd --user", "error": err if rc else ""}
    if _process_alive(cfg.run_dir / "player.pid"):
        return {"ok": True, "via": "already starting", "error": ""}
    return _spawn(cfg)


def _spawn(cfg: Config) -> dict[str, Any]:
    binary = Path(sys.executable).with_name("pitv")
    cmd = [str(binary), "play"] if binary.exists() else [sys.executable, "-m", "pitv.cli", "play"]
    env = dict(os.environ)
    if not is_raspberry_pi():
        env.setdefault("PITV_WINDOWED", "1")
    out_path = cfg.data_dir / "player.out"
    try:
        with out_path.open("ab") as out:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    env=env, start_new_session=True)
        (cfg.run_dir / "player.pid").write_text(str(proc.pid))    # what setup/dev.sh reads too
    except OSError as exc:
        log.error("could not start the player: %s", exc)
        return {"ok": False, "via": "command", "error": str(exc)}
    log.info("player started by command (pid %s); output in %s", proc.pid, out_path)
    return {"ok": True, "via": "command", "error": ""}


def _process_alive(pid_file: Path) -> bool:
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


class Keeper:
    """The watch itself: a thread in the web service that starts a stopped player."""

    def __init__(self, cfg: Config, state: Any, enabled: Any, stop: threading.Event) -> None:
        self.cfg = cfg
        self.state = state                     # callable: the player's last known state
        self.enabled = enabled                 # callable: the keep-alive setting, read each pass
        self.stop = stop
        self.offline_since: float | None = None
        self.last_attempt = 0.0
        self.last: dict[str, Any] = {}

    def start(self) -> None:
        threading.Thread(target=self._loop, name="pitv-keeper", daemon=True).start()

    def _loop(self) -> None:
        while not self.stop.wait(CHECK_EVERY):
            try:
                self._tick()
            except Exception:      # a bad pass must not end the watch
                log.exception("keeper pass failed")

    def _tick(self) -> None:
        now = time.monotonic()
        if self.state().get("online"):
            self.offline_since = None
            return
        if self.offline_since is None:
            self.offline_since = now
            return
        if not self.enabled() or now - self.offline_since < OFFLINE_GRACE or now - self.last_attempt < RETRY_AFTER:
            return
        self.last_attempt = now
        self.last = start_player(self.cfg)
        if self.last["ok"]:
            log.info("player was off for %.0fs; started it (%s)", now - self.offline_since, self.last["via"])
        else:
            log.warning("player is off and could not be started (%s): %s", self.last["via"], self.last["error"])
