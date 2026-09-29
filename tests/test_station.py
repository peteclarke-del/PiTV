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


def test_channel_keys_show_the_banner_at_once_and_load_where_they_stop(monkeypatch):
    """Each channel key loaded its channel before drawing the banner, so stepping through five
    channels loaded five files and the banner trailed the keys. The banner comes first, and a
    run of channel keys loads only the channel it lands on, once the keys stop."""
    from types import SimpleNamespace

    from pitv.player import controller as controller_mod
    from pitv.player.controller import Player

    calls: list[str] = []
    clock = [100.0]
    monkeypatch.setattr(controller_mod.time, "monotonic", lambda: clock[0])
    p = SimpleNamespace(
        channels=[{"id": n, "number": n} for n in (1, 2, 3)], channel={"id": 1, "number": 1},
        failed_slot_id=None, settings={"channel_switch_static": False}, _settle_until=0.0,
        _end_history=lambda: None, _forget_slot=lambda: None, _unpause=lambda: None,
        show_badge=lambda: calls.append(f"badge {p.channel['number']}"),
        play_live=lambda: calls.append(f"load {p.channel['number']}"),
        _save_state=lambda: None, _publish=lambda force=False: None)

    Player.tune(p, 2, settle=True)
    Player.tune(p, 3, settle=True)
    assert calls == ["badge 2", "badge 3"], "each key's banner at once, nothing loaded yet"

    tick = SimpleNamespace(**vars(p), mpv=SimpleNamespace(running=lambda: True), osd_expiry={}, standby=False)
    Player.tick(tick)
    assert calls[-1] == "badge 3", "still settling"
    clock[0] += controller_mod.CHANNEL_SETTLE_SECONDS + 0.01
    Player.tick(tick)
    assert calls[-1] == "load 3" and calls.count("load 3") == 1 and "load 2" not in calls
    assert tick._settle_until == 0.0

    calls.clear()
    Player.tune(p, 1)
    assert calls == ["badge 1", "load 1"], "a tune that is not a channel key loads at once, banner first"


def test_the_subtitle_track_shown_is_chosen_by_language_not_by_the_files_flags():
    """mpv's own choice follows the default flag an author happened to set, so two files with
    the same subtitles behaved differently. The player picks: the configured language first,
    then a track with no label, a forced track (foreign dialogue only) last, and never a track
    labelled in another language."""
    from pitv.player.subtitles import choose_track

    def sub(tid, lang=None, **flags):
        return {"id": tid, "type": "sub", "lang": lang, **flags}

    video, audio = {"id": 1, "type": "video"}, {"id": 1, "type": "audio", "lang": "eng"}
    assert choose_track([video, audio], "en") is None
    assert choose_track([video, sub(1, "fre", default=True), sub(2, "eng")], "en")["id"] == 2
    assert choose_track([sub(1, "eng", forced=True), sub(2, "en-GB")], "en")["id"] == 2
    assert choose_track([sub(1, "eng", forced=True)], "en")["id"] == 1, "a forced track is better than none"
    assert choose_track([sub(1), sub(2, "eng")], "en")["id"] == 2
    assert choose_track([sub(1, "und"), sub(2)], "en")["id"] == 1, "equals keep the file's order"
    assert choose_track([sub(1, "ger"), sub(2, "spa")], "en") is None
    assert choose_track([sub(1, "ger")], "de")["id"] == 1


