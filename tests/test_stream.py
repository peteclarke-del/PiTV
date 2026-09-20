import time
from pathlib import Path
from types import SimpleNamespace

from pitv.stream import Streams, _Channel, ffmpeg_command
from pitv.web.api.stream import _viewer_playlist


def test_web_stream_is_browser_safe_even_when_source_is_not(tmp_path):
    cmd = ffmpeg_command(
        Path("source.mkv"), start=0, seconds=60, out_dir=tmp_path, seq=0, segment_seconds=4,
        media={"vcodec": "hevc", "acodec": "ac3"}, where="nas",
        profile={"width": 1280, "height": 720, "max_bitrate_kbps": 4000}, encoder="libx264",
    )
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-profile:v") + 1] == "main"
    assert cmd[cmd.index("-preset") + 1] == "veryfast"
    assert cmd[cmd.index("-tune") + 1] == "zerolatency"
    assert cmd[cmd.index("-fflags") + 1] == "+genpts+discardcorrupt"
    assert cmd[cmd.index("-err_detect") + 1] == "ignore_err"
    assert cmd[cmd.index("-af") + 1] == "aresample=async=1:first_pts=0"
    assert cmd[cmd.index("-avoid_negative_ts") + 1] == "make_zero"
    assert "discont_start" not in cmd[cmd.index("-hls_flags") + 1]
    assert "append_list" not in cmd[cmd.index("-hls_flags") + 1]
    assert "setpts=PTS-STARTPTS" in cmd[cmd.index("-vf") + 1]
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert "copy" not in cmd


def test_existing_web_stream_playlist_is_resumed(tmp_path):
    (tmp_path / "index.m3u8").write_text("#EXTM3U\n")
    (tmp_path / "s00000.ts").touch()
    cmd = ffmpeg_command(
        Path("source.mp4"), start=0, seconds=30, out_dir=tmp_path, seq=1,
        segment_seconds=4, media=None, where="nas",
        profile={"width": 720, "height": 576, "max_bitrate_kbps": 3500},
        encoder="libx264",
    )
    assert "append_list" in cmd[cmd.index("-hls_flags") + 1]


def test_channel_change_replaces_viewers_least_recent_stream(tmp_path, monkeypatch):
    streams = Streams(type("Cfg", (), {"run_dir": tmp_path})())
    first = _Channel(number=1, channel_id=1, dir=tmp_path / "ch1", last_request=10,
                     viewers={"127.0.0.1#tab": 10})
    second = _Channel(number=2, channel_id=2, dir=tmp_path / "ch2", last_request=20,
                      viewers={"127.0.0.1#tab": 20})
    streams._channels = {1: first, 2: second}
    stopped = []
    monkeypatch.setattr(streams, "_stop_channel", lambda channel: stopped.append(channel.number))

    assert streams._make_room(3, "127.0.0.1#tab", 2)
    assert stopped == [1]
    assert set(streams._channels) == {2}


def test_channel_change_does_not_evict_another_viewer(tmp_path, monkeypatch):
    streams = Streams(type("Cfg", (), {"run_dir": tmp_path})())
    streams._channels = {
        1: _Channel(number=1, channel_id=1, dir=tmp_path / "ch1", viewers={"127.0.0.1#one": 10}),
        2: _Channel(number=2, channel_id=2, dir=tmp_path / "ch2", viewers={"127.0.0.1#two": 20}),
    }
    monkeypatch.setattr(streams, "_stop_channel", lambda channel: None)

    assert not streams._make_room(3, "127.0.0.1#three", 2)
    assert set(streams._channels) == {1, 2}


def test_channel_change_does_not_stop_a_stream_shared_with_another_viewer(tmp_path, monkeypatch):
    streams = Streams(type("Cfg", (), {"run_dir": tmp_path})())
    shared = _Channel(number=1, channel_id=1, dir=tmp_path / "ch1", viewers={
        "127.0.0.1#leaving": 10,
        "127.0.0.1#staying": 11,
    })
    other = _Channel(number=2, channel_id=2, dir=tmp_path / "ch2", viewers={"127.0.0.1#other": 20})
    streams._channels = {1: shared, 2: other}
    stopped = []
    monkeypatch.setattr(streams, "_stop_channel", lambda channel: stopped.append(channel.number))

    assert not streams._make_room(3, "127.0.0.1#leaving", 2)
    assert stopped == []
    assert set(streams._channels) == {1, 2}
    assert set(shared.viewers) == {"127.0.0.1#leaving", "127.0.0.1#staying"}


