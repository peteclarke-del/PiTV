from pathlib import Path

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
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert "copy" not in cmd


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
