"""Bootstrap configuration.

Only the handful of settings needed before the database is open live here (paths, sockets,
ports). Everything that describes the television itself (channels, sources, weights,
dayparts) lives in the database and is edited through the admin UI.

Values come from environment variables, falling back to sensible per-platform defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_data_dir() -> Path:
    if os.environ.get("PITV_DATA"):
        return Path(os.environ["PITV_DATA"])
    if os.geteuid() == 0 or Path("/var/lib/pitv").exists():
        return Path("/var/lib/pitv")
    xdg = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    return Path(xdg) / "pitv"


def _default_run_dir() -> Path:
    if os.environ.get("PITV_RUN"):
        return Path(os.environ["PITV_RUN"])
    if Path("/run/pitv").exists():
        return Path("/run/pitv")
    xdg = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/pitv-{os.getuid()}")
    return Path(xdg) / "pitv"


@dataclass
class Config:
    data_dir: Path = field(default_factory=_default_data_dir)
    run_dir: Path = field(default_factory=_default_run_dir)
    web_host: str = field(default_factory=lambda: os.environ.get("PITV_WEB_HOST", "0.0.0.0"))
    web_port: int = field(default_factory=lambda: int(os.environ.get("PITV_WEB_PORT", "8080")))
    mpv_binary: str = field(default_factory=lambda: os.environ.get("PITV_MPV", "mpv"))
    ffprobe_binary: str = field(default_factory=lambda: os.environ.get("PITV_FFPROBE", "ffprobe"))
    # Desktop development: run mpv in a window rather than on DRM.
    windowed: bool = field(default_factory=lambda: os.environ.get("PITV_WINDOWED", "") == "1")
    # Extra mpv arguments (space separated), e.g. "--vo=null --ao=null" for headless tests.
    mpv_extra_args: list[str] = field(default_factory=lambda: os.environ.get("PITV_MPV_ARGS", "").split())

    @property
    def db_path(self) -> Path:
        return Path(os.environ.get("PITV_DB", str(self.data_dir / "pitv.db")))

    @property
    def mpv_socket(self) -> Path:
        return self.run_dir / "mpv.sock"

    @property
    def player_socket(self) -> Path:
        return self.run_dir / "player.sock"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.run_dir.mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    return Config()
