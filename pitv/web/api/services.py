"""The systemd units of both applications and how each one is doing, for the admin's System page.

One `systemctl show` covers every unit. Beside it sits a live check (does the process answer?),
so the page is right on the Pi and also on a desktop, where nothing runs under systemd."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .deps import run_cmd


@dataclass(frozen=True)
class Unit:
    unit: str
    app: str                        # pitv | content: the application the unit belongs to
    role: str
    kind: str                       # daemon | run (oneshot, on demand or by timer) | boot (oneshot at boot) | timer
    actions: tuple[str, ...] = ()   # what the admin may do to it; setup/install.sh grants exactly these


CONTENT_RUN = "pitv-content.service"
CONTENT_TIMER = "pitv-content.timer"
UNITS = (
    Unit("pitv-web.service", "pitv", "Web interface, admin, and the API pitv_content reports to", "daemon", ("restart",)),
    Unit("pitv-player.service", "pitv", "Playback, on-screen graphics, the remote, schedule upkeep", "daemon",
         ("restart", "stop", "start")),
    Unit("pitv-splash.service", "pitv", "Test card on the screen early in boot", "boot"),
    Unit("pitv-content-api.service", "content", "pitv_content's API: sources, index, jobs, settings, log", "daemon", ("restart",)),
    Unit(CONTENT_RUN, "content", "A pitv_content run: works PiTV's manifest into the cache", "run", ("start",)),
    Unit(CONTENT_TIMER, "content", "Hourly tick that starts pitv_content runs at their configured hours", "timer"),
)
SERVICE_ACTIONS = {u.unit: u.actions for u in UNITS if u.actions}
_PROPS = "Id,LoadState,ActiveState,SubState,Result,UnitFileState,ActiveEnterTimestamp,NRestarts,MemoryCurrent"


def systemd_state(units: list[str]) -> dict[str, dict[str, str]]:
    """systemd's properties per unit id, from one call; empty where systemctl is unavailable."""
    _, out, _ = run_cmd(["systemctl", "show", "--timestamp=unix", "-p", _PROPS, *units])
    states = {}
    for block in out.split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if props.get("Id"):
            states[props["Id"]] = props
    return states


def _count(value: str | None) -> int | None:
    """systemd's numbers; its 'not set' and 2^64-1 (infinity) come back as None."""
    try:
        n = int(value or "")
    except ValueError:
        return None
    return n if 0 <= n < 2 ** 63 else None


def assess(u: Unit, props: dict[str, str] | None, responding: bool | None, on_pi: bool = True) -> tuple[str, str]:
    """(health, description). health is ok, idle, warn, down or absent; `responding` is the live
    check (None where there is none). A desktop runs both apps by hand, so a unit missing there
    is described by what stands in for it rather than reported as a fault."""
    if not props or props.get("LoadState") in ("", "not-found"):
        if u.kind == "timer" and responding:
            return "idle", "no timer; runs start through the API"
        if u.kind == "run" and responding is not None:
            return ("ok", "running") if responding else ("idle", "no unit; runs start through the API")
        if responding:
            return "ok", "running outside systemd"
        if u.kind == "boot" and not on_pi:
            return "idle", "Pi only"
        return ("absent", "not installed") if props else ("warn", "systemd state unavailable")
    active, sub, result = props.get("ActiveState", ""), props.get("SubState", ""), props.get("Result", "")
    if active == "failed":
        return "down", f"failed ({result})"
    if u.kind == "daemon":
        if active == "active":
            return ("ok", sub) if responding is not False else ("warn", f"{sub}, not answering")
        return ("warn", active) if active in ("activating", "reloading", "deactivating") else ("down", "stopped")
    if u.kind == "run":
        if active in ("active", "activating") or responding:
            return "ok", "running"
        return ("idle", "idle, last run ok") if result == "success" else ("warn", f"idle, last run {result}")
    if u.kind == "boot":
        return ("ok", "done") if active == "active" else ("idle", sub or active)
    return ("ok", sub) if active == "active" else ("down", "stopped")


def services(live: dict[str, bool | None], on_pi: bool = True) -> list[dict[str, Any]]:
    """Every unit of both applications with its state. `live` maps unit ids to the live check."""
    states = systemd_state([u.unit for u in UNITS])
    rows = []
    for u in UNITS:
        props = states.get(u.unit)
        health, state = assess(u, props, live.get(u.unit), on_pi)
        known = props if (props or {}).get("LoadState") == "loaded" else {}   # systemd's numbers mean nothing otherwise
        since = known.get("ActiveEnterTimestamp", "")
        rows.append({
            "unit": u.unit, "app": u.app, "role": u.role, "kind": u.kind, "health": health, "state": state,
            "enabled": known.get("UnitFileState") or None,
            "since_ts": _count(since[1:]) if since.startswith("@") and health in ("ok", "warn") else None,
            "restarts": _count(known.get("NRestarts")) if u.kind == "daemon" else None,
            "memory": _count(known.get("MemoryCurrent")),
            # Only a long-running process can be asked whether it answers; for a run the live
            # check means "busy" and is already folded into its state.
            "responding": live.get(u.unit) if u.kind == "daemon" else None,
            "actions": list(u.actions) if known else [],   # systemctl needs the unit installed
        })
    return rows
