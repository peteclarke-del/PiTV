"""Whole-week scheduler properties on the fake library."""
import json
import math
import os
import random
from dataclasses import replace
from datetime import date, datetime, timedelta
from itertools import pairwise

import pytest
from conftest import make_library

from pitv import db as dbm
from pitv.lineup import nas_only_for
from pitv.scheduler import bands
from pitv.scheduler.build import Builder
from pitv.scheduler.horizon import build_horizon, fresh_rebuild_horizon, rebuild_from
from pitv.scheduler.rules import (
    allowed_at,
    day_bounds,
    effective_cert,
    local_ts,
    minutes_of_day,
    tz_of,
)
from pitv.scheduler.slots import Show, Slot, parse_day, slot_titles


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    c = make_library(tmp_path_factory.mktemp("sched"), max_episodes=30)["conn"]
    now = local_ts(parse_day("2026-09-14"), "07:00", tz_of(c))
    r = build_horizon(c, start_day=parse_day("2026-09-14"), days=7, now=now, seed=42)
    assert r["status"] in ("ok", "warning"), r
    return c


def _programmes(conn):
    return conn.execute("SELECT s.*, m.kind AS mkind, m.certificate, m.year, m.show_id, m.season, m.episode, m.genres, m.duration"
                        " FROM schedule s JOIN media m ON m.id = s.media_id WHERE s.replay = 0 AND s.kind = 'programme'"
                        " ORDER BY s.channel_id, s.start_ts").fetchall()


def test_daily_show_limit_holds_until_the_rules_relax(tmp_path):
    """A series airs at most its daily limit while anything else fits; when nothing does, a
    further airing beats a holding card (docs/PLAN.md section 4.5), with the next episode."""
    c = make_library(tmp_path / "daily-cap", max_episodes=3)["conn"]
    now = local_ts(parse_day("2026-09-14"), "21:00", tz_of(c))     # its one series is post-watershed
    with dbm.tx(c):
        c.execute("UPDATE channels SET enabled=0")
        channel_id = c.execute("SELECT id FROM channels WHERE number=1").fetchone()[0]
        c.execute("UPDATE channels SET enabled=1,kind_weights='{\"tv\":1,\"movie\":0}' WHERE id=?",
                  (channel_id,))
        keep = c.execute("SELECT id FROM shows WHERE home_channel_id=? LIMIT 1", (channel_id,)).fetchone()[0]
        c.execute("UPDATE shows SET excluded=1 WHERE id!=?", (keep,))
    builder = Builder(c, now=now)
    channel = next(ch for ch in builder.channels if ch["id"] == channel_id)
    show = builder.library.free_shows[channel_id][0]
    gap = 2 * 3600
    assert builder.select.programme(channel, random.Random(1), now, gap, "show", {}, None, set()) is not None
    capped = {show.id: builder.settings["show_daily_limit"]}
    assert builder.select.programme(channel, random.Random(1), now, gap, "show", capped, None, set()) is None
    choice = builder.select.programme(channel, random.Random(1), now, gap, "show", capped, None, set(), relax=1)
    assert choice is not None and choice[1] is show and choice[0] == show.next_episode()
    c.close()


def test_a_bands_feature_follows_the_indexes_classification_for_any_kind():
    """Where the index says which items of a kind are features (it flags concerts among music
    videos), a band that opens with one takes only those: length alone would let a compilation
    pass for a concert. A kind the index does not classify goes by length. The rule is the same
    for every kind and is read from the library, not from a list of kinds in the code."""
    band = bands.Band(1, 5, "Double Bill", "20:30", None, (), ("music", "movie"), (), (), True)
    long_clip = {"id": 1, "kind": "music", "duration": 30 * 60, "concert": 0, "genres": [], "year": 1985}
    concert = {"id": 2, "kind": "music", "duration": 60 * 60, "concert": 1, "genres": [], "year": 1985}
    film = {"id": 3, "kind": "movie", "duration": 95 * 60, "concert": 0, "genres": [], "year": 1985}

    def picks(pool):
        filler = bands.Filler([band], pool, item_repeat=0, feature_repeat=0, rng=random.Random(1), last_placed={})
        out = set()
        for at in range(len(pool)):
            item = filler.pick(band, at, 3 * 3600, feature=True, exclude=out)
            if item is None:
                break
            out.add(item["id"])
        return out

    assert picks([long_clip, concert, film]) == {2, 3}, "the concert and the film, never the compilation"
    assert picks([long_clip, film]) == {1, 3}, "with no music classified, music goes by length like any kind"


def test_a_band_takes_its_own_genre_before_an_untagged_item_and_another_genre_last():
    """The order a band widens its search in: its genres, then an item the index gives no genre
    (it might be disco), and a genre it did not ask for only when there is nothing else."""
    band = bands.Band(1, 5, "Disco Lunch", "12:00", None, (), ("music",), ("Disco",), (1970,), False)
    disco = {"id": 1, "duration": 240, "concert": 0, "genres": ["Disco"], "year": 1978}
    untagged = {"id": 2, "duration": 240, "concert": 0, "genres": [], "year": 1979}
    metal = {"id": 3, "duration": 240, "concert": 0, "genres": ["Metal"], "year": 1976}
    eighties = {"id": 4, "duration": 240, "concert": 0, "genres": ["Disco"], "year": 1984}
    filler = bands.Filler([band], [metal, untagged, eighties, disco], item_repeat=36 * 3600, feature_repeat=0,
                          rng=random.Random(1), last_placed={})
    order = []
    for at in (0, 300, 600):
        item = filler.pick(band, at, 3600, feature=False)
        filler.note(item, at)
        order.append(item["id"])
    assert order == [1, 2, 3], "its genre, then the untagged item, then another genre"
    assert 4 not in order, "the decades are never given up"
    strict = bands.Filler([band], [metal, untagged, disco], item_repeat=36 * 3600, feature_repeat=0,
                          rng=random.Random(1), last_placed={}, strict=True)
    strict.note(strict.pick(band, 0, 3600, feature=False), 0)
    assert strict.pick(band, 300, 3600, feature=False) is disco, "a strict band repeats its own rather than widen"


