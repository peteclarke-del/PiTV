"""Channel line-ups: exclusive membership, genre-driven generation, external entries."""

import pytest

from conftest import make_library
from pitv import db as dbm
from pitv import lineup
from pitv.scheduler.build import build_horizon, parse_day
from pitv.scheduler.rules import local_ts, tz_of


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    ctx = make_library(tmp_path_factory.mktemp("lineup"), max_episodes=12)
    return {"conn": ctx["conn"], "data_dir": ctx["cfg"].data_dir}


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
    ids = {w["id"] for w in wanted}
    mine = [i for i in m["items"] if i["wanted_id"] in ids]
    assert mine and all(i["action"] == "fetch" and i["request_id"] == f"w:{i['wanted_id']}" for i in mine)
    assert mine[0]["show_title"] == "The Tripods" and mine[0]["transient"] is True
    assert "The Tripods" in mine[0]["search"]["phrase"] and mine[0]["dest_dir"]
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


def test_delivery_report_fills_placeholder(conn, tmp_path):
    """A film fetched online arrives in a report: it becomes a catalogue entry, takes its
    placeholder slot at its real length and turns the external entry into a library one."""
    from pitv.content import apply_report
    ch = conn.execute("SELECT id FROM channels WHERE content = 'general' ORDER BY number DESC LIMIT 1").fetchone()["id"]
    entry = lineup.add(conn, ch, title="Pending Film", year=1983, kind="movie", episode_minutes=90)
    start = 4102444800  # far future so the slot is untouched by other tests
    with dbm.tx(conn):
        wid = conn.execute("INSERT INTO wanted(kind, title, year, provider, lineup_id, transient, created_at)"
                           " VALUES ('movie', 'Pending Film', 1983, 'auto', ?, 1, 0)", (entry["id"],)).lastrowid
        conn.execute("INSERT INTO schedule(channel_id, day, start_ts, end_ts, media_id, offset, kind, title, subtitle, wanted_id)"
                     " VALUES (?, '2099-12-31', ?, ?, NULL, 0, 'programme', 'Pending Film', '', ?)",
                     (ch, start, start + 90 * 60, wid))
    film = tmp_path / "Pending Film (1983).mp4"
    film.write_bytes(b"x" * 10)
    counts = apply_report(conn, {"schema": 2, "items": [{
        "request_id": f"w:{wid}", "wanted_id": wid, "status": "done",
        "file": {"path": str(film), "duration": 6420.0, "vcodec": "h264", "height": 576, "size": 10},
        "meta": {"kind": "movie", "title": "Pending Film", "year": 1983, "genres": ["Drama"], "certificate": "PG",
                 "uid": "yt:pending"}}]})
    assert counts["wanted_done"] == 1 and counts["created"] == 1
    media = conn.execute("SELECT * FROM media WHERE uid = 'yt:pending'").fetchone()
    assert media["origin"] == "online" and media["cache_path"] == str(film) and media["transient"] == 1
    slot = conn.execute("SELECT media_id, end_ts, start_ts FROM schedule WHERE wanted_id = ?", (wid,)).fetchone()
    assert slot["media_id"] == media["id"] and slot["end_ts"] - slot["start_ts"] == 6420
    e = lineup.entry(conn, entry["id"])
    assert e["media_id"] == media["id"] and e["external"] is False
    with dbm.tx(conn):
        conn.execute("DELETE FROM schedule WHERE day >= '2099-12-31'")
    lineup.remove(conn, entry["id"])


def test_skip_in_progress_is_neither_done_nor_failed(conn):
    """"skipped, being written by another process" arrives without a file block; it must not use
    up an attempt or count as a failed delivery."""
    from pitv.content import apply_report
    with dbm.tx(conn):
        wid = conn.execute("INSERT INTO wanted(kind, title, provider, created_at) VALUES ('music', 'In Progress', 'auto', 0)").lastrowid
    counts = apply_report(conn, {"schema": 2, "items": [
        {"request_id": f"w:{wid}", "wanted_id": wid, "status": "skipped", "message": "being written by another process", "file": None},
        {"request_id": "m:1", "media_id": 1, "status": "skipped", "message": "being written by another process", "file": None}]})
    assert counts == {"items_done": 0, "items_failed": 0, "wanted_done": 0, "wanted_failed": 0, "created": 0}
    row = conn.execute("SELECT status, attempts FROM wanted WHERE id = ?", (wid,)).fetchone()
    assert (row["status"], row["attempts"]) == ("queued", 0)
    with dbm.tx(conn):
        conn.execute("DELETE FROM wanted WHERE id = ?", (wid,))
