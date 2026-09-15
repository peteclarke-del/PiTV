"""Whole-week scheduler properties on the fake library."""
import json
import math
import os
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timedelta
from itertools import pairwise

import pytest
from conftest import make_library

from pitv import db as dbm
from pitv.scheduler.build import Builder, Slot, build_horizon, parse_day, rebuild_from, slot_titles
from pitv.scheduler.rules import (
    allowed_at,
    day_bounds,
    effective_cert,
    local_ts,
    minutes_of_day,
    tz_of,
)


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
    by_show = {}
    for r in rows:
        if r["show_id"]:
            by_show.setdefault(r["show_id"], []).append(r)
    checked = 0
    for sid, eps in by_show.items():
        eps.sort(key=lambda r: r["start_ts"])
        keys = [(r["season"], r["episode"]) for r in eps]
        # strictly increasing until a wrap back to the first episode
        for a, b in pairwise(keys):
            assert b > a or b == (1, 1), f"show {sid}: {a} then {b}"
            checked += 1
    assert checked > 50


def test_show_stays_on_home_channel(conn):
    rows = conn.execute("SELECT DISTINCT s.channel_id, m.show_id, sh.home_channel_id FROM schedule s"
                        " JOIN media m ON m.id = s.media_id JOIN shows sh ON sh.id = m.show_id WHERE s.replay = 0").fetchall()
    for r in rows:
        assert r["channel_id"] == r["home_channel_id"]


def test_movies_spread_evenly(conn):
    rows = conn.execute("SELECT media_id, COUNT(*) AS n FROM schedule s JOIN media m ON m.id = s.media_id"
                        " WHERE s.replay = 0 AND m.kind = 'movie' GROUP BY media_id").fetchall()
    slots = sum(r["n"] for r in rows)
    movies = conn.execute("SELECT COUNT(*) FROM media WHERE kind = 'movie' AND year IS NOT NULL").fetchone()[0]
    # The fake library is far too small for a week, so repeats are unavoidable; they must be
    # spread evenly (least recently aired first) rather than piling onto a few titles.
    # Certificate sub-pools (only three 18-rated films for late slots) allow one extra repeat.
    assert max(r["n"] for r in rows) <= math.ceil(slots / movies) + 2, (slots, movies, [r["n"] for r in rows])


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
    rows = conn.execute("SELECT s.channel_id, s.start_ts, s.replay, m.show_id, sh.category FROM schedule s JOIN media m ON m.id = s.media_id"
                        " LEFT JOIN shows sh ON sh.id = m.show_id WHERE s.kind = 'programme' ORDER BY s.channel_id, s.start_ts").fetchall()
    for a, b in pairwise(rows):
        if a["channel_id"] == b["channel_id"] and a["show_id"] is not None and not (a["replay"] and b["replay"]):
            if a["category"] == "sport" and datetime.fromtimestamp(b["start_ts"], tz).weekday() >= 5:
                continue  # sport may run back to back at weekends
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

    def share(days, hours):
        rows = conn.execute("SELECT s.start_ts, s.end_ts, sh.category FROM schedule s JOIN media m ON m.id = s.media_id"
                            " LEFT JOIN shows sh ON sh.id = m.show_id WHERE s.replay = 0 AND s.kind = 'programme'"
                            f" AND s.day IN ({','.join('?' * len(days))})"
                            f" AND s.channel_id IN ({','.join('?' * len(sport_channels))})", (*days, *sport_channels)).fetchall()
        rows = [r for r in rows if hours[0] <= datetime.fromtimestamp(r["start_ts"], tz).hour < hours[1]]
        total = sum(r["end_ts"] - r["start_ts"] for r in rows) or 1
        return sum(r["end_ts"] - r["start_ts"] for r in rows if r["category"] == "sport") / total

    assert share(["2026-09-19", "2026-09-20"], (12, 17)) > 0.3
    assert share(["2026-09-15", "2026-09-16"], (8, 22)) < 0.2


def _channel(conn, number):
    return conn.execute("SELECT * FROM channels WHERE number = ?", (number,)).fetchone()