def test_the_timetable_is_the_one_the_scheduler_places_by():
    """A band without a length runs to the next band or to closedown; one with a length keeps
    it past midnight. The top-up measures a band by this same timetable, so what it asks for is
    what the band will actually play."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/London")
    morning = bands.Band(1, 5, "Morning", "08:00", None, (), ("music",), (), (), False)
    lunch = bands.Band(2, 5, "Lunch", "10:00", None, (), ("music",), (), (), False)
    late = bands.Band(3, 5, "Late", "23:30", 120, (), ("music",), (), (), False)
    day = parse_day("2026-09-14")
    day_start, day_end, next_start = day_bounds(day, dbm.DEFAULT_SETTINGS, tz)
    placed = {b.name: (start, end) for start, end, b in
              bands.timetable([morning, lunch, late], day, day_start, day_end, next_start, 480, tz)}
    minutes = {name: (end - start) // 60 for name, (start, end) in placed.items()}
    assert minutes == {"Morning": 120, "Lunch": 13 * 60 + 30, "Late": 120}
    assert placed["Late"][1] > day_end, "a band that starts before closedown keeps its length"


def test_fresh_rebuild_discards_derived_state_but_keeps_inputs(tmp_path):
    c = make_library(tmp_path / "fresh", max_episodes=6)["conn"]
    day = parse_day("2026-09-14")
    now = local_ts(day, "13:00", tz_of(c))
    build_horizon(c, start_day=day, days=1, now=now, seed=3)
    lineup_id = c.execute("SELECT id FROM lineup ORDER BY id LIMIT 1").fetchone()[0]
    with dbm.tx(c):
        c.execute("UPDATE schedule SET locked = 1")
        c.execute("UPDATE band SET last_fetch_at = ?", (now,))
        c.execute("INSERT INTO history(channel_id, media_id, schedule_id, started_at, title)"
                  " SELECT channel_id, media_id, id, start_ts, title FROM schedule LIMIT 1")
        old = c.execute("INSERT INTO wanted(kind,title,lineup_id,transient,status,created_at)"
                        " VALUES ('episode','Old placeholder',?,1,'queued',?)", (lineup_id, now)).lastrowid
        done = c.execute("INSERT INTO wanted(kind,title,lineup_id,transient,status,created_at)"
                         " VALUES ('episode','Delivered placeholder',?,1,'done',?)", (lineup_id, now)).lastrowid
        manual = c.execute("INSERT INTO wanted(kind,title,status,created_at)"
                           " VALUES ('movie','Keep me','queued',?)", (now,)).lastrowid
    inputs = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("sources", "channels", "band", "shows", "media", "lineup")}

    result = fresh_rebuild_horizon(c, start_day=day, days=2, now=now, seed=4)

    assert result["built"] == 12 and result["cleared_slots"] > 0 and result["cleared_history"] == 1
    assert c.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM schedule WHERE locked = 1").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(DISTINCT day || ':' || channel_id) FROM schedule").fetchone()[0] == 12
    # Requests the scheduler raised for itself go with the schedule they served; a request a
    # person made is an input and stays.
    assert result["cleared_wanted"] == 2 and result["kept_wanted"] == 1
    assert not c.execute("SELECT 1 FROM wanted WHERE id IN (?, ?)", (old, done)).fetchone()
    assert c.execute("SELECT status FROM wanted WHERE id = ?", (manual,)).fetchone()[0] == "queued"
    assert not c.execute("SELECT 1 FROM show_cursor").fetchone()
    assert not c.execute("SELECT 1 FROM band WHERE last_fetch_at IS NOT NULL").fetchone()
    assert c.execute("SELECT COUNT(*) FROM run_log").fetchone()[0] <= 1   # only this build's own row
    assert inputs == {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("sources", "channels", "band", "shows", "media", "lineup")}
    c.close()


def test_every_channel_day_is_covered(conn):
    settings = dbm.all_settings(conn)
    tz = tz_of(conn)
    for ch in conn.execute("SELECT id FROM channels"):
        for i in range(7):
            day = parse_day("2026-09-14") + timedelta(days=i)
            ds, _, nds = day_bounds(day, settings, tz)
            rows = conn.execute("SELECT start_ts, end_ts, replay FROM schedule WHERE channel_id = ? AND day = ? ORDER BY start_ts",
                                (ch["id"], day.isoformat())).fetchall()
            assert rows, f"no slots for channel {ch['id']} {day}"
            assert rows[0]["start_ts"] == ds
            # contiguous, no overlaps
            for a, b in pairwise(rows):
                assert a["end_ts"] == b["start_ts"], (a, b)
            assert rows[-1]["end_ts"] == nds, "overnight replay should run up to the next day start"


def test_no_slot_crosses_into_next_day_start(conn):
    rows = conn.execute("SELECT a.id FROM schedule a JOIN schedule b ON a.channel_id = b.channel_id AND a.id != b.id"
                        " AND a.start_ts < b.end_ts AND b.start_ts < a.end_ts").fetchall()
    assert not rows


def test_watershed_respected(conn):
    settings = dbm.all_settings(conn)
    tz = tz_of(conn)
    for r in _programmes(conn):
        item = {"kind": r["mkind"], "certificate": r["certificate"], "genres": r["genres"]}
        assert allowed_at(item, minutes_of_day(r["start_ts"], tz), settings), \
            f"{r['title']} ({effective_cert(item, settings)}) at {r['start_ts']}"


def test_episodes_in_order_per_show(conn):
    rows = _programmes(conn)
    # A series two channels share has one episode position between them, so neither sees every
    # episode and a wrap can land anywhere; the borrowing test covers those. The rest are strict.
    shared = {r["show_id"] for r in rows if r["show_id"]
              and len({x["channel_id"] for x in rows if x["show_id"] == r["show_id"]}) > 1}
    by_show = {}
    for r in rows:
        if r["show_id"] and r["show_id"] not in shared:
            by_show.setdefault(r["show_id"], []).append(r)
    checked = 0
    for sid, eps in by_show.items():
        eps.sort(key=lambda r: r["start_ts"])
        keys = [(r["season"], r["episode"]) for r in eps]
        first = min(keys)      # a series numbered by year starts at (1985, 1), not (1, 1)
        # strictly increasing until a wrap back to the first episode
        for a, b in pairwise(keys):
            assert b > a or b == first, f"show {sid}: {a} then {b}"
            checked += 1
    assert checked > 50


def test_show_stays_on_home_channel(conn):
    """A series airs on its own channel, or on one that borrows its type ("also carries")."""
    from pitv.genres import programme_type
    rows = conn.execute("SELECT DISTINCT s.channel_id, m.show_id, sh.home_channel_id, sh.genres, sh.category, c.also_carries"
                        " FROM schedule s JOIN media m ON m.id = s.media_id JOIN shows sh ON sh.id = m.show_id"
                        " JOIN channels c ON c.id = s.channel_id WHERE s.replay = 0").fetchall()
    assert rows
    for r in rows:
        borrowed = programme_type("show", json.loads(r["genres"] or "[]"), r["category"]) in json.loads(r["also_carries"] or "[]")
        assert r["channel_id"] == r["home_channel_id"] or borrowed, dict(r)


def test_movies_spread_evenly(conn):
    rows = conn.execute("SELECT media_id, COUNT(*) AS n FROM schedule s JOIN media m ON m.id = s.media_id"
                        " WHERE s.replay = 0 AND m.kind = 'movie' GROUP BY media_id").fetchall()
    slots = sum(r["n"] for r in rows)
    movies = conn.execute("SELECT COUNT(*) FROM media WHERE kind = 'movie' AND year IS NOT NULL").fetchone()[0]
    # The fake library is far too small for a week: a channel has eight films and under ten
    # series, and a series airs once a week, so films carry much of its day and repeats are
    # unavoidable. They must be spread evenly (least recently aired first) rather than piling
    # onto a few titles. Sub-pools are smaller still (three PG films a channel may show by day,
    # three 18-rated ones for late slots), which is what the allowance is for.
    assert max(r["n"] for r in rows) <= math.ceil(slots / movies) + 5, (slots, movies, [r["n"] for r in rows])


def test_ads_only_on_ad_channels(conn):
    rows = conn.execute("SELECT c.number, c.ads_enabled, COUNT(*) AS n FROM schedule s JOIN channels c ON c.id = s.channel_id"
                        " WHERE s.kind = 'advert' GROUP BY c.number").fetchall()
    for r in rows:
        assert r["ads_enabled"] == 1 and r["n"] > 0


def test_rebuild_from_keeps_past_and_locked(conn):
    tz = tz_of(conn)
    ch = conn.execute("SELECT id FROM channels WHERE number = 1").fetchone()["id"]
    now = local_ts(parse_day("2026-09-16"), "12:00", tz)
    before = conn.execute("SELECT id, title FROM schedule WHERE channel_id = ? AND day = '2026-09-16' AND replay = 0 ORDER BY start_ts", (ch,)).fetchall()
    evening = conn.execute("SELECT id FROM schedule WHERE channel_id = ? AND day = '2026-09-16' AND replay = 0 AND start_ts > ? AND kind = 'programme' ORDER BY start_ts LIMIT 1 OFFSET 3", (ch, now)).fetchone()
    with dbm.tx(conn):
        conn.execute("UPDATE schedule SET locked = 1 WHERE id = ?", (evening["id"],))
    r = rebuild_from(conn, ch, now, now=now, seed=99)
    assert r["status"] in ("ok", "warning")
    after = conn.execute("SELECT id, title, start_ts FROM schedule WHERE channel_id = ? AND day = '2026-09-16' AND replay = 0 ORDER BY start_ts", (ch,)).fetchall()
    ids_after = {r["id"] for r in after}
    for b in before:
        row = conn.execute("SELECT start_ts FROM schedule WHERE id = ?", (b["id"],)).fetchone()
        if row is None:
            continue
        if row["start_ts"] < now:
            assert b["id"] in ids_after, "slots before the cut must survive"
    assert evening["id"] in ids_after, "locked slot must survive a rebuild"
    starts = [r["start_ts"] for r in after]
    assert starts == sorted(starts)


def test_no_same_show_back_to_back(conn):
    """Consecutive programmes on a channel (ads in between are fine, overnight replays count)
    are never episodes of the same series."""
    tz = tz_of(conn)
    rows = conn.execute("SELECT s.channel_id, s.start_ts, s.replay, s.block, m.show_id, sh.category FROM schedule s"
                        " JOIN media m ON m.id = s.media_id"
                        " LEFT JOIN shows sh ON sh.id = m.show_id WHERE s.kind = 'programme' ORDER BY s.channel_id, s.start_ts").fetchall()
    for a, b in pairwise(rows):
        if a["channel_id"] == b["channel_id"] and a["show_id"] is not None and not (a["replay"] and b["replay"]):
            if a["category"] == "sport" and datetime.fromtimestamp(b["start_ts"], tz).weekday() >= 5:
                continue  # sport may run back to back at weekends
            if a["block"] and a["block"] == b["block"]:
                continue  # a run of short episodes is one programme in the guide, not two
            assert a["show_id"] != b["show_id"], f"same show back to back at {b['start_ts']} (replay={b['replay']})"


def test_subtitle_is_episode_title(conn):
    row = conn.execute("SELECT s.subtitle FROM schedule s JOIN media m ON m.id = s.media_id"
                       " WHERE m.kind = 'episode' LIMIT 1").fetchone()
    assert row["subtitle"] and not row["subtitle"].startswith("S0")


def test_healthy_mix_of_eras_and_adverts_only_80s_90s(conn):
    years = [r["year"] for r in conn.execute("SELECT m.year FROM schedule s JOIN media m ON m.id = s.media_id"
                                             " WHERE s.replay = 0 AND s.kind = 'programme' AND m.year IS NOT NULL")]
    # Episodes carry the year they aired (later seasons of a 1978 series are 1980s material), so
    # the pre-1980 share sits below the series count; a fifth of the airtime is a healthy mix.
    pre = sum(1 for y in years if y < 1980)
    assert 0.15 < pre / len(years) < 0.7, f"pre-1980 share {pre / len(years):.2f}"
    ad_years = [r["year"] for r in conn.execute("SELECT m.year FROM schedule s JOIN media m ON m.id = s.media_id WHERE s.kind = 'advert'")]
    assert ad_years and all(1980 <= y <= 1999 for y in ad_years)


def test_weekend_afternoons_carry_sport(conn):
    """On the channels whose line-ups carry sport, Saturday and Sunday afternoons should be
    largely sport and weekday daytime should not."""
    tz = tz_of(conn)
    sport_channels = [r[0] for r in conn.execute(
        "SELECT DISTINCT l.channel_id FROM lineup l JOIN shows sh ON sh.id = l.show_id WHERE sh.category = 'sport'")]
    assert sport_channels, "no channel's line-up carries sport"

    def share(days, hours, channels=None):
        channels = channels or sport_channels
        rows = conn.execute("SELECT s.start_ts, s.end_ts, sh.category FROM schedule s JOIN media m ON m.id = s.media_id"
                            " LEFT JOIN shows sh ON sh.id = m.show_id WHERE s.replay = 0 AND s.kind = 'programme'"
                            f" AND s.day IN ({','.join('?' * len(days))})"
                            f" AND s.channel_id IN ({','.join('?' * len(channels))})", (*days, *channels)).fetchall()
        rows = [r for r in rows if hours[0] <= datetime.fromtimestamp(r["start_ts"], tz).hour < hours[1]]
        total = sum(r["end_ts"] - r["start_ts"] for r in rows) or 1
        return sum(r["end_ts"] - r["start_ts"] for r in rows if r["category"] == "sport") / total

    assert share(["2026-09-19", "2026-09-20"], (12, 17)) > 0.3
    # Weekday daytime is quiet wherever the channel's own week says so. The channel modelled on
    # BBC Two welcomes sport on weekday afternoons and evenings, as that channel did, and is
    # judged by its own profile rather than by the others'.
    def weekday_sport(channel_id):
        profile = json.loads(conn.execute("SELECT daypart_profile FROM channels WHERE id = ?", (channel_id,)).fetchone()[0] or "{}")
        rows = (profile.get("weekday") if isinstance(profile, dict) else None) or dbm.all_settings(conn)["dayparts"]
        return max(r.get("sport", 0) for r in rows if r["start"] < "22:00")
    quiet = [ch for ch in sport_channels if weekday_sport(ch) < 0.5]
    busy = [ch for ch in sport_channels if ch not in quiet]
    if quiet:
        assert share(["2026-09-15", "2026-09-16"], (8, 22), quiet) < 0.2
    if quiet and busy:
        assert share(["2026-09-15", "2026-09-16"], (8, 22), busy) > share(["2026-09-15", "2026-09-16"], (8, 22), quiet)


def _channel(conn, number):
    return conn.execute("SELECT * FROM channels WHERE number = ?", (number,)).fetchone()


def test_cartoons_routed_to_cartoon_channel(conn):
    toons = _channel(conn, 6)
    assert toons["content"] == "cartoons"
    assert 0 < json.loads(toons["kind_weights"])["movie"] < 0.5, "animated films are cartoons and air here, series first"
    rows = conn.execute("SELECT title, category, kids, genres FROM shows WHERE home_channel_id = ? ORDER BY title",
                        (toons["id"],)).fetchall()
    titles = {r["title"] for r in rows}
    assert {"Danger Mouse", "Bananaman", "Thundercats", "Count Duckula"} <= titles
    # The channel allows Children as well as Animation, so a live-action children's series belongs
    # here too; what must never land is something that is not for children at all.
    assert all(r["kids"] for r in rows)
    # and no cartoon series on the general channels
    stray = conn.execute("SELECT s.title FROM shows s JOIN channels c ON c.id = s.home_channel_id"
                         " WHERE c.content = 'general' AND (s.genres LIKE '%Animation%' OR s.genres LIKE '%Anime%')").fetchall()
    assert not stray
    # cartoons run all evening on their own channel (kids cutoff does not apply there)
    tz = tz_of(conn)
    starts = conn.execute("SELECT start_ts FROM schedule WHERE channel_id = ? AND kind = 'programme' AND replay = 0",
                          (toons["id"],)).fetchall()
    assert any(datetime.fromtimestamp(r["start_ts"], tz).hour >= 21 for r in starts)
    films = conn.execute("SELECT COUNT(*) FROM schedule s JOIN media m ON m.id = s.media_id"
                         " WHERE s.channel_id = ? AND s.replay = 0 AND m.kind = 'movie'", (toons["id"],)).fetchone()[0]
    assert films == 0


def test_music_channel_day(conn):
    """The configuration is the authority. Every band holds the stretch it is set to, under its
    own name: what the library has for it plays, nothing twice in one airing, and whatever is
    left is the band's own holding card, never other material under other names."""
    music = _channel(conn, 5)
    assert music["content"] == "music"
    rows = conn.execute("SELECT s.*, m.concert, m.kind AS mkind FROM schedule s LEFT JOIN media m ON m.id = s.media_id"
                        " WHERE s.channel_id = ? AND s.day = '2026-09-16' AND s.replay = 0 ORDER BY s.start_ts",
                        (music["id"],)).fetchall()
    assert rows and all(r["mkind"] == "music" for r in rows if r["media_id"])
    tz = tz_of(conn)
    day = parse_day("2026-09-16")
    configured = conn.execute("SELECT name, start, fill FROM band WHERE channel_id = ? ORDER BY start",
                              (music["id"],)).fetchall()
    nominal = [(local_ts(day, b["start"], tz), b) for b in configured]
    tolerance = 5 * 60

    def band_at(ts):
        return nominal[max(i for i, (at, _) in enumerate(nominal) if at <= ts)][1]

    # The fixture's bands cover the day, so every slot belongs to one: the band whose stretch
    # it starts in, or the one before it when that band's last item ran over by a few minutes.
    airings: dict[int, list] = {}
    for r in rows:
        assert r["block"], f"{r['title']} at {r['start_ts']} is in no band"
        index = max(i for i, (at, _) in enumerate(nominal) if at <= r["start_ts"])
        if nominal[index][1]["name"] != r["block"]:
            index -= 1
            assert r["start_ts"] - nominal[index + 1][0] <= tolerance and r["kind"] == "programme"
        assert nominal[index][1]["name"] == r["block"]
        airings.setdefault(index, []).append(r)
    assert set(airings) == set(range(len(nominal))), "every configured band appears in the day"
    for index, slots in airings.items():
        band, starts = nominal[index][1], nominal[index][0]
        ends = nominal[index + 1][0] if index + 1 < len(nominal) else slots[-1]["end_ts"]
        assert 0 <= slots[0]["start_ts"] - starts <= tolerance, f"{band['name']} starts when it is set to"
        assert -60 <= slots[-1]["end_ts"] - ends <= tolerance, f"{band['name']} holds its whole stretch"
        played = [r["media_id"] for r in slots if r["kind"] == "programme"]
        assert len(played) == len(set(played)), f"{band['name']} repeated itself within one airing"
        cards = [r for r in slots if r["kind"] == "filler"]
        assert len(cards) <= 1 and (not cards or cards[0] is slots[-1]), "the card closes the band"
        if cards:
            assert cards[0]["title"] == band["name"] and "resumes at" in cards[0]["subtitle"]
        fill = json.loads(band["fill"])
        if fill.get("feature"):
            assert len(played) == 1 and slots[0]["concert"], "a concert band is one concert"
        decades = fill.get("decades") or []
        if decades and not fill.get("feature"):
            years = " OR ".join(f"year BETWEEN {d} AND {d + 9}" for d in decades)
            pool = conn.execute(f"SELECT COUNT(*) FROM media WHERE kind = 'music' AND concert = 0 AND ({years})").fetchone()[0]
            assert bool(played) == bool(pool), f"{band['name']} plays exactly when the library has its decades"
    # Contiguous from 08:00 to closedown, every slot counted.
    for a, b in pairwise(rows):
        assert a["end_ts"] == b["start_ts"]


