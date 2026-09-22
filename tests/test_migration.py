"""Upgrading a database created before the catalogue existed."""

import sqlite3

from pitv import db as dbm

OLD_SCHEMA = """
CREATE TABLE transcode_queue (id INTEGER PRIMARY KEY, media_id INTEGER);
CREATE TABLE sources (id INTEGER PRIMARY KEY, type TEXT NOT NULL CHECK (type IN ('tv', 'movie', 'advert', 'ident')),
    name TEXT NOT NULL, path TEXT NOT NULL, remote TEXT, category TEXT NOT NULL DEFAULT 'general',
    enabled INTEGER NOT NULL DEFAULT 1, last_scanned_at INTEGER, last_scan_summary TEXT);
CREATE TABLE shows (id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    path TEXT NOT NULL UNIQUE, title TEXT NOT NULL, year INTEGER, certificate TEXT, genres TEXT, plot TEXT,
    kids INTEGER NOT NULL DEFAULT 0, home_channel_id INTEGER, mode TEXT NOT NULL DEFAULT 'auto', anchor_time TEXT,
    anchor_days TEXT, rest_weeks INTEGER NOT NULL DEFAULT 4, excluded INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0, overrides TEXT NOT NULL DEFAULT '{}', updated_at INTEGER);
CREATE TABLE media (id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('episode', 'movie', 'advert', 'ident')), show_id INTEGER, season INTEGER,
    episode INTEGER, title TEXT NOT NULL, year INTEGER, path TEXT NOT NULL UNIQUE, size INTEGER, mtime INTEGER,
    duration REAL, vcodec TEXT, acodec TEXT, width INTEGER, height INTEGER, interlaced INTEGER NOT NULL DEFAULT 0,
    hwdec INTEGER NOT NULL DEFAULT 0, certificate TEXT, genres TEXT, plot TEXT, channel_hint INTEGER,
    excluded INTEGER NOT NULL DEFAULT 0, missing INTEGER NOT NULL DEFAULT 0, attention TEXT,
    overrides TEXT NOT NULL DEFAULT '{}', transcoded_path TEXT, updated_at INTEGER);
CREATE TABLE probe_cache (path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime INTEGER NOT NULL, duration REAL,
    vcodec TEXT, acodec TEXT, width INTEGER, height INTEGER, interlaced INTEGER NOT NULL DEFAULT 0, probed_at INTEGER NOT NULL);
INSERT INTO sources(id, type, name, path) VALUES (1, 'tv', 'TV', '/mnt/tvshows');
INSERT INTO shows(id, source_id, path, title, year) VALUES (7, 1, '/mnt/tvshows/Minder (1979)', 'Minder', 1979);
INSERT INTO media(id, source_id, kind, show_id, season, episode, title, path, duration, transcoded_path)
    VALUES (42, 1, 'episode', 7, 1, 1, 'Episode 1', '/mnt/tvshows/Minder (1979)/S01E01.mkv', 3000, '/mnt/cache/pitv/42_x.mp4');
"""


def test_old_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    raw.commit()
    raw.close()
    conn = dbm.connect(path)
    dbm.init_db(conn)
    dbm.init_db(conn)   # and again: idempotent
    m = conn.execute("SELECT * FROM media WHERE id = 42").fetchone()
    assert m["cache_path"] == "/mnt/cache/pitv/42_x.mp4" and m["show_id"] == 7 and m["origin"] == "nas"
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(media)")}
    assert "transcoded_path" not in cols and {"uid", "cache_vcodec", "family_safe", "home_channel_id"} <= cols
    assert "source_id INTEGER NOT NULL" not in conn.execute("SELECT sql FROM sqlite_master WHERE name = 'shows'").fetchone()[0]
    src_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)")}
    assert {"uid", "location", "last_indexed_at", "index_summary"} <= src_cols and "last_scan_summary" not in src_cols
    for dropped in ('probe_cache', 'transcode_queue'):
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name = ?", (dropped,)).fetchone()
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'media_uid'").fetchone()
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'schedule_wanted'").fetchone()
    assert conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 6
    # music rows are now allowed, and a fetched item needs no source
    with dbm.tx(conn):
        conn.execute("INSERT INTO media(kind, title, path, origin) VALUES ('music', 'x', '/c/x.mp4', 'online')")


