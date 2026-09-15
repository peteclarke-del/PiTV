"""Upgrading a database created before the catalogue existed."""

import sqlite3

from pitv import db as dbm

OLD_SCHEMA = """
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
    assert not conn.execute("SELECT name FROM sqlite_master WHERE name = 'probe_cache'").fetchone()
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'media_uid'").fetchone()
    assert conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 6
    # music rows are now allowed, and a fetched item needs no source
    with dbm.tx(conn):
        conn.execute("INSERT INTO media(kind, title, path, origin) VALUES ('music', 'x', '/c/x.mp4', 'online')")


def test_table_definitions_survive_comments_with_brackets():
    stmt = dbm._create_statement("shows")
    assert stmt.startswith("CREATE TABLE IF NOT EXISTS shows (") and stmt.rstrip().endswith(");")
    sqlite3.connect(":memory:").execute(stmt.replace("IF NOT EXISTS shows", "IF NOT EXISTS t"))