def test_guide_collapses_music_blocks(conn):
    from pitv.guide import block_entry, collapse_blocks, next_programmes, slot_at
    music = _channel(conn, 5)
    rows = conn.execute("SELECT * FROM schedule WHERE channel_id = ? AND day = '2026-09-16' AND replay = 0 ORDER BY start_ts", (music["id"],)).fetchall()
    merged = collapse_blocks([dict(r) for r in rows])
    # Every stretch a band filled is one entry; time a band gave back to the channel is billed
    # item by item, as it is on any other channel.
    stretches = sum(1 for i, r in enumerate(rows) if r["block"] and (i == 0 or rows[i - 1]["block"] != r["block"]))
    assert stretches >= 5
    assert sum(1 for e in merged if e.get("block")) == stretches
    assert sum(1 for e in merged if not e.get("block")) == sum(1 for r in rows if not r["block"])
    assert merged[0]["title"] == "Seventies Breakfast" and merged[0]["items"] > 1
    # the shared lookups (player OSD and web) agree with the raw merge
    ts = rows[3]["start_ts"] + 10
    slot = slot_at(conn, music["id"], ts)
    assert slot["id"] == rows[3]["id"]
    entry = block_entry(conn, slot, ts)
    assert entry["title"] == "Seventies Breakfast" and entry["video_title"] == slot["title"] and entry["video_id"] == slot["id"]
    assert entry["start_ts"] == merged[0]["start_ts"] and entry["end_ts"] == merged[0]["end_ts"]
    # What follows is the next band, past the caption that closes a band whose last item did
    # not fit; captions are not programmes.
    following = next(e for e in merged[1:] if e["kind"] == "programme")
    nxt = next_programmes(conn, music["id"], entry["end_ts"], 2)
    assert len(nxt) == 2 and nxt[0]["start_ts"] == following["start_ts"] and nxt[0]["title"] == following["title"]


def test_slot_titles():
    assert slot_titles({"kind": "episode", "title": "The Beach", "episode": 5}, "Minder") == ("Minder", "The Beach")
    assert slot_titles({"kind": "episode", "title": "", "episode": 5}, "Minder") == ("Minder", "Episode 5")
    assert slot_titles({"kind": "movie", "title": "Brazil", "year": 1985, "certificate": "15"}) == ("Brazil", "(1985) 15")
    assert slot_titles({"kind": "movie", "title": "Mystery Movie"}) == ("Mystery Movie", "")
    assert slot_titles({"kind": "music", "title": "Queen - Radio Ga Ga", "year": 1984, "genres": '["Pop", "Rock"]'}) == \
        ("Queen - Radio Ga Ga", "(1984) Pop, Rock")
    assert slot_titles({"kind": "music", "title": "X", "genres": ["Pop"]}) == ("X", "Pop")


def test_family_safe_adverts_on_cartoon_channel(conn):
    toons = _channel(conn, 6)
    assert toons["family_safe_ads"] == 1
    flagged = {r["title"] for r in conn.execute("SELECT title FROM media WHERE kind = 'advert' AND family_safe = 0")}
    assert {"Hofmeister", "Hamlet Cigars", "Cinzano", "Guinness Surfer"} <= flagged
    assert "Milk Tray" not in flagged
    aired = conn.execute("SELECT DISTINCT m.title FROM schedule s JOIN media m ON m.id = s.media_id"
                         " WHERE s.channel_id = ? AND s.kind = 'advert'", (toons["id"],)).fetchall()
    assert aired and not ({r["title"] for r in aired} & flagged)
    # while a commercial general channel still uses them
    general = conn.execute("SELECT COUNT(*) FROM schedule s JOIN media m ON m.id = s.media_id JOIN channels c ON c.id = s.channel_id"
                           " WHERE c.content = 'general' AND s.kind = 'advert' AND m.family_safe = 0").fetchone()[0]
    assert general > 0


def test_music_channel_decades(conn):
    music = _channel(conn, 5)
    years = [r["year"] for r in conn.execute("SELECT m.year FROM schedule s JOIN media m ON m.id = s.media_id"
                                             " WHERE s.channel_id = ? AND m.kind = 'music' AND m.year IS NOT NULL", (music["id"],))]
    assert years and all(1970 <= y <= 2009 for y in years)


def test_readiness_substitutes_missing_file(conn, tmp_path):
    """Delete one scheduled file: the check reports it, replaces it and rebalances the day."""
    from pitv.readiness import check
    now = local_ts(parse_day("2026-09-16"), "06:00", tz_of(conn))
    row = conn.execute("SELECT s.id AS slot_id, s.channel_id, s.start_ts, m.id AS media_id, m.path FROM schedule s JOIN media m ON m.id = s.media_id"
                       " JOIN channels c ON c.id = s.channel_id WHERE c.number = 1 AND s.day = '2026-09-16' AND s.kind = 'programme' AND s.replay = 0"
                       " AND s.start_ts > ? ORDER BY s.start_ts LIMIT 1 OFFSET 4", (now,)).fetchone()
    backup = tmp_path / "gone.mp4"
    os.rename(row["path"], backup)
    try:
        r = check(conn, now=now, days=1)
        assert r["status"] == "error" and r["missing"] >= 1 and r["substituted"] >= 1
        assert not conn.execute("SELECT 1 FROM schedule WHERE media_id = ? AND start_ts >= ? AND replay = 0", (row["media_id"], now)).fetchone()
        # the day is still contiguous after rebalancing
        rows = conn.execute("SELECT start_ts, end_ts FROM schedule WHERE channel_id = ? AND day = '2026-09-16' ORDER BY start_ts", (row["channel_id"],)).fetchall()
        for a, b in pairwise(rows):
            assert a["end_ts"] == b["start_ts"]
        log_row = conn.execute("SELECT status FROM run_log WHERE kind = 'readiness' ORDER BY id DESC LIMIT 1").fetchone()
        assert log_row["status"] == "error"
    finally:
        os.rename(backup, row["path"])
    r2 = check(conn, now=now, days=1)
    assert r2["status"] in ("ok", "warning") and r2["missing"] == 0


