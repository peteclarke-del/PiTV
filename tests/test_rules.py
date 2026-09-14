from datetime import date
from zoneinfo import ZoneInfo

from pitv.db import DEFAULT_SETTINGS
from pitv.scheduler.rules import (allowed_at, broadcast_day_for, day_bounds, daypart_for,
                                  era_weight, local_ts, parse_pattern)

TZ = ZoneInfo("Europe/London")


def test_era_weight_span():
    ew = {"1980-1989": 0.85, "1990-1999": 0.15}
    assert era_weight(1985, ew) == 0.85
    assert era_weight(1995, ew) == 0.15
    assert era_weight(1978, ew) == 0.0
    assert era_weight(1978, ew, end_year=1981) == 0.85
    assert era_weight(None, ew) == 0.0
    assert era_weight(None, ew, unknown=0.2) == 0.2


def test_default_eras_allow_old_programmes_but_not_old_adverts():
    prog = DEFAULT_SETTINGS["era_weights"]
    ads = DEFAULT_SETTINGS["advert_era_weights"]
    assert era_weight(1942, prog) > 0 and era_weight(1985, prog) > 0
    assert era_weight(1975, ads) == 0.0 and era_weight(1985, ads) > 0


def test_watershed_movie_vs_tv():
    s = DEFAULT_SETTINGS
    movie12 = {"kind": "movie", "certificate": "12"}
    tv12 = {"kind": "episode", "certificate": "12"}
    tv15 = {"kind": "episode", "certificate": "15"}
    assert not allowed_at(movie12, 14 * 60, s)
    assert allowed_at(movie12, 20 * 60, s)
    assert allowed_at(tv12, 14 * 60, s)
    assert not allowed_at(tv15, 20 * 60, s)
    assert allowed_at(tv15, 21 * 60, s)


def test_unknown_movie_is_post_watershed():
    assert not allowed_at({"kind": "movie"}, 15 * 60, DEFAULT_SETTINGS)
    assert allowed_at({"kind": "movie"}, 21 * 60, DEFAULT_SETTINGS)


def test_kids_cutoff():
    kids = {"kind": "episode", "certificate": "U", "kids": True}
    assert allowed_at(kids, 16 * 60, DEFAULT_SETTINGS)
    assert not allowed_at(kids, 21 * 60 + 30, DEFAULT_SETTINGS)


def test_day_bounds_and_broadcast_day():
    start, end, nxt = day_bounds(date(2026, 9, 14), DEFAULT_SETTINGS, TZ)
    assert start == local_ts(date(2026, 9, 14), "08:00", TZ)
    assert end == local_ts(date(2026, 9, 15), "00:00", TZ)
    assert nxt == local_ts(date(2026, 9, 15), "08:00", TZ)
    assert broadcast_day_for(local_ts(date(2026, 9, 15), "03:00", TZ), DEFAULT_SETTINGS, TZ) == date(2026, 9, 14)


def test_daypart_lookup():
    dps = DEFAULT_SETTINGS["dayparts"]
    assert daypart_for(8 * 60, dps)["name"] == "Breakfast"
    assert daypart_for(16 * 60, dps)["name"] == "Children's"
    assert daypart_for(23 * 60 + 30, dps)["name"] == "Late"


def test_parse_pattern():
    assert parse_pattern("show, ad, ad") == ["show", "ad", "ad"]
    assert parse_pattern("garbage") == ["show"]
