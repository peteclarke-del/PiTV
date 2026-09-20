"""The snow shown over a channel change."""

import threading
import time
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace

from pitv.player import controller as ctl
from pitv.player.osd import OVERLAY_STATIC, Renderer


def test_the_snow_is_several_different_frames_in_one_file(tmp_path):
    """A detuned set is never a still picture. The renderer keeps several frames, as many as its
    memory budget allows at the screen size and never fewer than three, in one file the player
    steps through by offset."""
    (path, w, h, x, y), frames, frame_bytes = Renderer(tmp_path, UTC, 768, 576).static()
    data = Path(path).read_bytes()
    assert (w, h, x, y) == (768, 576, 0, 0) and frames == 8 and frame_bytes == 768 * 576 * 4
    assert len(data) == frames * frame_bytes
    assert len({data[n * frame_bytes:(n + 1) * frame_bytes] for n in range(frames)}) == frames, "two frames were the same"
    (tmp_path / "hd").mkdir()
    _, big, _ = Renderer(tmp_path / "hd", UTC, 1920, 1080).static()
    assert 3 <= big < 8, "a large screen keeps fewer frames, inside the budget"


def test_a_channel_change_steps_through_the_frames_and_then_clears(tmp_path, monkeypatch):
    """The burst animates at the television's frame rate for its short life and takes the overlay
    away; a second change before the first has finished leaves the clearing to the newer burst."""
    calls: list[tuple] = []
    lock = threading.Lock()

    class Mpv:
        def overlay_add(self, oid, x, y, path, w, h, offset=0):
            with lock:
                calls.append(("add", oid, offset))

        def overlay_remove(self, oid):
            with lock:
                calls.append(("remove", oid))
    monkeypatch.setattr(ctl, "STATIC_SECONDS", 0.2)
    monkeypatch.setattr(ctl, "STATIC_FRAME_SECONDS", 0.02)
    player = SimpleNamespace(renderer=Renderer(tmp_path, UTC, 320, 240), mpv=Mpv(), osd_expiry={}, _static_burst=0, stopping=False)
    ctl.Player._play_static(player)
    time.sleep(0.05)
    ctl.Player._play_static(player)          # a second channel change, mid-burst
    time.sleep(0.5)
    adds = [c for c in calls if c[0] == "add"]
    assert len({c[2] for c in adds}) >= 3, "the overlay never moved on from its first frame"
    assert all(c[1] == OVERLAY_STATIC for c in calls)
    assert [c[0] for c in calls].count("remove") == 1 and calls[-1][0] == "remove", "only the latest burst clears the screen"