def test_browser_viewer_is_carried_to_segment_urls():
    playlist = "#EXTM3U\n#EXTINF:4,\n6/s00012.ts\n"
    assert _viewer_playlist(playlist, "tab-123") == (
        "#EXTM3U\n#EXTINF:4,\n6/s00012.ts?viewer=tab-123\n"
    )
    assert _viewer_playlist(playlist, "not safe!") == playlist


def test_last_browser_viewer_closes_stream_immediately(tmp_path, monkeypatch):
    streams = Streams(type("Cfg", (), {"run_dir": tmp_path})())
    channel = _Channel(number=6, channel_id=6, dir=tmp_path / "ch6",
                       viewers={"127.0.0.1#this-tab": 10})
    streams._channels = {6: channel}
    stopped = []
    monkeypatch.setattr(streams, "_stop_channel", lambda item: stopped.append(item.number))

    streams.close(6, "127.0.0.1#this-tab")

    assert stopped == [6]
    assert streams._channels == {}


def test_closing_one_browser_keeps_other_viewer_streaming(tmp_path, monkeypatch):
    streams = Streams(type("Cfg", (), {"run_dir": tmp_path})())
    channel = _Channel(number=6, channel_id=6, dir=tmp_path / "ch6",
                       viewers={"127.0.0.1#this-tab": 10, "127.0.0.1#other-tab": 11})
    streams._channels = {6: channel}
    stopped = []
    monkeypatch.setattr(streams, "_stop_channel", lambda item: stopped.append(item.number))

    streams.close(6, "127.0.0.1#this-tab")

    assert stopped == []
    assert channel.viewers == {"127.0.0.1#other-tab": 11}


class _Exited:
    """A finished ffmpeg run."""

    def __init__(self, code: int = 0) -> None:
        self.returncode = code

    def poll(self) -> int:
        return self.returncode


def test_a_finished_run_is_not_repackaged_until_its_media_has_played(tmp_path):
    """ffmpeg writes a slot faster than it plays, so it exits with the viewer still watching what
    it wrote. Packaging the next item then re-read the same slot from a few seconds earlier: the
    end of a programme came round again and a fifteen second ident arrived in pieces."""
    streams = Streams(type("Cfg", (), {"run_dir": tmp_path})())
    ch = _Channel(number=1, channel_id=1, dir=tmp_path / "ch1", proc=_Exited(), seq=0)
    ch.dir.mkdir(parents=True)
    (ch.dir / "s00000.ts").touch()          # the run wrote its segments
    ch.until = time.monotonic() + 30        # and they are half a minute of viewing
    assert streams._played_out(ch) is False
    ch.until = time.monotonic() - 0.1       # once they have been watched, the next item may start
    assert streams._played_out(ch) is True


def test_a_failed_or_empty_run_is_not_waited_for(tmp_path):
    """Waiting on media that was never written would freeze the stream, so only a run that wrote
    something and ended cleanly is given its time. A file that yields nothing is set aside so the
    card fills the slot rather than the same question being asked every second."""
    streams = Streams(type("Cfg", (), {"run_dir": tmp_path})())
    failed = _Channel(number=1, channel_id=1, dir=tmp_path / "ch1", proc=_Exited(1), seq=0)
    failed.dir.mkdir(parents=True)
    failed.until = time.monotonic() + 30
    assert streams._played_out(failed) is True

    empty = _Channel(number=2, channel_id=2, dir=tmp_path / "ch2", proc=_Exited(), seq=0, slot_id=77)
    empty.dir.mkdir(parents=True)
    empty.until = time.monotonic() + 30
    assert streams._played_out(empty) is True and empty.exhausted == 77


