"""Channel line-ups: exclusive membership, genre-driven generation, external entries."""
import os

import pytest

from pitv import db as dbm
from pitv import lineup
from pitv.config import Config
from pitv.devtools import build_fake_library
from pitv.library.scanner import scan_all
from pitv.scheduler.build import build_horizon, parse_day
from pitv.scheduler.rules import local_ts, tz_of


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("lineup")
    lib = build_fake_library(root / "lib", max_episodes_per_show=12)
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
    return {"conn": c, "data_dir": cfg.data_dir}


@pytest.fixture(scope="module")
def conn(env):
    return env["conn"]


@pytest.fixture(scope="module")
def data_dir(env):
    return env["data_dir"]


def test_every_programme_belongs_to_exactly_one_channel(conn):
    shows = conn.execute("SELECT COUNT(*) FROM shows WHERE missing = 0 AND excluded = 0").fetchone()[0]
    movies = conn.execute("SELECT COUNT(*) FROM media WHERE kind = 'movie' AND missing = 0 AND excluded = 0").fetchone()[0]
    entries = conn.execute("SELECT kind, COUNT(*) AS n FROM lineup GROUP BY kind").fetchall()
    counts = {r["kind"]: r["n"] for r in entries}
    assert counts.get("show", 0) == shows and counts.get("movie", 0) == movies
    assert not conn.execute("SELECT show_id FROM lineup WHERE show_id IS NOT NULL GROUP BY show_id HAVING COUNT(*) > 1").fetchall()
    assert not conn.execute("SELECT 1 FROM shows WHERE missing = 0 AND excluded = 0 AND home_channel_id IS NULL").fetchall()
    assert not conn.execute("SELECT 1 FROM media WHERE kind = 'movie' AND missing = 0 AND home_channel_id IS NULL").fetchall()


def test_generation_respects_channel_genres(conn):
    channels = {r["id"]: dbm.row_to_dict(r) for r in conn.execute("SELECT * FROM channels")}
    for e in lineup.entries(conn):
        ch = channels[e["channel_id"]]
        assert ch["content"] in ("general", "cartoons")
        genres = {g.lower() for g in (e["genres"] or [])}
        assert lineup.channel_accepts(ch, genres), (e["title"], genres, ch["allowed_genres"])
    toons = conn.execute("SELECT id FROM channels WHERE content = 'cartoons'").fetchone()["id"]
    titles = {e["title"] for e in lineup.entries(conn, channel_id=toons)}
    assert {"Danger Mouse", "Thundercats"} <= titles


def test_week_never_shares_a_programme_across_channels(conn):
    now = local_ts(parse_day("2026-09-14"), "07:00", tz_of(conn))
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=7, now=now, seed=5)
    rows = conn.execute(
        "SELECT COALESCE(m.show_id, -m.id) AS prog, COUNT(DISTINCT s.channel_id) AS channels FROM schedule s"
        " JOIN media m ON m.id = s.media_id WHERE s.kind = 'programme' AND m.kind IN ('episode', 'movie')"
        " GROUP BY prog HAVING channels > 1").fetchall()
    assert not rows, [dict(r) for r in rows]
    homes = conn.execute(
        "SELECT COUNT(*) FROM schedule s JOIN media m ON m.id = s.media_id LEFT JOIN shows sh ON sh.id = m.show_id"
        " WHERE s.kind = 'programme' AND m.kind IN ('episode','movie')"
        " AND s.channel_id != COALESCE(sh.home_channel_id, m.home_channel_id)").fetchone()[0]
    assert homes == 0


def test_move_and_remove_entries(conn, data_dir):
    show = conn.execute("SELECT id, home_channel_id FROM shows WHERE title = 'Minder'").fetchone()
    other = conn.execute("SELECT id FROM channels WHERE content = 'general' AND id != ? ORDER BY number LIMIT 1", (show["home_channel_id"],)).fetchone()["id"]
    entry = lineup.add(conn, other, show_id=show["id"])
    assert entry["channel_id"] == other and entry["pinned"] == 1
    assert conn.execute("SELECT home_channel_id FROM shows WHERE id = ?", (show["id"],)).fetchone()[0] == other
    assert conn.execute("SELECT COUNT(*) FROM lineup WHERE show_id = ?", (show["id"],)).fetchone()[0] == 1
    lineup.remove(conn, entry["id"])
    assert conn.execute("SELECT home_channel_id FROM shows WHERE id = ?", (show["id"],)).fetchone()[0] is None
    lineup.generate(conn)  # unassigned again: generation puts it back somewhere
    assert conn.execute("SELECT home_channel_id FROM shows WHERE id = ?", (show["id"],)).fetchone()[0] is not None
    doc = lineup.export(conn)
    assert doc["schema"] == 1 and any(c["lineup"] for c in doc["channels"])
    lineup.write_mirror(conn)
    assert lineup.mirror_path(conn) == data_dir / "lineups.json" and (data_dir / "lineups.json").exists()


