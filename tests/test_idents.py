"""The channel idents PiTV makes from a channel's name and colour."""

import json
import shutil
import subprocess
import wave

import pytest

from pitv import idents

CHANNEL = {"number": 3, "name": "PiTV Three", "short_name": "Three", "colour": "#f4a261"}


def test_the_picture_runs_fifteen_seconds_up_from_black_and_back():
    frames = list(idents.frames(CHANNEL, 160, 120))
    assert len(frames) == idents.SECONDS * idents.FPS and {f.size for f in frames} == {(160, 120)}
    brightness = [sum(f.convert("L").resize((8, 6)).tobytes()) for f in (frames[0], frames[200], frames[-1])]
    assert brightness[0] < brightness[1] / 4 and brightness[2] < brightness[1] / 2, "it should open and close on black"
    assert frames[100].tobytes() != frames[300].tobytes(), "nothing moved"


def test_each_channel_has_its_own_key(tmp_path):
    idents.sting(tmp_path / "one.wav", 1)
    idents.sting(tmp_path / "two.wav", 2)
    with wave.open(str(tmp_path / "one.wav")) as w:
        assert (w.getnchannels(), w.getframerate(), w.getnframes()) == (2, idents.SAMPLE_RATE, idents.SECONDS * idents.SAMPLE_RATE)
    assert (tmp_path / "one.wav").read_bytes() != (tmp_path / "two.wav").read_bytes()


def test_a_voiceover_is_the_owners_recording_or_nothing(tmp_path):
    """PiTV never invents a voice. It uses a recording named for the channel's number or its short
    name, and without one the ident carries its music alone."""
    assert idents.voice_for(CHANNEL, None) is None and idents.voice_for(CHANNEL, tmp_path) is None
    (tmp_path / "three.mp3").write_bytes(b"x")
    assert idents.voice_for(CHANNEL, tmp_path) == tmp_path / "three.mp3"
    (tmp_path / "3.wav").write_bytes(b"x")
    assert idents.voice_for(CHANNEL, tmp_path) == tmp_path / "3.wav", "the number is looked for first"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_the_film_lands_where_pitv_content_reads_its_channel_from(tmp_path):
    """`ch<number>/` is the folder layout pitv_content's ident scan takes the channel from, and a
    half-written film never sits under the final name."""
    voices = tmp_path / "voices"
    voices.mkdir()
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=1",
                    str(voices / "3.wav")], check=True)
    made = idents.make(CHANNEL, tmp_path / "out", 160, 120, voices)
    assert made == tmp_path / "out" / "ch3" / "PiTV Three ident.mp4" and not list(made.parent.glob(".*"))
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name",
                                       "-of", "json", str(made)], check=True, capture_output=True, text=True).stdout)
    assert abs(float(probe["format"]["duration"]) - idents.SECONDS) < 0.2
    assert {(s["codec_type"], s["codec_name"]) for s in probe["streams"]} == {("video", "h264"), ("audio", "aac")}
