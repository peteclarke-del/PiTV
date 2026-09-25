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


def test_a_short_overrun_holds_the_picture_instead_of_flashing_a_card():
    """A file is almost never exactly as long as the slot it was given. The player showed the
    continuity card for whatever was left over, which on a music channel of three minute videos
    put a card on screen for a fraction of a second between one and the next: on and gone before
    it could be read, which looks like a fault rather than continuity.

    Under the threshold the last frame holds, as a broadcast does at a junction. Past it there is
    a real gap and the card belongs. The threshold is `card_after_seconds`, because how long is
    too long is a judgement about the set, not a constant."""
    from types import SimpleNamespace

    from pitv.player.controller import Player

    shown: list[str] = []

    def stub(left: float, after: int):
        now = 1_000_000
        return SimpleNamespace(
            _current_entry=lambda _e: True, playing_path="/some/file.mp4", behind_live=True, paused=True,
            channel={"id": 1, "number": 5}, clock=lambda: now, playing_slot_id=7,
            settings={"card_after_seconds": after},
            station=SimpleNamespace(slot_at=lambda _c, _t: {"id": 7, "end_ts": now + left}),
            _show_testcard=lambda text, sub: shown.append(text),
            play_live=lambda: shown.append("LOADED NEXT"))

    Player.do(stub(0.4, 15), "eof", None)
    assert shown == [], "a fraction of a second left is a junction, not a gap"

    Player.do(stub(14.0, 15), "eof", None)
    assert shown == [], "still under the threshold"

    Player.do(stub(600.0, 15), "eof", None)
    assert shown == ["Programmes will continue shortly"], "ten minutes is a real gap and says so"

    # The threshold is the setting's, not a constant: raising it holds the picture for longer.
    shown.clear()
    Player.do(stub(30.0, 60), "eof", None)
    assert shown == []


def test_a_programme_freezes_on_its_last_frame_rather_than_going_blank():
    """Holding the picture only works if mpv keeps it. It is launched with keep-open off, so a
    file that ends leaves an empty window, and "show no card" would have traded a flashing card
    for a flash of nothing. The option is set per file, so it does not leak onto the card or the
    test signal, which are stills that must not stop the player advancing."""
    import inspect

    from pitv.player import controller as controller_mod

    source = inspect.getsource(controller_mod.Player._load)
    assert '"keep-open"' in source, "a programme must hold its last frame when it ends early"


def test_the_next_programme_after_a_held_frame_plays(monkeypatch):
    """mpv pauses itself at the end of a file kept open to hold its last frame, and the pause
    outlived the file: the next programme loaded paused, and the drift check re-seeked it every
    ten seconds, a jerky still with no sound until a channel change unpaused it. A load now
    plays unless the viewer has paused."""
    from types import SimpleNamespace

    from pitv.player import controller as controller_mod
    from pitv.player.controller import Player

    calls: list[tuple] = []
    mpv = SimpleNamespace(loadfile=lambda path, start, options: 2,
                          set=lambda name, value: calls.append((name, value)),
                          overlay_remove=lambda _id: None)
    monkeypatch.setattr(controller_mod, "decode_options", lambda *_a, **_k: {"hwdec": "no", "deinterlace": "no"})

    def player(paused: bool):
        return SimpleNamespace(mpv=mpv, paused=paused, on_pi=False, settings={}, channel={"number": 8},
                               _decode_props=lambda *_a: {}, _start_history=lambda _s: None)
    slot = {"id": 5, "kind": "programme", "title": "Next"}
    Player._load(player(False), slot, {"id": 1}, "/n/next.mp4", "nas", 0.0)
    assert ("pause", False) in calls, "the file after a held frame must play"
    calls.clear()
    Player._load(player(True), slot, {"id": 1}, "/n/next.mp4", "nas", 0.0)
    assert ("pause", True) in calls, "a viewer's own pause is kept"