def _assert_channel_day_sound(conn, channel_id, day):
    rows = conn.execute("SELECT start_ts, end_ts, kind, locked FROM schedule WHERE channel_id = ? AND day = ? ORDER BY start_ts",
                        (channel_id, day)).fetchall()
    assert rows
    assert rows[0]["start_ts"] == day_bounds(parse_day(day), dbm.all_settings(conn), tz_of(conn))[0]
    for a, b in pairwise(rows):
        assert a["end_ts"] == b["start_ts"], (dict(a), dict(b))
    return rows


def test_rebuild_never_overlaps_a_locked_slot(conn):
    """A programme may overrun closedown by the tolerance, never a fixed slot that follows it."""
    tz = tz_of(conn)
    ch = conn.execute("SELECT id FROM channels WHERE number = 2").fetchone()["id"]
    now = local_ts(parse_day("2026-09-19"), "11:00", tz)
    fixed = conn.execute("SELECT id FROM schedule WHERE channel_id = ? AND day = '2026-09-19' AND replay = 0 AND kind = 'programme'"
                         " AND start_ts > ? ORDER BY start_ts LIMIT 1 OFFSET 6", (ch, now)).fetchone()
    with dbm.tx(conn):
        conn.execute("UPDATE schedule SET locked = 1 WHERE id = ?", (fixed["id"],))
    try:
        for seed in range(6):
            r = rebuild_from(conn, ch, now, now=now, seed=seed)
            assert r["status"] in ("ok", "warning")
            rows = _assert_channel_day_sound(conn, ch, "2026-09-19")
            assert any(r["locked"] for r in rows), "locked slot must survive"
    finally:
        with dbm.tx(conn):
            conn.execute("UPDATE schedule SET locked = 0 WHERE id = ?", (fixed["id"],))


def test_anchored_show_keeps_the_day_contiguous(conn):
    """A strip anchored at a fixed time is padded up to, never left with a hole before it."""
    tz = tz_of(conn)
    ch = conn.execute("SELECT id FROM channels WHERE number = 1").fetchone()["id"]
    now = local_ts(parse_day("2026-09-20"), "10:00", tz)
    # A series that has not aired on that day yet (an anchor is skipped once the show has been
    # placed earlier in the kept part of the day) and still has episodes to come.
    show = conn.execute(
        "SELECT sh.id FROM shows sh WHERE sh.home_channel_id = ? AND sh.excluded = 0 AND sh.missing = 0 AND sh.category = 'general'"
        " AND NOT EXISTS (SELECT 1 FROM schedule s JOIN media m ON m.id = s.media_id"
        "                 WHERE s.channel_id = ? AND s.day = '2026-09-20' AND s.start_ts < ? AND m.show_id = sh.id)"
        " ORDER BY (SELECT COUNT(DISTINCT s.media_id) FROM schedule s JOIN media m ON m.id = s.media_id WHERE m.show_id = sh.id)"
        "        - (SELECT COUNT(*) FROM media m WHERE m.show_id = sh.id AND m.missing = 0) LIMIT 1",
        (ch, ch, now)).fetchone()["id"]
    # With exclusive line-ups a channel carries few series in the test library, so by the last
    # day even this one may have aired every episode and be resting (a resting series is never
    # anchored). Restart it from its first episode the way the admin would, with the cursor.
    first = conn.execute("SELECT season, episode FROM media WHERE show_id = ? AND missing = 0"
                         " ORDER BY season, episode LIMIT 1", (show,)).fetchone()
    with dbm.tx(conn):
        conn.execute("UPDATE shows SET mode = 'strip', anchor_time = '17:03', anchor_days = '[0,1,2,3,4,5,6]' WHERE id = ?", (show,))
        conn.execute("INSERT OR REPLACE INTO show_cursor(show_id, next_season, next_episode, set_at) VALUES (?,?,?,?)",
                     (show, first["season"], first["episode"], now + 1))
    try:
        for seed in range(4):
            r = rebuild_from(conn, ch, now, now=now, seed=seed)
            assert r["status"] in ("ok", "warning"), r
            _assert_channel_day_sound(conn, ch, "2026-09-20")
            anchored = conn.execute("SELECT s.start_ts FROM schedule s JOIN media m ON m.id = s.media_id WHERE s.channel_id = ?"
                                    " AND s.day = '2026-09-20' AND s.replay = 0 AND m.show_id = ?", (ch, show)).fetchall()
            assert any(minutes_of_day(a["start_ts"], tz) == 17 * 60 + 3 for a in anchored), "strip not placed at its time"
    finally:
        with dbm.tx(conn):
            conn.execute("UPDATE shows SET mode = 'auto', anchor_time = NULL, anchor_days = NULL WHERE id = ?", (show,))
            conn.execute("DELETE FROM show_cursor WHERE show_id = ?", (show,))
        rebuild_from(conn, ch, now, now=now, seed=42)


def test_rebuild_continues_episode_order_from_the_cut(conn):
    """Slots the rebuild replaces must not advance a series' cursor: the first episode of each
    series placed after the cut follows the latest one placed before it (anywhere), so a
    mid-week rebuild does not skip ahead of what later days already hold."""
    tz = tz_of(conn)
    ch = conn.execute("SELECT id FROM channels WHERE number = 2").fetchone()["id"]
    now = local_ts(parse_day("2026-09-18"), "13:00", tz)
    r = rebuild_from(conn, ch, now, now=now, seed=5)
    assert r["status"] in ("ok", "warning")
    after = conn.execute("SELECT m.show_id, m.season, m.episode, s.start_ts FROM schedule s JOIN media m ON m.id = s.media_id"
                         " WHERE s.channel_id = ? AND s.day = '2026-09-18' AND s.replay = 0 AND s.kind = 'programme'"
                         " AND s.start_ts >= ? AND m.show_id IS NOT NULL ORDER BY s.start_ts", (ch, now)).fetchall()
    assert after
    for sid in {r["show_id"] for r in after}:
        first = next(r for r in after if r["show_id"] == sid)
        before = conn.execute("SELECT m.season, m.episode FROM schedule s JOIN media m ON m.id = s.media_id"
                              " WHERE m.show_id = ? AND s.replay = 0 AND s.start_ts < ? ORDER BY s.start_ts DESC LIMIT 1",
                              (sid, now)).fetchone()
        eps = [(e["season"], e["episode"]) for e in conn.execute(
            "SELECT season, episode FROM media WHERE show_id = ? AND missing = 0 AND excluded = 0"
            " ORDER BY COALESCE(season, 999), COALESCE(episode, 999), path", (sid,))]
        expected = eps[(eps.index((before["season"], before["episode"])) + 1) % len(eps)] if before else eps[0]
        assert (first["season"], first["episode"]) == expected, (sid, dict(first), dict(before) if before else None)


def test_music_rebuild_fills_around_a_locked_slot(conn):
    """A locked video later in the day is built around: the gap before it is filled, not skipped."""
    tz = tz_of(conn)
    music = _channel(conn, 5)
    now = local_ts(parse_day("2026-09-18"), "09:00", tz)
    fixed = conn.execute("SELECT id FROM schedule WHERE channel_id = ? AND day = '2026-09-18' AND replay = 0"
                         " AND start_ts > ? ORDER BY start_ts LIMIT 1 OFFSET 40", (music["id"], now)).fetchone()
    with dbm.tx(conn):
        conn.execute("UPDATE schedule SET locked = 1 WHERE id = ?", (fixed["id"],))
    try:
        r = rebuild_from(conn, music["id"], now, now=now, seed=2)
        assert r["status"] in ("ok", "warning"), r
        rows = _assert_channel_day_sound(conn, music["id"], "2026-09-18")
        assert any(r["locked"] for r in rows), "locked slot must survive"
    finally:
        with dbm.tx(conn):
            conn.execute("UPDATE schedule SET locked = 0 WHERE id = ?", (fixed["id"],))


def test_kept_slots_inform_the_rebuild(conn):
    """Adverts before the cut count for the advert repeat rules, and the kept programmes feed
    the next day's same-slot bonus alongside the new ones."""
    tz = tz_of(conn)
    ch = _channel(conn, 3)
    assert ch["ads_enabled"]
    day = parse_day("2026-09-17")
    cut = local_ts(day, "15:00", tz)
    kept_ads = conn.execute("SELECT media_id, start_ts FROM schedule WHERE channel_id = ? AND day = ? AND replay = 0"
                            " AND kind = 'advert' AND start_ts < ?", (ch["id"], day.isoformat(), cut)).fetchall()
    kept_shows = {r[0] for r in conn.execute(
        "SELECT m.show_id FROM schedule s JOIN media m ON m.id = s.media_id WHERE s.channel_id = ? AND s.day = ?"
        " AND s.replay = 0 AND s.start_ts < ? AND m.show_id IS NOT NULL", (ch["id"], day.isoformat(), cut))}
    assert kept_ads and kept_shows
    builder = Builder(conn, now=cut, seed=3, rebuild={ch["id"]: (cut, None)})
    builder.build_channel_day(dict(ch), day, force=True, from_ts=cut)   # not saved
    assert all(builder.library.ad_last.get((ch["id"], a["media_id"]), 0) >= a["start_ts"] for a in kept_ads)


def test_replayed_placeholder_shares_its_request(conn):
    """The overnight replay of a placeholder is bound to the same wanted row, so the file that
    pitv_content delivers plays in both slots."""
    tz = tz_of(conn)
    builder = Builder(conn, now=local_ts(parse_day("2026-09-20"), "07:00", tz), seed=1)
    ch = builder.channels[0]
    day = parse_day("2026-09-20")
    _, day_end, next_day_start = day_bounds(day, builder.settings, tz)
    spec = {"kind": "episode", "lineup_id": 999, "title": "Episode 3", "season": 1, "episode": 3,
            "year": 1984, "reuse": 424242}
    placeholder = Slot(channel_id=ch["id"], day=day.isoformat(), start_ts=day_end - 1800, end_ts=day_end,
                       media_id=None, offset=0, kind="programme", title="The Tripods", subtitle="Episode 3",
                       wanted_spec=spec)
    replays = [s for s in builder._overnight(ch, day, day_end, next_day_start, [placeholder]) if s.kind == "programme"]
    assert replays
    builder._raise_wanted([placeholder, *replays])   # a reused request: nothing is written
    assert {s.wanted_id for s in [placeholder, *replays]} == {424242}