def test_subtitles_are_the_viewers_switch_and_show_only_what_the_file_has(monkeypatch):
    """The subtitles key turns them on for every programme that has a track and says so when
    the one on air has none; each file is loaded with no track, because a track number chosen
    for one file means something else in the next."""
    from types import SimpleNamespace

    from pitv.player import controller as controller_mod
    from pitv.player.controller import Player

    sets: list[tuple] = []
    notices: list[tuple] = []
    tracks = [{"id": 1, "type": "video"}, {"id": 3, "type": "sub", "lang": "eng", "codec": "subrip"}]
    loaded: dict = {}
    mpv = SimpleNamespace(get=lambda prop, default=None: tracks if prop == "track-list" else default,
                          set=lambda name, value: sets.append((name, value)),
                          loadfile=lambda path, start, options: loaded.update(options) or 2,
                          overlay_remove=lambda _id: None)
    p = SimpleNamespace(mpv=mpv, subtitles=False, subtitle_track=None, playing_path="/c/film.mkv", paused=False,
                        on_pi=False, channel={"number": 1}, settings={"subtitle_language": "en"},
                        renderer=SimpleNamespace(notice=lambda label, detail="": (label, detail)),
                        _overlay=lambda _oid, rendered, ttl: notices.append(rendered),
                        _sync_osd_size=lambda: None,
                        _decode_props=lambda *_a: {}, _start_history=lambda _s: None)
    p._apply_subtitles = lambda: Player._apply_subtitles(p)

    Player._apply_subtitles(p)
    assert sets == [], "off, and the file was loaded with none: nothing to do"

    Player._toggle_subtitles(p)
    assert p.subtitles and ("sid", 3) in sets and p.subtitle_track["lang"] == "eng"
    assert notices[-1] == ("SUBTITLES ON", "")

    sets.clear()
    Player._apply_subtitles(p)
    assert sets == [], "the settings reload must not reselect the track every minute"

    monkeypatch.setattr(controller_mod, "decode_options", lambda *_a, **_k: {"hwdec": "no", "deinterlace": "no"})
    Player._load(p, {"id": 5, "kind": "programme", "title": "Next"}, {"id": 1}, "/c/next.mkv", "cache", 0.0)
    assert loaded["sid"] == "no" and p.subtitle_track is None

    tracks[:] = [{"id": 1, "type": "video"}]
    Player._apply_subtitles(p)
    assert p.subtitle_track is None
    Player._toggle_subtitles(p)
    Player._toggle_subtitles(p)
    assert notices[-1] == ("SUBTITLES ON", "None with this programme")


def test_subtitles_is_a_remote_key_and_its_language_is_checked():
    import pytest

    from pitv.player.controller import validate_control
    from pitv.settings_rules import SettingError, check_setting

    assert validate_control({"cmd": "key", "key": "subtitles"}) == ("key", "subtitles")
    assert check_setting("subtitle_language", " EN ") == "en"
    for bad in ("english", "", "e1", 5):
        with pytest.raises(SettingError):
            check_setting("subtitle_language", bad)


def test_the_subtitles_setting_is_the_switch_at_start_and_when_it_is_changed():
    """The remote's switch lasts until the player next starts. What an unattended set does is
    the configuration's to say, so a change to the setting moves the switch at once, and a
    reload that finds the setting unchanged leaves the viewer's choice alone."""
    from types import SimpleNamespace

    from pitv.player.controller import Player

    sets: list[tuple] = []
    settings = {"subtitles_default": False, "subtitle_font_size": 40, "osd_safe_margin": 0.07, "keymap": {}}
    mpv = SimpleNamespace(args=["x"], set=lambda name, value: sets.append((name, value)))
    applied: list[bool] = []
    p = SimpleNamespace(settings=dict(settings), subtitles=True, mpv=mpv, tz=None,
                        station=SimpleNamespace(settings=lambda: dict(settings), timezone=lambda: None),
                        evdev=SimpleNamespace(set_keymap=lambda _k: None), _load_channels=lambda: None,
                        _mpv_args=lambda: ["x"], _apply_subtitles=lambda: applied.append(p.subtitles))
    p._style_subtitles = lambda: Player._style_subtitles(p)

    Player._reload_settings(p)
    assert applied == [True], "the viewer switched them on and the setting has not changed"
    assert ("sub-font-size", 40) in sets and ("sub-margin-y", 50) in sets

    settings["subtitles_default"] = True
    p.subtitles = False
    Player._reload_settings(p)
    assert applied[-1] is True, "switching the setting on switches subtitles on"
