"""The player's maintenance pass: what it keeps pitv_content told."""

from pathlib import Path

from pitv.db import DEFAULT_SETTINGS
from pitv.player import maintenance
from pitv.player.maintenance import Maintenance


def test_the_screen_is_pushed_after_a_start_and_again_only_when_it_changes(monkeypatch):
    """A catalogue run has no manifest, so pitv_content encodes to the screen it was last told.
    It is told after every start, since a release may have changed the profile table, retried
    while it cannot be reached, and otherwise left alone."""
    answers = ["HTTP 503: offline", None, None]
    pushed: list[str] = []

    def push(settings):
        pushed.append(settings["display_profile"])
        return answers.pop(0)

    monkeypatch.setattr(maintenance, "push_screen", push)
    m = Maintenance(Path("unused.db"), lambda: 0, lambda: None, cache=None)
    crt = dict(DEFAULT_SETTINGS, display_profile="crt_pal")
    m._keep_content_screen(crt)      # pitv_content is down
    m._keep_content_screen(crt)      # and is tried again
    m._keep_content_screen(crt)      # taken: nothing more to say
    m._keep_content_screen(dict(crt, display_profile="lcd_1080p"))
    assert pushed == ["crt_pal", "crt_pal", "lcd_1080p"]