def test_overnight_drops_break_when_its_programme_is_skipped(conn):
    """Avoid an orphaned wall of adverts when a replayed series is suppressed at breakfast."""
    tz = tz_of(conn)
    day = parse_day("2026-09-20")
    builder = Builder(conn, now=local_ts(day, "07:00", tz), seed=1)
    ch = builder.channels[0]
    _, day_end, _ = day_bounds(day, builder.settings, tz)
    next_start = day_end + 50 * 60
    slots = [
        Slot(ch["id"], day.isoformat(), day_end - 7200, day_end - 5400, 1, 0, "programme",
             title="Different series", show_id=2),
        Slot(ch["id"], day.isoformat(), day_end - 5400, day_end - 1800, 2, 0, "programme",
             title="Breakfast series", show_id=1),
        Slot(ch["id"], day.isoformat(), day_end - 1800, day_end - 1740, 3, 0, "advert", title="Advert"),
        Slot(ch["id"], day.isoformat(), day_end - 1740, day_end, 4, 0, "programme",
             title="Film", show_id=None),
    ]
    builder._adjacent_show = lambda *_args, **_kwargs: 1
    replay = builder._overnight(ch, day, day_end, next_start, slots)
    assert not [s for s in replay if s.kind == "advert"]


def test_advert_runs_obey_channel_count(conn):
    """Rounding and gap padding share the configured per-break advert count."""
    for channel in conn.execute("SELECT id, ads_per_break FROM channels WHERE ads_enabled = 1"):
        run = 0
        for slot in conn.execute(
                "SELECT kind FROM schedule WHERE channel_id = ? AND replay = 0 ORDER BY start_ts", (channel["id"],)):
            run = run + 1 if slot["kind"] == "advert" else 0
            assert run <= channel["ads_per_break"]


def test_film_request_shared_across_days(conn):
    """A film placed on two days of one build raises a single wanted row."""
    builder = Builder(conn, seed=1)
    spec = {"kind": "movie", "lineup_id": 998, "title": "Brazil", "season": None, "episode": None,
            "year": 1985, "reuse": None}
    ch = builder.channels[0]["id"]
    monday, thursday = (Slot(channel_id=ch, day=d, start_ts=0, end_ts=1, media_id=None, offset=0, kind="programme",
                             title="Brazil", wanted_spec=dict(spec)) for d in ("2026-09-21", "2026-09-24"))
    try:
        with dbm.tx(conn):
            builder._raise_wanted([monday])
            builder._raise_wanted([thursday])
        assert monday.wanted_id is not None and monday.wanted_id == thursday.wanted_id
        assert conn.execute("SELECT COUNT(*) FROM wanted WHERE lineup_id = 998").fetchone()[0] == 1
    finally:
        with dbm.tx(conn):
            conn.execute("DELETE FROM wanted WHERE lineup_id = 998")


def test_anchor_after_midnight_across_clock_change(conn):
    """An anchor after midnight lands on the next calendar day's wall clock, including the night
    the clocks go back (02:30 on 25 October 2026 is 02:30 GMT, not 24 hours after 02:30 BST)."""
    tz = tz_of(conn)
    builder = Builder(conn, seed=1)
    ch = builder.channels[0]
    show = replace(builder.library.free_shows[ch["id"]][0], mode="strip", anchor_time="02:30",
                   anchor_days=list(range(7)), resting_until=None)
    builder.library.anchored[ch["id"]] = [show]
    day = date(2026, 10, 24)
    anchors = builder._anchors_for(ch, day, local_ts(day, "08:00", tz), local_ts(date(2026, 10, 25), "04:00", tz))
    assert [ts for ts, _ in anchors] == [local_ts(date(2026, 10, 25), "02:30", tz)]


def test_failed_build_is_logged(conn, monkeypatch):
    """A build that raises is recorded as an error rather than left 'running' in the run log."""
    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(Builder, "build_channel_day", boom)
    with pytest.raises(RuntimeError):
        build_horizon(conn, start_day=parse_day("2026-09-21"), days=1, seed=1,
                      now=local_ts(parse_day("2026-09-21"), "07:00", tz_of(conn)))
    row = conn.execute("SELECT status, summary FROM run_log WHERE kind = 'schedule' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "error" and "boom" in row["summary"]


def test_channel_without_idents_gets_the_stand_in(tmp_path):
    """Where a pattern asks for an ident and the channel has none, of its own or generic, the
    slot has no file (the player shows the shipped test signal under the badge); a channel with
    idents never gets the stand-in, and gaps are not padded with it."""
    from pitv.config import TEST_SIGNAL
    assert TEST_SIGNAL.is_file() and TEST_SIGNAL.stat().st_size > 0, "the test signal ships with the code"
    ctx = make_library(tmp_path, max_episodes=4)
    conn = ctx["conn"]
    toons, one = (conn.execute("SELECT id FROM channels WHERE number = ?", (n,)).fetchone()["id"] for n in (6, 1))
    with dbm.tx(conn):
        conn.execute("UPDATE channels SET pattern = 'show, ident' WHERE id IN (?, ?)", (toons, one))
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=1, seed=3, force=True)
    idents = {cid: conn.execute("SELECT media_id FROM schedule WHERE channel_id = ? AND kind = 'ident'", (cid,)).fetchall()
              for cid in (toons, one)}
    assert idents[toons] and all(r["media_id"] is None for r in idents[toons])
    assert idents[one] and all(r["media_id"] is not None for r in idents[one])


def _band_row(conn, channel_id, name, start, minutes, kinds, genres=(), decades=(), feature=False):
    conn.execute("INSERT INTO band(channel_id, name, start, minutes, days, fill, enabled, created_at)"
                 " VALUES (?,?,?,?,'[]',?,1,?)",
                 (channel_id, name, start, minutes,
                  json.dumps({"kinds": list(kinds), "genres": list(genres), "decades": list(decades), "feature": feature}),
                  dbm.now_ts()))


def test_a_band_is_a_titled_stretch_of_any_channels_day(tmp_path):
    """Bands are not a music feature: one on an ordinary channel takes its stretch of the day,
    carries its own title into the guide, and holds only what it asked for. The music channel is
    the same thing with no pattern of its own, so its whole day comes from bands."""
    ctx = make_library(tmp_path, max_episodes=6)
    conn = ctx["conn"]
    one, music = (conn.execute("SELECT id FROM channels WHERE number = ?", (n,)).fetchone()["id"] for n in (1, 5))
    with dbm.tx(conn):
        _band_row(conn, one, "Teatime Toons", "17:00", 60, ["episode"], genres=["animation"])
    day = parse_day("2026-09-14")
    build_horizon(conn, start_day=day, days=1, seed=4, force=True)
    tz = tz_of(conn)
    start, end = local_ts(day, "17:00", tz), local_ts(day, "18:00", tz)
    banded = conn.execute("SELECT * FROM schedule WHERE channel_id = ? AND replay = 0 AND start_ts >= ? AND start_ts < ?"
                          " ORDER BY start_ts", (one, start, end)).fetchall()
    assert banded and all(s["block"] == "Teatime Toons" for s in banded), "the band names its stretch"
    outside = conn.execute("SELECT COUNT(*) FROM schedule WHERE channel_id = ? AND replay = 0 AND block = 'Teatime Toons'"
                           " AND (start_ts < ? OR start_ts >= ?)", (one, start, end)).fetchone()[0]
    assert outside == 0, "a band stays inside its own stretch"
    # The music channel: no pattern, so its day is its bands, and the only unbilled time is
    # what a band gave back (nothing of its decades, or its one concert over). The small hours
    # are more of the channel's own material, never a replay of the day.
    music_slots = conn.execute("SELECT s.block, s.start_ts, m.kind FROM schedule s JOIN media m ON m.id = s.media_id"
                               " WHERE s.channel_id = ? AND s.replay = 0 AND s.kind = 'programme'", (music,)).fetchall()
    assert music_slots and {s["kind"] for s in music_slots} == {"music"}
    assert any(s["block"] for s in music_slots)
    overnight = conn.execute("SELECT s.block, m.kind FROM schedule s JOIN media m ON m.id = s.media_id"
                             " WHERE s.channel_id = ? AND s.replay = 1", (music,)).fetchall()
    assert overnight and {s["kind"] for s in overnight} == {"music"} and not any(s["block"] for s in overnight)
    # Each band runs at its own time: a day of bands must not collapse into one all-day block,
    # and nothing is billed under a band outside that band's stretch.
    windows = [(local_ts(day, r["start"], tz), r["name"])
               for r in conn.execute("SELECT name, start FROM band WHERE channel_id = ? ORDER BY start", (music,))
               if local_ts(day, r["start"], tz) >= local_ts(day, "08:00", tz)]
    placed = [s for s in music_slots if s["block"]]
    assert len({s["block"] for s in placed}) > 1, "one band must not take the whole day"
    for slot in placed:
        due = [name for at, name in windows if at <= slot["start_ts"]]
        assert slot["block"] == due[-1], f"{slot['block']} played in {due[-1]} band's time"


def test_final_band_item_finishes_before_overnight_replay(conn):
    """Midnight is a closedown boundary, not a point where a band item is cut off."""
    music = dbm.row_to_dict(_channel(conn, 5))     # the builder is given channels as dicts
    builder = Builder(conn, seed=1)
    band = next(b for b in builder.library.bands[music["id"]] if not b.feature)

    class OneItem:
        item_minutes = 15

        def __init__(self):
            self.used = False

        def pick(self, _band, _at, gap, feature, **_):
            if self.used or feature or gap < 180:
                return None
            self.used = True
            return {"id": 999999, "title": "Last song", "duration": 180, "year": 1985,
                    "genres": ["pop"]}

        def note(self, _item, _at):
            pass

    slots = []
    end = 1_000
    actual = builder._fill_band(music, "2026-09-16", band, end - 30, end, OneItem(), slots.append,
                                hard_end=end + 8 * 3600)
    assert actual == end + 150 and slots[-1].end_ts == actual

    day = parse_day("2026-09-16")
    _, day_end, next_start = day_bounds(day, builder.settings, tz_of(conn))
    programme = replace(slots[-1], channel_id=music["id"], day=day.isoformat(),
                        start_ts=day_end - 30, end_ts=day_end + 150)
    overnight = builder._overnight(music, day, day_end, next_start, [programme])
    assert overnight and overnight[0].start_ts == programme.end_ts


