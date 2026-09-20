"""The seam between a television's front end and the station that schedules for it."""

import inspect
import re

from conftest import make_library

from pitv.player import controller
from pitv.player.station import LocalStation, Station
from pitv.scheduler.horizon import build_horizon
from pitv.scheduler.rules import local_ts, tz_of
from pitv.scheduler.slots import parse_day


def test_the_player_knows_the_schedule_only_through_its_station():
    """PiTV runs all in one and also with a remote front end. The player is the front end in both,
    so it may not reach for the database itself: a query added to the controller would work on
    one machine and nowhere else."""
    source = inspect.getsource(controller)
    for forbidden in ("dbm.connect", "self.conn", "sqlite3"):
        assert forbidden not in source, f"the player reaches past its station: {forbidden}"
    for query in ("slot_at", "block_entry", "next_programmes", "enabled_channels", "all_settings", "tz_of"):
        assert not re.search(rf"(?<!station\.){query}\(", source), f"the player calls {query} itself, not through its station"
    asked = {name for name, _ in inspect.getmembers(Station, inspect.isfunction) if not name.startswith("_")}
    assert asked <= {name for name, _ in inspect.getmembers(LocalStation, inspect.isfunction)}


def test_the_local_station_answers_from_the_database(tmp_path):
    ctx = make_library(tmp_path, 3)
    conn = ctx["conn"]
    day = parse_day("2026-09-14")
    build_horizon(conn, start_day=day, days=1, now=local_ts(day, "07:00", tz_of(conn)), seed=1, force=True)
    station = LocalStation(ctx["cfg"].db_path)
    channels = station.channels()
    assert channels and station.settings()["day_start"] and str(station.timezone())
    at = local_ts(day, "20:00", tz_of(conn))
    slot = station.slot_at(channels[0]["id"], at)
    assert slot and slot["start_ts"] <= at < slot["end_ts"]
    after = station.next_programmes(channels[0]["id"], slot["end_ts"], 3)
    assert after and all(p["start_ts"] >= slot["end_ts"] for p in after)
    programme = slot if slot["kind"] == "programme" else after[0]
    handle = station.watched(programme, at)
    station.watched_until(handle, at + 60)
    row = conn.execute("SELECT started_at, ended_at, title FROM history WHERE id = ?", (handle,)).fetchone()
    assert (row["started_at"], row["ended_at"], row["title"]) == (at, at + 60, programme["title"])
    station.close()
