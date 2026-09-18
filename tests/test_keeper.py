"""The web service keeps the television on."""

from __future__ import annotations

import threading

from pitv.config import Config
from pitv.web import keeper


def test_keeper_starts_a_stopped_player(tmp_path, monkeypatch):
    """A player that has been silent past the grace period is started, once, and not again until
    the retry gap has passed; one that is online, or one the setting says to leave, is not."""
    cfg = Config(data_dir=tmp_path / "data", run_dir=tmp_path / "run")
    cfg.ensure_dirs()
    started: list[str] = []
    monkeypatch.setattr(keeper, "start_player", lambda _cfg: started.append("start") or {"ok": True, "via": "test", "error": ""})
    clock = {"t": 1000.0}
    monkeypatch.setattr(keeper.time, "monotonic", lambda: clock["t"])
    state = {"online": False}
    enabled = {"on": True}
    k = keeper.Keeper(cfg, lambda: state, lambda: enabled["on"], threading.Event())

    k._tick()                       # first sight of it offline: the clock starts
    clock["t"] += keeper.OFFLINE_GRACE - 1
    k._tick()
    assert not started, "within the grace period nothing happens"
    clock["t"] += 2
    k._tick()
    assert started == ["start"]
    clock["t"] += 10
    k._tick()
    assert started == ["start"], "not again until the retry gap has passed"
    clock["t"] += keeper.RETRY_AFTER
    k._tick()
    assert started == ["start", "start"]

    state["online"] = True
    k._tick()
    assert k.offline_since is None, "back online: the clock is forgotten"
    state["online"] = False
    enabled["on"] = False
    clock["t"] += keeper.RETRY_AFTER + keeper.OFFLINE_GRACE + 5
    k._tick()
    k._tick()
    assert started == ["start", "start"], "switched off in Settings, it only watches"


def test_start_player_falls_back_to_the_command(tmp_path, monkeypatch):
    """With no systemd unit anywhere, the player is started as a detached command, and a second
    call while that is coming up does not start another."""
    cfg = Config(data_dir=tmp_path / "data", run_dir=tmp_path / "run")
    cfg.ensure_dirs()
    monkeypatch.setattr(keeper, "systemd_state", lambda units, user=False: {})
    launched: list[list[str]] = []

    class FakeProc:
        pid = 4242

    monkeypatch.setattr(keeper.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd) or FakeProc())
    monkeypatch.setattr(keeper, "_process_alive", lambda pid_file: pid_file.exists())
    first = keeper.start_player(cfg)
    assert first["ok"] and first["via"] == "command" and launched and launched[0][-1] == "play"
    assert (cfg.run_dir / "player.pid").read_text() == "4242"
    second = keeper.start_player(cfg)
    assert second["via"] == "already starting" and len(launched) == 1
