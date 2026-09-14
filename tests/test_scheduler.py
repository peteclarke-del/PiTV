"""Whole-week scheduler properties on the fake library."""
import os

import pytest

from pitv import db as dbm
from pitv.config import Config
from pitv.devtools import build_fake_library
from pitv.library.scanner import scan_all
from pitv.scheduler.build import build_horizon, parse_day, rebuild_from
from pitv.scheduler.rules import (allowed_at, day_bounds, effective_cert, local_ts, minutes_of_day, tz_of)


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    root = tmp_path_factory.mktemp("sched")
    lib = build_fake_library(root / "lib", max_episodes_per_show=30)
    cfg = Config(data_dir=root / "data", run_dir=root / "run")
    os.environ.pop("PITV_DB", None)
    cfg.ensure_dirs()
    c = dbm.connect(cfg.db_path)
    dbm.init_db(c)
    with dbm.tx(c):
        for stype, name, p in (("tv", "TV", lib["tv"]), ("movie", "Movies", lib["movies"]),
                               ("advert", "Ads", lib["pitv"] / "Adverts"), ("ident", "Idents", lib["pitv"] / "Idents")):
            c.execute("INSERT INTO sources(type, name, path) VALUES (?,?,?)", (stype, name, str(p)))
        c.execute("INSERT INTO sources(type, name, path, category) VALUES ('tv', 'Sport', ?, 'sport')", (str(lib["sport"]),))
    scan_all(c)
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
            day = parse_day("2026-09-14").replace(day=14 + i)
            ds, de, nds = day_bounds(day, settings, tz)
            rows = conn.execute("SELECT start_ts, end_ts, replay FROM schedule WHERE channel_id = ? AND day = ? ORDER BY start_ts",
                                (ch["id"], day.isoformat())).fetchall()
            assert rows, f"no slots for channel {ch['id']} {day}"
            assert rows[0]["start_ts"] == ds
            # contiguous, no overlaps
            for a, b in zip(rows, rows[1:]):
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
        for a, b in zip(keys, keys[1:]):
            assert b > a or b == (1, 1), f"show {sid}: {a} then {b}"
            checked += 1
    assert checked > 50


def test_show_stays_on_home_channel(conn):
    rows = conn.execute("SELECT DISTINCT s.channel_id, m.show_id, sh.home_channel_id FROM schedule s"
                        " JOIN media m ON m.id = s.media_id JOIN shows sh ON sh.id = m.show_id WHERE s.replay = 0").fetchall()
    for r in rows:
        assert r["channel_id"] == r["home_channel_id"]


def test_movies_spread_evenly(conn):
    import math
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
    from datetime import datetime
    tz = tz_of(conn)
    rows = conn.execute("SELECT s.channel_id, s.start_ts, s.replay, m.show_id, sh.category FROM schedule s JOIN media m ON m.id = s.media_id"
                        " LEFT JOIN shows sh ON sh.id = m.show_id WHERE s.kind = 'programme' ORDER BY s.channel_id, s.start_ts").fetchall()
    for a, b in zip(rows, rows[1:]):
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
    pre = sum(1 for y in years if y < 1980)
    assert 0.2 < pre / len(years) < 0.7, f"pre-1980 share {pre / len(years):.2f}"
    ad_years = [r["year"] for r in conn.execute("SELECT m.year FROM schedule s JOIN media m ON m.id = s.media_id WHERE s.kind = 'advert'")]
    assert ad_years and all(1980 <= y <= 1999 for y in ad_years)


def test_weekend_afternoons_carry_sport(conn):
    """Saturday 19th and Sunday 20th afternoons should be largely sport; weekday daytime should not."""
    from datetime import datetime
    tz = tz_of(conn)

    def share(days, hours):
        rows = conn.execute("SELECT s.start_ts, s.end_ts, sh.category FROM schedule s JOIN media m ON m.id = s.media_id"
                            " LEFT JOIN shows sh ON sh.id = m.show_id WHERE s.replay = 0 AND s.kind = 'programme'"
                            f" AND s.day IN ({','.join('?' * len(days))})", days).fetchall()
        rows = [r for r in rows if hours[0] <= datetime.fromtimestamp(r["start_ts"], tz).hour < hours[1]]
        total = sum(r["end_ts"] - r["start_ts"] for r in rows) or 1
        return sum(r["end_ts"] - r["start_ts"] for r in rows if r["category"] == "sport") / total

    assert share(["2026-09-19", "2026-09-20"], (12, 17)) > 0.3
    assert share(["2026-09-15", "2026-09-16"], (8, 22)) < 0.2