def test_external_entry_scheduled_ahead_and_requested(conn):
    ch = conn.execute("SELECT id FROM channels WHERE content = 'general' ORDER BY number LIMIT 1").fetchone()["id"]
    entry = lineup.add(conn, ch, title="The Tripods", year=1984, kind="show", genres=["Science Fiction"], episode_minutes=25)
    now = local_ts(parse_day("2026-09-14"), "07:00", tz_of(conn))
    # NAS-only (default): nothing not on disk is scheduled.
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=4, now=now, seed=9, force=True)
    assert conn.execute("SELECT COUNT(*) FROM schedule WHERE wanted_id IS NOT NULL").fetchone()[0] == 0
    with dbm.tx(conn):
        dbm.set_setting(conn, "nas_only", False)
        dbm.set_setting(conn, "external_weight", 50.0)  # make the entry win often in the test library
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=4, now=now, seed=9, force=True)
    slots = conn.execute("SELECT s.*, w.episode, w.transient FROM schedule s JOIN wanted w ON w.id = s.wanted_id"
                         " WHERE s.replay = 0 ORDER BY s.start_ts").fetchall()
    assert slots, "external entry never placed"
    lead = local_ts(parse_day("2026-09-16"), "08:00", tz_of(conn))
    assert all(sl["start_ts"] >= lead for sl in slots), "placed sooner than external_lead_days"
    assert all(sl["media_id"] is None and sl["title"] == "The Tripods" and sl["transient"] == 1 for sl in slots)
    episodes = [sl["episode"] for sl in slots]
    assert episodes == sorted(episodes) and len(set(episodes)) == len(episodes)
    wanted = conn.execute("SELECT * FROM wanted WHERE lineup_id = ?", (entry["id"],)).fetchall()
    assert len(wanted) == len(set(episodes))
    # Rebuilding reuses the wanted rows instead of raising duplicates.
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=4, now=now, seed=9, force=True)
    assert conn.execute("SELECT COUNT(*) FROM wanted WHERE lineup_id = ?", (entry["id"],)).fetchone()[0] >= len(wanted)
    assert not conn.execute("SELECT episode FROM wanted WHERE lineup_id = ? GROUP BY episode HAVING COUNT(*) > 1", (entry["id"],)).fetchall()
    # The manifest carries them for pitv_content with the series title and a search phrase.
    from pitv.content import manifest
    m = manifest(conn, days=3, now=now)
    mine = [w for w in m["wanted"] if w["lineup_id"] == entry["id"]]
    assert mine and mine[0]["show_title"] == "The Tripods" and mine[0]["transient"] is True
    assert "The Tripods" in mine[0]["hints"][0]
    with dbm.tx(conn):
        dbm.set_setting(conn, "nas_only", True)
        dbm.set_setting(conn, "external_weight", 0.7)


def test_readiness_substitutes_unfetched_placeholders(conn):
    from pitv.readiness import check
    placeholder = conn.execute("SELECT * FROM schedule WHERE wanted_id IS NOT NULL AND media_id IS NULL AND replay = 0 ORDER BY start_ts LIMIT 1").fetchone()
    assert placeholder is not None
    now = placeholder["start_ts"] - 6 * 3600
    r = check(conn, now=now, days=1)
    assert r["status"] == "error" and r["substituted"] >= 1
    assert conn.execute("SELECT COUNT(*) FROM schedule WHERE channel_id = ? AND wanted_id IS NOT NULL AND media_id IS NULL"
                        " AND start_ts >= ? AND start_ts < ?", (placeholder["channel_id"], now, now + 86400)).fetchone()[0] == 0
    rows = conn.execute("SELECT start_ts, end_ts FROM schedule WHERE channel_id = ? AND day = ? ORDER BY start_ts",
                        (placeholder["channel_id"], placeholder["day"])).fetchall()
    for a, b in zip(rows, rows[1:]):
        assert a["end_ts"] == b["start_ts"]


def test_bind_fetched_attaches_file_to_placeholder(conn):
    ch = conn.execute("SELECT id FROM channels WHERE content = 'general' ORDER BY number DESC LIMIT 1").fetchone()["id"]
    entry = lineup.add(conn, ch, title="Pending Film", year=1983, kind="movie", episode_minutes=90)
    with dbm.tx(conn):
        cur = conn.execute("INSERT INTO wanted(kind, title, year, provider, lineup_id, transient, created_at)"
                           " VALUES ('movie', 'Pending Film', 1983, 'auto', ?, 1, 0)", (entry["id"],))
        wid = cur.lastrowid
        media = conn.execute("SELECT id, path, duration FROM media WHERE kind = 'movie' ORDER BY id DESC LIMIT 1").fetchone()
        start = 4102444800  # far future so the slot is untouched by other tests
        conn.execute("INSERT INTO schedule(channel_id, day, start_ts, end_ts, media_id, offset, kind, title, subtitle, wanted_id)"
                     " VALUES (?, '2099-12-31', ?, ?, NULL, 0, 'programme', 'Pending Film', '', ?)",
                     (ch, start, start + 90 * 60, wid))
        conn.execute("UPDATE wanted SET status = 'done', dest_path = ? WHERE id = ?", (media["path"], wid))
    result = lineup.bind_fetched(conn)
    assert result["bound"] == 1 and result["converted"] == 1
    slot = conn.execute("SELECT media_id, end_ts, start_ts FROM schedule WHERE wanted_id = ?", (wid,)).fetchone()
    assert slot["media_id"] == media["id"]
    assert slot["end_ts"] - slot["start_ts"] == int(round(media["duration"]))
    e = lineup.entry(conn, entry["id"])
    assert e["media_id"] == media["id"] and e["external"] is False
    assert conn.execute("SELECT transient FROM media WHERE id = ?", (media["id"],)).fetchone()[0] == 1
    with dbm.tx(conn):
        conn.execute("DELETE FROM schedule WHERE day >= '2099-12-31'")
        conn.execute("UPDATE media SET transient = 0 WHERE id = ?", (media["id"],))
    lineup.remove(conn, entry["id"])