def test_table_definitions_survive_comments_with_brackets():
    stmt = dbm._create_statement("shows")
    assert stmt.startswith("CREATE TABLE IF NOT EXISTS shows (") and stmt.rstrip().endswith(");")
    sqlite3.connect(":memory:").execute(stmt.replace("IF NOT EXISTS shows", "IF NOT EXISTS t"))


def test_a_station_built_before_broadcasters_is_given_them(tmp_path):
    """The broadcaster a channel stands for arrived with the rule that uses it, and the rule
    reads the channel row, so a station built before it had the rule and nothing to feed it: a
    programme whose network was known was still placed by whichever channel listed the most of
    its genres. The shipped channels are given theirs once, and anything the owner has set is
    left exactly as it is."""
    from pitv.db import DEFAULT_CHANNELS, connect, init_db, rows_to_dicts

    conn = connect(tmp_path / "before.db")
    init_db(conn)
    with conn:
        conn.execute("UPDATE channels SET networks = NULL")
        # One renamed, and one the owner has already answered for themselves.
        conn.execute("UPDATE channels SET name = 'The Other One' WHERE number = 1")
        conn.execute("UPDATE channels SET networks = '[\"Somewhere Else\"]' WHERE number = 3")
    init_db(conn)

    after = {c["number"]: c for c in rows_to_dicts(conn.execute("SELECT * FROM channels"))}
    shipped = {c["number"]: c for c in DEFAULT_CHANNELS if c.get("networks")}
    assert after[1]["networks"] is None, "a renamed channel is not assumed to be the shipped one"
    assert after[3]["networks"] == ["Somewhere Else"], "what the owner set is left alone"
    for number in shipped:
        if number not in (1, 3):
            assert after[number]["networks"] == shipped[number]["networks"]
    conn.close()


def test_an_empty_install_comes_up_as_the_whole_station(tmp_path):
    """Deploying empty and rebuilding to the configured station must need nobody editing rows by
    hand, so what the seed omits is a thing somebody has to know to set. The seed had drifted
    behind the scheduler: patterns written before the ident led the break, no broadcasters on the
    general channels, and a cartoon channel that ran two programmes together. The checks here are
    on the properties the rest of the code relies on, not on the values themselves, which are
    configuration and are meant to be edited."""
    from pitv.db import DEFAULT_CHANNELS, connect, init_db, rows_to_dicts

    conn = connect(tmp_path / "fresh.db")
    init_db(conn)
    channels = rows_to_dicts(conn.execute("SELECT * FROM channels"))
    assert {c["number"] for c in channels} == {c["number"] for c in DEFAULT_CHANNELS}, "the whole station"

    for c in channels:
        tokens = [t.strip() for t in (c["pattern"] or "").split(",") if t.strip()]
        if "ident" in tokens:
            # An ident is placed only directly after a programme, so one written at the head of
            # the pattern is skipped on the day's first pass and the day opens without one.
            at = tokens.index("ident")
            assert at > 0 and tokens[at - 1] == "show", f"{c['name']}: the ident follows the programme"
    general = [c for c in channels if (c["content"] or "general") == "general"]
    # Each general channel stands for a broadcaster of its own, which is what places a series on
    # the channel that actually showed it. Two sharing a first broadcaster would both claim it.
    assert general and all(c["networks"] for c in general), "every general channel names a broadcaster"
    assert len({c["networks"][0] for c in general}) == len(general), "no two stand for the same one"
    # No general channel bars an era: which decades suit depends on the library somebody has,
    # and how sparse later material is belongs to the era weights rather than to a bar.
    assert not any(c["decades"] for c in general), "the general channels bar no era"
    assert conn.execute("SELECT COUNT(*) FROM band").fetchone()[0] > 0, "the music channel has its bands"

    colours = [c["colour"] for c in channels]
    assert len(set(colours)) == len(colours), "every channel is told apart at a glance"
    conn.close()
