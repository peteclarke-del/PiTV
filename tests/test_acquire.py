from datetime import datetime

from pitv.acquire.worker import in_hours, safe_name
from pitv.acquire.providers import is_direct_media


def test_in_hours_same_day():
    assert in_hours("01:00-07:00", datetime(2026, 1, 1, 3, 0))
    assert not in_hours("01:00-07:00", datetime(2026, 1, 1, 12, 0))


def test_in_hours_wrapping_midnight():
    assert in_hours("22:00-06:00", datetime(2026, 1, 1, 23, 30))
    assert in_hours("22:00-06:00", datetime(2026, 1, 1, 2, 0))
    assert not in_hours("22:00-06:00", datetime(2026, 1, 1, 12, 0))


def test_safe_name():
    assert safe_name("Blake's 7: The Way Back/Part 1?") == "Blake's 7 The Way BackPart 1"


def test_direct_media():
    assert is_direct_media("https://archive.org/download/x/y.mp4?foo=1")
    assert not is_direct_media("https://www.youtube.com/watch?v=abc")