def test_short_episodes_run_together_under_the_series_title(tmp_path):
    """A five minute cartoon takes no slot of its own: the next episodes follow it straight away
    under the series title, so the guide shows one entry of an ordinary programme's length."""
    ctx = make_library(tmp_path, max_episodes=8)
    conn = ctx["conn"]
    toons = conn.execute("SELECT id FROM channels WHERE content = 'cartoons'").fetchone()["id"]
    with dbm.tx(conn):
        conn.execute("UPDATE channels SET short_episode_minutes = 12, short_episode_run_minutes = 20"
                     " WHERE id = ?", (toons,))
    # The second day exercises cadence repeats: they must replay the whole bundle rather than
    # collapsing back to one short episode followed by adverts.
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=2, seed=7, force=True)
    slots = conn.execute("SELECT title, block, start_ts, end_ts FROM schedule WHERE channel_id = ?"
                         " AND replay = 0 AND kind = 'programme' ORDER BY start_ts", (toons,)).fetchall()
    runs, current = [], []
    for slot in slots:
        if current and slot["block"] and slot["block"] == current[-1]["block"]:
            current.append(slot)
            continue
        if len(current) > 1:
            runs.append(current)
        current = [slot]
    if len(current) > 1:
        runs.append(current)
    assert runs, "short episodes should have been run together"
    for run in runs:
        assert all(s["title"] == run[0]["block"] for s in run), "a run is one series under its own title"
        assert all(s["end_ts"] - s["start_ts"] < 12 * 60 for s in run), "only short episodes are run together"
        assert run[-1]["end_ts"] - run[0]["start_ts"] >= 15 * 60, "a run lasts about as long as a programme"
    assert not conn.execute(
        "SELECT 1 FROM schedule WHERE channel_id = ? AND replay = 0 AND kind = 'programme'"
        " AND end_ts - start_ts < 12 * 60 AND block IS NULL LIMIT 1", (toons,)
    ).fetchone(), "short first-runs and cadence repeats must always belong to a bundle"


def test_a_band_keeps_its_length_past_closedown(conn):
    """Midnight does not cut a band short: one that starts at 23:30 for two hours runs two hours,
    and the overnight starts when it ends."""
    music = dbm.row_to_dict(_channel(conn, 5))
    builder = Builder(conn, seed=2)
    day = parse_day("2026-09-16")
    day_start, day_end, next_start = day_bounds(day, builder.settings, tz_of(conn))
    with dbm.tx(conn):
        conn.execute("UPDATE band SET start = '23:30', minutes = 120 WHERE channel_id = ? AND id ="
                     " (SELECT id FROM band WHERE channel_id = ? ORDER BY start DESC LIMIT 1)",
                     (music["id"], music["id"]))
    builder = Builder(conn, seed=2)
    placed = bands.timetable(builder.library.bands[music["id"]], day, day_start, day_end, next_start,
                             builder.day_start_min, tz_of(conn))
    start, end, band = placed[-1]
    assert start == local_ts(day, "23:30", tz_of(conn))
    assert end == start + 120 * 60 > day_end, f"{band.name} was cut at closedown"
    assert end <= next_start, "and never runs into tomorrow's broadcast day"


def test_specials_stay_out_of_the_rotation(tmp_path):
    """Season 0 sorts first, so a series used to open its run with a gag reel. Specials are
    never scheduled, as episodes or in a band, and remain in the catalogue."""
    c = make_library(tmp_path, max_episodes=4)["conn"]
    with dbm.tx(c):
        c.execute("UPDATE media SET season = 0 WHERE kind = 'episode' AND episode = 1")
    specials = c.execute("SELECT COUNT(*) FROM media WHERE season = 0 AND missing = 0").fetchone()[0]
    build_horizon(c, start_day=parse_day("2026-09-14"), days=2, seed=8, force=True)
    assert specials and c.execute("SELECT COUNT(*) FROM schedule WHERE kind = 'programme'").fetchone()[0]
    assert not c.execute("SELECT 1 FROM schedule s JOIN media m ON m.id = s.media_id WHERE m.season = 0").fetchone()
    assert c.execute("SELECT COUNT(*) FROM media WHERE season = 0 AND missing = 0").fetchone()[0] == specials
    c.close()


def test_a_run_never_shows_the_same_episode_twice():
    """A series whose only short file sits among long episodes (a trailer filed beside them)
    comes round to that file again when the run looks for more; the run ends instead."""
    from pitv.scheduler.runs import Runs
    from pitv.scheduler.slots import programme_slot
    c = dbm.connect(":memory:")
    dbm.init_db(c)
    builder = Builder(c, now=local_ts(parse_day("2026-09-14"), "10:00", tz_of(c)))
    channel = dbm.row_to_dict(c.execute("SELECT * FROM channels WHERE number = 6").fetchone())
    episodes = [{"id": n, "title": f"Part {n}", "duration": 2700.0, "season": 1, "episode": n, "kind": "episode"}
                for n in (1, 2, 3)]
    trailer = {"id": 9, "title": "tvshow-trailer", "duration": 120.0, "season": 1, "episode": 9, "kind": "episode"}
    show = Show(id=1, title="Trainwreck", year=2022, home_channel_id=channel["id"], mode="auto",
                anchor_time=None, anchor_days=[], rest_weeks=0, episodes=[*episodes, trailer], next_index=3)
    first = programme_slot(channel["id"], "2026-09-14", builder.now, show.next_episode(), show)
    show.advance(builder.now)
    more, end = Runs(builder.policy, builder.library).series_run(channel, "2026-09-14", show, first, 3600)
    assert first.media_id == 9 and more == [] and end == first.end_ts
    c.close()


def test_a_strict_channel_plays_only_labelled_material(tmp_path):
    """A channel may refuse anything the index has not labelled. Its programmes and its bands
    then carry a genre and a year, and a band with nothing left shows its own title card rather
    than something untagged."""
    ctx = make_library(tmp_path, max_episodes=6)
    conn = ctx["conn"]
    music = conn.execute("SELECT id FROM channels WHERE content = 'music'").fetchone()["id"]
    one = conn.execute("SELECT id FROM channels WHERE number = 1").fetchone()["id"]
    with dbm.tx(conn):
        conn.execute("UPDATE channels SET strict_matching = 1 WHERE id IN (?, ?)", (music, one))
        conn.execute("UPDATE media SET year = NULL, genres = '[]' WHERE kind = 'music' AND id IN"
                     " (SELECT id FROM media WHERE kind = 'music' ORDER BY id LIMIT 20)")
    day = parse_day("2026-09-14")
    build_horizon(conn, start_day=day, days=1, seed=11, force=True)
    # An episode's genres are the series', as they are when the scheduler weighs it up.
    played = conn.execute("SELECT m.year, COALESCE(NULLIF(m.genres, '[]'), sh.genres) AS genres"
                          " FROM schedule s JOIN media m ON m.id = s.media_id"
                          " LEFT JOIN shows sh ON sh.id = m.show_id"
                          " WHERE s.channel_id IN (?, ?) AND s.kind = 'programme' AND s.replay = 0",
                          (music, one)).fetchall()
    assert played
    for row in played:
        assert row["year"], "a strict channel plays nothing undated"
        assert json.loads(row["genres"] or "[]"), "a strict channel plays nothing untagged"


def test_explicit_channel_assignment_beats_automatic_year_and_metadata_filters():
    """A direct line-up choice is an instruction, not a suggestion. Strict matching and the
    channel's automatic era routing must not silently discard it."""
    c = dbm.connect(":memory:")
    dbm.init_db(c)
    now = local_ts(parse_day("2026-09-14"), "07:00", tz_of(c))
    with dbm.tx(c):
        c.execute("UPDATE channels SET enabled = 0")
        channel = c.execute("SELECT id FROM channels WHERE number = 1").fetchone()[0]
        c.execute("UPDATE channels SET enabled = 1, pattern = 'show', ads_enabled = 0,"
                  " idents_enabled = 0, strict_matching = 1, decades = '[1980]',"
                  " kind_weights = '{\"tv\":1,\"movie\":0}' WHERE id = ?", (channel,))
        source = c.execute("INSERT INTO sources(uid,type,name,path) VALUES ('tv','tv','TV','/tv')").lastrowid
        show = c.execute("INSERT INTO shows(source_id,path,title,year,certificate,genres,home_channel_id,updated_at)"
                         " VALUES (?, 'modern-doc', 'Modern Documentary', 2025, 'U', '[]', ?, ?)",
                         (source, channel, now)).lastrowid
        media = c.execute("INSERT INTO media(source_id,kind,show_id,season,episode,title,year,path,duration,"
                          " certificate,genres,updated_at) VALUES (?, 'episode', ?, 1, 1, 'Pilot', 2025,"
                          " '/tv/modern-doc-s01e01.mp4', 1800, 'U', '[]', ?)",
                          (source, show, now)).lastrowid
        c.execute("INSERT INTO lineup(channel_id,kind,show_id,key,title,source,pinned,created_at,updated_at)"
                  " VALUES (?, 'show', ?, ?, 'Modern Documentary', 'library', 1, ?, ?)",
                  (channel, show, f"show:{show}", now, now))

    result = build_horizon(c, start_day=parse_day("2026-09-14"), days=1, now=now, seed=1, force=True)
    assert result["status"] in ("ok", "warning")
    assert c.execute("SELECT 1 FROM schedule WHERE channel_id = ? AND media_id = ? AND replay = 0",
                     (channel, media)).fetchone()
    c.close()