def test_cartoons_routed_to_cartoon_channel(conn):
    toons = _channel(conn, 6)
    assert toons["content"] == "cartoons"
    rows = conn.execute("SELECT title, category FROM shows WHERE home_channel_id = ? ORDER BY title", (toons["id"],)).fetchall()
    titles = {r["title"] for r in rows}
    assert {"Danger Mouse", "Bananaman", "Thundercats", "Count Duckula"} <= titles
    assert all(r["category"] == "cartoon" for r in rows)
    # and no cartoon series on the general channels
    stray = conn.execute("SELECT s.title FROM shows s JOIN channels c ON c.id = s.home_channel_id"
                         " WHERE c.content = 'general' AND s.category = 'cartoon'").fetchall()
    assert not stray
    # cartoons run all evening on their own channel (kids cutoff does not apply there)
    tz = tz_of(conn)
    starts = conn.execute("SELECT start_ts FROM schedule WHERE channel_id = ? AND kind = 'programme' AND replay = 0",
                          (toons["id"],)).fetchall()
    assert any(datetime.fromtimestamp(r["start_ts"], tz).hour >= 21 for r in starts)


def test_music_channel_day(conn):
    music = _channel(conn, 5)
    assert music["content"] == "music"
    rows = conn.execute("SELECT s.*, m.concert, m.kind AS mkind FROM schedule s JOIN media m ON m.id = s.media_id"
                        " WHERE s.channel_id = ? AND s.day = '2026-09-16' AND s.replay = 0 ORDER BY s.start_ts", (music["id"],)).fetchall()
    assert rows and all(r["mkind"] == "music" for r in rows)
    assert all(r["block"] for r in rows)
    concerts = [r for r in rows if r["concert"]]
    assert len(concerts) == 2, [r["title"] for r in concerts]
    # contiguous from 08:00 to closedown
    for a, b in pairwise(rows):
        assert a["end_ts"] == b["start_ts"]
    # every eligible video is used before any repeats, and repeats are spread evenly
    ids = [r["media_id"] for r in rows]
    eligible = conn.execute("SELECT COUNT(*) FROM media WHERE kind = 'music' AND concert = 0 AND year BETWEEN 1970 AND 2009").fetchone()[0]
    assert len(set(ids)) >= min(eligible, len(ids)) - 2
    assert max(Counter(ids).values()) <= math.ceil(len(ids) / eligible) + 2
    # A block prefers its genres: they are over-represented in it compared with the whole day.
    # (Counts depend on what aired in the last 36 hours, so the test asserts the preference.)
    def soulful(r):
        genres = conn.execute("SELECT genres FROM media WHERE id = ?", (r["media_id"],)).fetchone()["genres"]
        return any(g.lower() in ("disco", "funk", "soul", "motown") for g in json.loads(genres))
    disco = [r for r in rows if r["block"] == "Disco & Soul"]
    assert disco
    assert sum(map(soulful, disco)) / len(disco) > sum(map(soulful, rows)) / len(rows)


def test_guide_collapses_music_blocks(conn):
    from pitv.guide import block_entry, collapse_blocks, next_programmes, slot_at
    music = _channel(conn, 5)
    rows = conn.execute("SELECT * FROM schedule WHERE channel_id = ? AND day = '2026-09-16' AND replay = 0 ORDER BY start_ts", (music["id"],)).fetchall()
    merged = collapse_blocks([dict(r) for r in rows])
    assert 5 <= len(merged) <= 12
    assert merged[0]["title"] == "Seventies Breakfast" and merged[0]["items"] > 1
    # the shared lookups (player OSD and web) agree with the raw merge
    ts = rows[3]["start_ts"] + 10
    slot = slot_at(conn, music["id"], ts)
    assert slot["id"] == rows[3]["id"]
    entry = block_entry(conn, slot, ts)
    assert entry["title"] == "Seventies Breakfast" and entry["video_title"] == slot["title"] and entry["video_id"] == slot["id"]
    assert entry["start_ts"] == merged[0]["start_ts"] and entry["end_ts"] == merged[0]["end_ts"]
    nxt = next_programmes(conn, music["id"], entry["end_ts"], 2)
    assert len(nxt) == 2 and nxt[0]["start_ts"] == entry["end_ts"] and nxt[0]["title"] == merged[1]["title"]


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
    assert all(builder.ad_last.get((ch["id"], a["media_id"]), 0) >= a["start_ts"] for a in kept_ads)
    assert kept_shows <= set(builder._day_minutes[(ch["id"], day.isoformat())])


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
    show = replace(builder._free_shows[ch["id"]][0], mode="strip", anchor_time="02:30",
                   anchor_days=list(range(7)), resting_until=None)
    builder._anchored[ch["id"]] = [show]
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