def test_a_run_is_never_asked_for_more_than_the_file_has_left(tmp_path):
    """The slot outlasting the file is what made the last seconds repeat. The run is cut to what
    the file holds, and once that is spent the rest of the slot is the continuity card."""
    streams = Streams(type("Cfg", (), {"run_dir": tmp_path})())
    source = tmp_path / "programme.mp4"
    source.touch()
    cache = SimpleNamespace(locate=lambda media, fallback: (str(source), "cache"))
    slot = {"id": 5, "start_ts": 1000, "end_ts": 1600, "title": "Short Film", "offset": 0}
    media = {"duration": 300.0}

    path, where, start, seconds, _ = streams._source(slot, media, cache, {}, now=1000)
    assert (path, where, start, seconds) == (source, "cache", 0.0, 300), "the file, not the slot"

    _, where, _, seconds, _ = streams._source(slot, media, cache, {}, now=1300)
    assert where == "card" and seconds == 60, "the file is spent: the card holds the slot"

    _, where, _, _, _ = streams._source(slot, media, cache, {}, now=1000, exhausted=5)
    assert where == "card", "a file that gave nothing is not asked again"


def test_a_browser_safe_source_is_copied_rather_than_re_encoded(monkeypatch, tmp_path):
    """Re-encoding every stream spends most of a core a channel: 8.5 seconds of processor for
    fifteen seconds of video against 0.2 to copy it. A stream that cannot hold real time falls
    behind its own schedule, so what a browser already accepts goes through untouched."""
    import pitv.stream as stream_mod
    monkeypatch.setattr(stream_mod, "copyable", lambda source: (True, True))
    cmd = ffmpeg_command(Path("already-h264.mp4"), start=0, seconds=60, out_dir=tmp_path, seq=0,
                         segment_seconds=4, media={"vcodec": "h264"}, where="cache",
                         profile={"width": 768, "height": 576, "max_bitrate_kbps": 3500}, encoder="libx264")
    assert cmd[cmd.index("-c:v") + 1] == "copy" and cmd[cmd.index("-c:a") + 1] == "copy"
    assert "-vf" not in cmd, "a copied stream takes no filter"

    monkeypatch.setattr(stream_mod, "copyable", lambda source: (True, False))
    cmd = ffmpeg_command(Path("h264-with-ac3.mkv"), start=0, seconds=60, out_dir=tmp_path, seq=0,
                         segment_seconds=4, media=None, where="cache",
                         profile={"width": 768, "height": 576, "max_bitrate_kbps": 3500}, encoder="libx264")
    assert cmd[cmd.index("-c:v") + 1] == "copy" and cmd[cmd.index("-c:a") + 1] == "aac", "only the sound is remade"


def test_hevc_and_interlaced_sources_are_still_re_encoded(monkeypatch, tmp_path):
    """HEVC is refused or patchy in browsers and ten bit more so, and an interlaced source needs
    deinterlacing whatever its codec. The test signal is looped and always encoded."""
    import pitv.stream as stream_mod
    monkeypatch.setattr(stream_mod, "copyable", lambda source: (False, False))
    cmd = ffmpeg_command(Path("film.mkv"), start=0, seconds=60, out_dir=tmp_path, seq=0, segment_seconds=4,
                         media={"vcodec": "hevc"}, where="cache",
                         profile={"width": 768, "height": 576, "max_bitrate_kbps": 3500}, encoder="libx264")
    assert cmd[cmd.index("-c:v") + 1] == "libx264" and "scale=768:576" in cmd[cmd.index("-vf") + 1]

    monkeypatch.setattr(stream_mod, "copyable", lambda source: (True, True))
    card = ffmpeg_command(Path("test_signal.mp4"), start=0, seconds=60, out_dir=tmp_path, seq=0,
                          segment_seconds=4, media=None, where="card", loop=True, silent_audio=True,
                          profile={"width": 768, "height": 576, "max_bitrate_kbps": 3500}, encoder="libx264")
    assert card[card.index("-c:v") + 1] == "libx264", "one frame a second would be a single long segment"


def test_what_a_browser_will_take_is_read_from_the_file(tmp_path):
    """The catalogue records neither pixel format nor field order, so the file is asked. Ten bit
    HEVC is the case that matters: it sits in the cache beside plain H.264 and a browser refuses
    it, and nothing in the database tells them apart."""
    from pitv.stream import copyable
    assert copyable(tmp_path / "not-there.mp4") == (False, False)