def test_remote_cutoff_and_weekly_series_cadence_are_hour_accurate():
    c = dbm.connect(":memory:")
    dbm.init_db(c)
    now = local_ts(parse_day("2026-09-14"), "10:00", tz_of(c))
    with dbm.tx(c):
        dbm.set_setting(c, "nas_only", False)
    builder = Builder(c, now=now)
    channel = dbm.row_to_dict(c.execute("SELECT * FROM channels WHERE number = 1").fetchone())
    # The preparation boundary changes weighting, never eligibility: remote material is still
    # preferable to a holding card when local/NAS choices cannot fill a near-term slot.
    assert not nas_only_for(channel, builder.settings)
    policy = builder.policy
    assert policy.external_weight(now + 23 * 3600) < policy.external_weight(now + 23 * 3600 + 1)
    bonus = dbm.all_settings(c)["series_cadence_bonus"]
    assert policy.cadence_factor(channel, now, now + 24 * 3600) < 0.1
    assert policy.cadence_factor(channel, now, now + 7 * 86400) == bonus
    assert not policy.next_episode_due(channel, now, now + 3 * 86400)
    assert policy.next_episode_due(channel, now, now + 7 * 86400 - 6 * 3600)
    # The cadence is the channel's to set: with a day, every series there is a daily strip, due
    # again the next day and wanted in the same slot, while other channels stay weekly.
    # A series keeps to its own day of the week: early on that day is fine once two fifths of the
    # week has passed, other days wait until it is half a week overdue, and a thin day (the
    # first step of relaxation) goes by the plain interval.
    ordinal = parse_day("2026-09-14").toordinal()
    key = ordinal % 7                                     # a series whose own day is the 14th
    assert policy.series_due(channel, key, None, now, ordinal) and not policy.series_due(channel, key + 1, None, now, ordinal)
    assert policy.series_due(channel, key, now - 3 * 86400, now, ordinal), "its own day, three days on"
    assert not policy.series_due(channel, key, now - 2 * 86400, now, ordinal), "too soon even on its own day"
    assert not policy.series_due(channel, key + 1, now - 8 * 86400, now, ordinal), "not its day, not yet overdue"
    assert policy.series_due(channel, key + 1, now - 11 * 86400, now, ordinal), "its day was missed"
    assert policy.series_due(channel, key + 1, now - 7 * 86400, now, ordinal, relax=1), "a thin day brings it forward"
    daily = {**channel, "series_cadence_days": 1}
    assert policy.next_episode_due(daily, now, now + 86400 - 6 * 3600)
    assert not policy.next_episode_due(daily, now, now + 6 * 3600)
    assert policy.cadence_factor(daily, now, now + 86400) == bonus
    c.close()


def test_a_channel_with_a_daily_cadence_shows_its_series_every_day(tmp_path):
    """A cartoon channel whose series waited a week between episodes filled its days with films.
    With the channel's cadence set to a day, each series it holds airs on each day built. Left
    alone, the same channel gives most series one day in the four (the fixture is small enough
    that one or two come round early as the last step before a holding card)."""
    c = make_library(tmp_path, 12)["conn"]
    now = local_ts(parse_day("2026-09-14"), "07:00", tz_of(c))
    # The general channel with the most films: they carry the day, so the weekly rule holds there
    # without the last resort that a channel short of material falls back on.
    channel = c.execute("SELECT l.channel_id FROM lineup l JOIN channels ch ON ch.id = l.channel_id"
                        " WHERE ch.content = 'general' GROUP BY 1 ORDER BY SUM(l.kind = 'movie') DESC, 1").fetchone()[0]

    def days_aired() -> dict[int, int]:
        build_horizon(c, start_day=parse_day("2026-09-14"), days=4, now=now, seed=3, force=True)
        return {r[0]: r[1] for r in c.execute(
            "SELECT m.show_id, COUNT(DISTINCT s.day) FROM schedule s JOIN media m ON m.id = s.media_id"
            " JOIN shows sh ON sh.id = m.show_id WHERE s.channel_id = ? AND s.replay = 0 AND sh.category != 'sport'"
            " GROUP BY 1", (channel,))}
    weekly = days_aired()
    assert weekly and min(weekly.values()) == 1 and sum(weekly.values()) < 4 * len(weekly), "weekly by default"
    with dbm.tx(c):
        c.execute("UPDATE channels SET series_cadence_days = 1 WHERE id = ?", (channel,))
    daily = days_aired()
    every_day = [show for show, days in daily.items() if days >= 3]    # the peak hours may not reach one on a given day
    # Which series a run happens to reach varies; the comparison is over those that aired in both.
    both = set(daily) & set(weekly)
    assert len(both) >= 4 and sum(daily[s] for s in both) > sum(weekly[s] for s in both), (weekly, daily)
    assert len(every_day) >= len(daily) - 1, daily     # a series kept to weekends, say, is the exception
    c.close()


def test_external_repeat_reuses_request_without_advancing_episode():
    c = dbm.connect(":memory:")
    dbm.init_db(c)
    builder = Builder(c, now=local_ts(parse_day("2026-09-14"), "10:00", tz_of(c)))
    channel = dbm.row_to_dict(c.execute("SELECT * FROM channels WHERE number=1").fetchone())
    entry = {"id": -99, "lineup_id": 99, "kind": "episode", "title": "Remote Docs",
             "duration": 1800, "year": 1995, "genres": ["Documentary"], "taken": {1, 2, 3},
             "spare_wanted": []}
    first = builder.runs.external_slot(channel["id"], "2026-09-14", builder.now, entry)
    repeat = builder.runs.external_slot(channel["id"], "2026-09-14", builder.now + 3600,
                                        {**entry, "_external_repeat": True})
    assert first.wanted_spec["episode"] == repeat.wanted_spec["episode"] == 4
    assert entry["taken"] == {1, 2, 3, 4}, "the repeat took no number of its own"
    assert first.replay == 0 and repeat.replay == 1
    c.close()


def test_a_band_short_of_material_asks_for_more(tmp_path, monkeypatch):
    """A band with nothing of its own in the library has material fetched for it, of the kind its
    channel asks for, carrying the band's genres and decades. Nothing about this is particular to
    music: the kind is configuration, so a cartoons channel asks for cartoons."""
    from pitv import tool_client, wanted
    ctx = make_library(tmp_path, max_episodes=4)
    conn = ctx["conn"]
    toons = conn.execute("SELECT id FROM channels WHERE content = 'cartoons'").fetchone()["id"]
    with dbm.tx(conn):
        conn.execute("UPDATE channels SET fetch_kind = 'cartoons' WHERE id = ?", (toons,))
        # The fixture's music channel is short of material too and airs first. A channel that
        # asks for nothing is left alone however thin its bands, which leaves the cartoons.
        conn.execute("UPDATE channels SET fetch_kind = '' WHERE content = 'music'")
        _band_row(conn, toons, "Saturday Morning", "09:00", 120, ["episode"],
                  genres=["stop motion"], decades=[1980])
    settings = dbm.all_settings(conn)
    needs = wanted.band_needs(conn, settings)
    assert [(n["channel"]["id"], n["band"].name) for n in needs] == [(toons, "Saturday Morning")]
    need = next(n for n in needs if n["channel"]["id"] == toons)
    assert need["want"] == 60, "a daily two-hour band needs two days for its 36-hour repeat gap"

    sent = {}
    def fake_request(base, method, path, query="", body=None, timeout=15):
        sent.update({"path": path, "body": body})
        return 200, {"ok": True, "job_id": "test"}
    monkeypatch.setattr(tool_client, "request", fake_request)
    result = wanted.request_band_material(conn, settings)
    assert result["asked"] == 1 and sent["path"] == "run"
    assert sent["body"]["mode"] == "catalogue" and sent["body"]["kind"] == "cartoons"
    assert sent["body"]["genres"] == ["Stop Motion"] and sent["body"]["years"] == [1980, 1989]
    assert sent["body"]["max_minutes"] == settings["band_item_max_minutes"]
    assert sent["body"]["count"] == 60
    # Asked once, then left alone, so one stubborn band cannot block the rest night after night.
    assert wanted.request_band_material(conn, settings)["summary"] != result["summary"]


def test_a_channels_decades_limit_what_it_shows(tmp_path):
    """A channel may be held to certain decades; unknown years are still allowed, as their era
    weight already decides how often they air."""
    ctx = make_library(tmp_path, max_episodes=6)
    conn = ctx["conn"]
    one = conn.execute("SELECT id FROM channels WHERE number = 1").fetchone()["id"]
    with dbm.tx(conn):
        conn.execute("UPDATE channels SET decades = ? WHERE id = ?", (json.dumps([1980]), one))
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=1, seed=6, force=True)
    years = conn.execute(
        "SELECT DISTINCT COALESCE(m.year, sh.year) AS year FROM schedule s JOIN media m ON m.id = s.media_id"
        " LEFT JOIN shows sh ON sh.id = m.show_id WHERE s.channel_id = ? AND s.kind = 'programme' AND s.replay = 0",
        (one,)).fetchall()
    placed = [r["year"] for r in years if r["year"] is not None]
    assert placed, "the channel still has programmes"
    assert all(1980 <= y <= 1989 for y in placed), f"outside the 1980s: {sorted(placed)}"


def test_each_general_channel_is_modelled_on_its_own_broadcaster(tmp_path):
    """PiTV One to Four follow BBC One, BBC Two, ITV and Channel 4 of the 1980s. The models are
    seed data on the channels, editable in the admin: each has its own weekday, Saturday and
    Sunday, a well formed day, genre names the library can match, and sport where that
    broadcaster had it. An upgrade seeds them once and never over what the owner has set."""
    from pitv import genres
    from pitv.channel_profiles import BY_DEFAULT_CHANNEL
    from pitv.scheduler.rules import hhmm_to_minutes
    c = dbm.connect(tmp_path / "p.db")
    dbm.init_db(c)
    profiles = {r["number"]: json.loads(r["daypart_profile"]) for r in
                c.execute("SELECT number, daypart_profile FROM channels WHERE number <= 4")}
    assert profiles == BY_DEFAULT_CHANNEL and len({json.dumps(p) for p in profiles.values()}) == 4
    for profile in profiles.values():
        assert set(profile) == {"weekday", "saturday", "sunday"}
        for rows in profile.values():
            starts = [hhmm_to_minutes(r["start"]) for r in rows]
            assert starts[0] == 480 and starts == sorted(set(starts)), "the day opens at 08:00 and its parts are in order"
            for r in rows:
                assert all(genres.canonical(g) == g for g in r.get("genres", {})), r
    def block(profile, part):
        return max(r["sport"] for r in profile[part])

    assert block(profiles[1], "saturday") >= 3 and block(profiles[3], "saturday") >= 3, "Grandstand and World of Sport"
    assert block(profiles[2], "sunday") >= 3 and block(profiles[2], "saturday") >= 3, "BBC Two took the weekend overflow"
    weekday_evening = [r["sport"] for r in profiles[2]["weekday"] if hhmm_to_minutes(r["start"]) >= 17 * 60]
    assert min(weekday_evening) >= 1.0, "snooker and darts ran through BBC Two's weekday evenings"
    assert max(r["sport"] for r in profiles[1]["weekday"] if hhmm_to_minutes(r["start"]) < 22 * 60) < 0.5
    for number, profile in profiles.items():
        assert profile["saturday"][0]["kids"] >= 3, f"Saturday morning was children's television on channel {number}"
        for rows in profile.values():
            for r in rows:
                quiz = r.get("genres", {}).get("Game Show")
                daytime_quiz = (number, r["name"]) in {(3, "Afternoon"), (4, "Teatime")}     # as the listings had them
                assert (quiz == 0 or daytime_quiz) if r["start"] < "17:00" else (quiz is None or quiz > 0), (number, r["name"])
    # An upgrade: a channel with nothing of its own is seeded, one the owner has touched is not.
    mine = json.dumps([{"name": "All day", "start": "08:00", "tv": 1, "movie": 1, "kids": 1, "sport": 1}])
    with dbm.tx(c):
        c.execute("UPDATE channels SET daypart_profile = NULL WHERE number = 1")
        c.execute("UPDATE channels SET daypart_profile = ? WHERE number = 2", (mine,))
        c.execute("UPDATE channels SET daypart_profile = NULL, name = 'Pete One' WHERE number = 3")
    dbm.init_db(c)
    after = {r["number"]: r["daypart_profile"] for r in c.execute("SELECT number, daypart_profile FROM channels WHERE number <= 3")}
    assert json.loads(after[1]) == BY_DEFAULT_CHANNEL[1] and after[2] == mine and after[3] is None
    c.close()


def test_a_daypart_can_favour_a_genre():
    """What keeps the quiz at teatime: a daypart's genre weights multiply a programme's chance
    by the largest weight given to any of its genres, read canonically, and leave others alone."""
    from pitv.scheduler.select import Selector
    teatime = {"genres": {"Game Show": 3.0, "Soap": 2.5, "Sci-Fi": 0.5}}
    assert Selector._daypart_genre_weight(teatime, ["Comedy", "Game Show"]) == 3.0
    assert Selector._daypart_genre_weight(teatime, ["Science Fiction"]) == 0.5
    assert Selector._daypart_genre_weight(teatime, ["Drama"]) == 1.0
    assert Selector._daypart_genre_weight({}, ["Game Show"]) == 1.0
    # A weight of 0 is a bar: a game show by day is out whatever else it is tagged.
    daytime = {"genres": {"Game Show": 0.0, "Comedy": 1.5}}
    assert Selector._daypart_genre_weight(daytime, ["Comedy", "Game Show"]) == 0.0
    assert Selector._daypart_genre_weight(daytime, ["Comedy"]) == 1.5


def test_a_general_channel_borrows_cartoons_only_where_its_day_asks_for_them(tmp_path):
    """Cartoons live on the cartoon channel. A general channel that lists them under "also
    carries" shows some on a Saturday morning, when its dayparts ask for children's television,
    and none in the evening or on a channel that borrows nothing; the cartoon channel's own
    daily run is undisturbed."""
    c = make_library(tmp_path, 12)["conn"]
    toons = c.execute("SELECT id FROM channels WHERE content = 'cartoons'").fetchone()["id"]
    general = [r["id"] for r in c.execute("SELECT id FROM channels WHERE content = 'general' ORDER BY number")]
    assert all(json.loads(r["also_carries"]) == ["cartoon"] for r in c.execute("SELECT also_carries FROM channels WHERE content = 'general'"))
    with dbm.tx(c):
        c.execute("UPDATE channels SET also_carries = '[]' WHERE id = ?", (general[1],))     # this one borrows nothing
    saturday = parse_day("2026-09-19")
    now = local_ts(saturday, "06:00", tz_of(c))
    build_horizon(c, start_day=saturday, days=2, now=now, seed=11, force=True)
    borrowed = c.execute(
        "SELECT s.channel_id, s.day, CAST(strftime('%H', s.start_ts, 'unixepoch', 'localtime') AS INTEGER) AS hour, m.title"
        " FROM schedule s JOIN media m ON m.id = s.media_id JOIN shows sh ON sh.id = m.show_id"
        " WHERE sh.home_channel_id = ? AND s.channel_id != ? AND s.replay = 0", (toons, toons)).fetchall()
    assert borrowed, "no general channel carried a cartoon on Saturday morning"
    assert all(r["channel_id"] != general[1] for r in borrowed), "a channel that borrows nothing carried one"
    assert all(8 <= r["hour"] < 13 for r in borrowed), [(r["day"], r["hour"], r["title"]) for r in borrowed]
    own = c.execute("SELECT COUNT(*) FROM schedule s JOIN media m ON m.id = s.media_id WHERE s.channel_id = ? AND m.show_id IS NOT NULL",
                    (toons,)).fetchone()[0]
    assert own > 10, "the cartoon channel still runs its own"
    # One episode position between the channels: a borrower never shows the same episode twice
    # in a day (the fixture's series are short enough to come round again within the two days).
    twice = c.execute("SELECT s.channel_id, s.day, m.id FROM schedule s JOIN media m ON m.id = s.media_id JOIN shows sh ON sh.id = m.show_id"
                      " WHERE sh.home_channel_id = ? AND s.channel_id != ? AND s.replay = 0 GROUP BY 1, 2, 3 HAVING COUNT(*) > 1",
                      (toons, toons)).fetchall()
    assert not twice, "a borrowed episode aired twice in a day on one channel"


def test_the_small_hours_do_not_repeat_what_has_been_withdrawn(tmp_path):
    """A wrong delivery aired in the day and was then excluded. The replay of that day is still
    to come, and must not carry it."""
    c = make_library(tmp_path, 6)["conn"]
    day = parse_day("2026-09-14")
    build_horizon(c, start_day=day, days=1, now=local_ts(day, "07:00", tz_of(c)), seed=2, force=True)
    aired = c.execute("SELECT s.channel_id, s.media_id FROM schedule s JOIN media m ON m.id = s.media_id WHERE s.replay = 1"
                      " AND s.kind = 'programme' AND m.kind IN ('episode', 'movie') LIMIT 1").fetchone()
    assert aired, "nothing was replayed to begin with"
    with dbm.tx(c):
        c.execute("UPDATE media SET excluded = 1 WHERE id = ?", (aired["media_id"],))
    # Late in the evening: the day's airing is in the past and kept, the small hours are rebuilt.
    build_horizon(c, start_day=day, days=1, now=local_ts(day, "23:30", tz_of(c)), seed=2, force=True)
    assert not c.execute("SELECT 1 FROM schedule WHERE media_id = ? AND replay = 1 AND start_ts > ?",
                         (aired["media_id"], local_ts(day, "23:30", tz_of(c)))).fetchone()


def test_a_week_built_from_nothing_opens_its_series_across_the_week(tmp_path):
    """With one episode a week every series is due at once in a week built from nothing; the
    first days took them all, and since a series returns to its weekday the later days stayed
    thin for good. A series that has never aired is offered first on its own day of the week;
    only when a day runs thin is another brought forward. A remote series is never offered
    straight after itself, whatever the relaxation."""
    c = make_library(tmp_path, 6)["conn"]
    day = parse_day("2026-09-14")
    now = local_ts(day, "07:00", tz_of(c))
    builder = Builder(c, now=now)
    channel = next(ch for ch in builder.channels if ch["content"] == "general")
    own = {s.id for s in builder.library.free_shows.get(channel["id"], ()) if s.category != "sport"}
    assert len(own) >= 4
    rng = random.Random(3)
    for offset in range(7):
        t = local_ts(day + timedelta(days=offset), "19:30", tz_of(c))
        ordinal = (day + timedelta(days=offset)).toordinal()
        offered = set()
        for _ in range(150):
            pick = builder.select.programme(channel, rng, t, 2 * 3600, "tv", {}, None, set(), relax=0, slack=300)
            if pick and pick[1] is not None and pick[1].id in own:
                offered.add(pick[1].id)
        assert all(sid % 7 == ordinal % 7 for sid in offered), (offset, offered)
    brought_forward = {p[1].id for p in (builder.select.programme(channel, rng, t, 2 * 3600, "tv", {}, None, set(), relax=1, slack=300)
                                         for _ in range(150)) if p and p[1] is not None}
    assert len(brought_forward & own) > 1, "a thin day may bring others forward"

    entry = {"id": -5, "lineup_id": 5, "kind": "episode", "title": "Remote", "duration": 1800, "year": 1985, "genres": ["Comedy"],
             "taken": {1}, "spare_wanted": [], "channel_id": channel["id"], "pinned": 1, "kids": False, "category": "general",
             "last_spec": {"kind": "episode", "lineup_id": 5, "title": "Episode 1", "season": 1, "episode": 1, "year": 1985, "reuse": None}}
    builder.library.externals_on[channel["id"]] = [entry]
    builder.library.free_shows[channel["id"]] = []
    builder.library.movies_on[channel["id"]] = []
    with dbm.tx(c):
        dbm.set_setting(c, "nas_only", False)
    builder.select.settings["nas_only"] = False
    again = builder.select.programme(channel, rng, t, 2 * 3600, "tv", {}, None, {-5}, relax=2, slack=300)
    assert again is None, "the same remote series straight after itself"
    c.close()


def test_the_days_series_are_kept_for_the_peak_hours_when_there_are_too_few(tmp_path):
    """Filled from the morning on, a day spent its few due series by lunchtime and gave prime
    time to films. Outside the peak hours a series is offered only while more are due than the
    peak still to come could hold; children's series are not held back; and the peak hours
    themselves take series as before."""
    c = make_library(tmp_path, 6)["conn"]
    day = parse_day("2026-09-14")
    builder = Builder(c, now=local_ts(day, "07:00", tz_of(c)))
    channel = next(ch for ch in builder.channels if ch["content"] == "general"
                   and len(builder.library.free_shows.get(ch["id"], ())) >= 4)
    rng = random.Random(4)

    def offered(hhmm: str) -> set[str]:
        t = local_ts(day, hhmm, tz_of(c))
        picks = (builder.select.programme(channel, rng, t, 3 * 3600, "show", {}, None, set(), relax=0, slack=300) for _ in range(200))
        return {("kids " if p[0].get("kids") else "") + ("series" if p[1] is not None else "film") for p in picks if p}
    morning, evening = offered("10:00"), offered("19:30")
    assert "series" not in morning, morning          # a handful due, five peak hours to come
    assert "series" in evening, evening
    with dbm.tx(c):
        dbm.set_setting(c, "peak_from", "00:00")
        dbm.set_setting(c, "peak_until", "00:00")     # no peak hours: nothing is held back
    builder = Builder(c, now=local_ts(day, "07:00", tz_of(c)))
    assert "series" in offered("10:00")
    c.close()
