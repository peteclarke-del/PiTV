"""SQLite schema, migrations, default data and small helpers.

The database is the source of truth for everything the admin UI can change. Time values
in the schedule are integer UNIX timestamps so lookups are cheap and unambiguous; local
times are derived for display using the configured timezone.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL            -- JSON
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('tv', 'movie', 'advert', 'ident')),
    name TEXT NOT NULL,
    path TEXT NOT NULL,            -- local mount path
    remote TEXT,                   -- e.g. smb://synologynas/tvshows/ (informational)
    enabled INTEGER NOT NULL DEFAULT 1,
    last_scanned_at INTEGER,
    last_scan_summary TEXT
);

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    number INTEGER NOT NULL UNIQUE,
    name TEXT NOT NULL,
    short_name TEXT NOT NULL,
    colour TEXT NOT NULL DEFAULT '#ffffff',
    enabled INTEGER NOT NULL DEFAULT 1,
    ads_enabled INTEGER NOT NULL DEFAULT 0,
    ads_per_break INTEGER NOT NULL DEFAULT 2,
    pattern TEXT NOT NULL DEFAULT 'show',     -- comma separated tokens
    era_weights TEXT,                          -- JSON or NULL (use global)
    genre_weights TEXT,                        -- JSON {genre: weight} or NULL
    kind_weights TEXT,                         -- JSON {"tv": w, "movie": w} or NULL
    daypart_profile TEXT,                      -- JSON or NULL (use global)
    overnight_replay_from TEXT NOT NULL DEFAULT '08:00',
    idents_enabled INTEGER NOT NULL DEFAULT 1,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS shows (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    path TEXT NOT NULL UNIQUE,     -- show folder
    title TEXT NOT NULL,
    year INTEGER,                  -- premiered year as scanned
    certificate TEXT,
    genres TEXT,                   -- JSON list
    plot TEXT,
    kids INTEGER NOT NULL DEFAULT 0,
    home_channel_id INTEGER REFERENCES channels(id) ON DELETE SET NULL,
    mode TEXT NOT NULL DEFAULT 'auto' CHECK (mode IN ('auto', 'strip', 'weekly')),
    anchor_time TEXT,              -- 'HH:MM' for strip/weekly
    anchor_days TEXT,              -- JSON list of weekday ints (0=Mon) for strip/weekly
    rest_weeks INTEGER NOT NULL DEFAULT 4,
    excluded INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    overrides TEXT NOT NULL DEFAULT '{}',   -- JSON: fields that beat scanned values
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('episode', 'movie', 'advert', 'ident')),
    show_id INTEGER REFERENCES shows(id) ON DELETE CASCADE,
    season INTEGER,
    episode INTEGER,
    title TEXT NOT NULL,
    year INTEGER,
    path TEXT NOT NULL UNIQUE,
    size INTEGER,
    mtime INTEGER,
    duration REAL,                 -- seconds
    vcodec TEXT,
    acodec TEXT,
    width INTEGER,
    height INTEGER,
    interlaced INTEGER NOT NULL DEFAULT 0,
    hwdec INTEGER NOT NULL DEFAULT 0,   -- 1 if the Pi 4 can hardware decode it
    certificate TEXT,
    genres TEXT,                   -- JSON list
    plot TEXT,
    channel_hint INTEGER,          -- idents: channel number the ident belongs to
    excluded INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    attention TEXT,                -- reason this item needs a look, or NULL
    overrides TEXT NOT NULL DEFAULT '{}',
    transcoded_path TEXT,          -- H.264 copy made by the transcode queue
    updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS media_show ON media(show_id, season, episode);
CREATE INDEX IF NOT EXISTS media_kind ON media(kind, year);

CREATE TABLE IF NOT EXISTS show_cursor (
    show_id INTEGER PRIMARY KEY REFERENCES shows(id) ON DELETE CASCADE,
    next_season INTEGER NOT NULL,
    next_episode INTEGER NOT NULL,
    set_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    day TEXT NOT NULL,             -- broadcast day YYYY-MM-DD (08:00 to 08:00)
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
    offset REAL NOT NULL DEFAULT 0,       -- seconds into the file at start_ts
    kind TEXT NOT NULL CHECK (kind IN ('programme', 'advert', 'ident', 'filler')),
    part INTEGER NOT NULL DEFAULT 1,
    replay INTEGER NOT NULL DEFAULT 0,    -- 1 for overnight replays
    locked INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',       -- denormalised for fast guide rendering
    subtitle TEXT NOT NULL DEFAULT ''     -- e.g. 'S02E05 Episode title' or '1983  PG'
);
CREATE INDEX IF NOT EXISTS schedule_lookup ON schedule(channel_id, start_ts);
CREATE INDEX IF NOT EXISTS schedule_day ON schedule(day);
CREATE INDEX IF NOT EXISTS schedule_media ON schedule(media_id, start_ts);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    media_id INTEGER,
    schedule_id INTEGER,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    title TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS history_media ON history(media_id, started_at);

CREATE TABLE IF NOT EXISTS probe_cache (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime INTEGER NOT NULL,
    duration REAL,
    vcodec TEXT,
    acodec TEXT,
    width INTEGER,
    height INTEGER,
    interlaced INTEGER NOT NULL DEFAULT 0,
    probed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS wanted (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('episode', 'movie', 'advert')),
    title TEXT NOT NULL,
    year INTEGER,
    season INTEGER,
    episode INTEGER,
    show_id INTEGER REFERENCES shows(id) ON DELETE SET NULL,
    provider TEXT NOT NULL DEFAULT 'auto',     -- auto | archive | url
    ref TEXT,                                  -- archive.org identifier[/file] or a URL
    status TEXT NOT NULL DEFAULT 'queued',     -- queued | searching | downloading | transcoding | done | failed
    progress REAL NOT NULL DEFAULT 0,
    message TEXT,
    dest_path TEXT,
    media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
    auto INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS transcode_queue (
    id INTEGER PRIMARY KEY,
    media_id INTEGER NOT NULL UNIQUE REFERENCES media(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',     -- queued | running | done | failed
    progress REAL NOT NULL DEFAULT 0,
    message TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,            -- scan | schedule | transcode
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    status TEXT NOT NULL DEFAULT 'running',
    summary TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '[]'   -- JSON list of messages
);
"""

DEFAULT_DAYPARTS = [
    {"name": "Breakfast", "start": "08:00", "tv": 1.0, "movie": 0.1, "kids": 1.5, "max_minutes": 40},
    {"name": "Morning", "start": "09:30", "tv": 1.0, "movie": 0.3, "kids": 0.5},
    {"name": "Lunchtime", "start": "12:00", "tv": 1.0, "movie": 0.2, "kids": 0.3, "max_minutes": 40},
    {"name": "Matinee", "start": "13:30", "tv": 0.4, "movie": 2.0, "kids": 0.5},
    {"name": "Children's", "start": "15:30", "tv": 1.0, "movie": 0.1, "kids": 6.0, "max_minutes": 35},
    {"name": "Early evening", "start": "17:30", "tv": 1.0, "movie": 0.1, "kids": 0.4, "max_minutes": 40},
    {"name": "Prime time", "start": "19:00", "tv": 1.0, "movie": 0.6, "kids": 0.05},
    {"name": "Post-watershed", "start": "21:00", "tv": 1.0, "movie": 1.2, "kids": 0.0},
    {"name": "Late", "start": "23:00", "tv": 0.6, "movie": 2.0, "kids": 0.0},
]

DEFAULT_SETTINGS: dict[str, Any] = {
    "timezone": "Europe/London",
    "day_start": "08:00",
    "day_end": "00:00",
    "horizon_days": 7,
    "rebuild_when_days_left": 2,
    "era_weights": {"1980-1989": 0.85, "1990-1999": 0.15},
    "watershed": {"U": "00:00", "PG": "00:00", "12": "20:00", "12A": "20:00", "15": "21:00", "18": "22:00"},
    "tv_watershed": {"15": "21:00", "18": "22:00"},
    "show_daily_limit": 2,
    "show_repeat_penalty": 0.3,
    "unknown_movie_certificate": "15",
    "unknown_tv_certificate": "PG",
    "kids_cutoff": "21:00",
    "kind_weights": {"tv": 0.7, "movie": 0.3},
    "dayparts": DEFAULT_DAYPARTS,
    "movie_repeat_days": 21,
    "episode_recency_days": 7,
    "same_slot_bonus": 3.0,
    "genre_repeat_penalty": 0.4,
    "duration_tolerance_minutes": 5,
    "start_rounding_minutes": 5,
    "end_of_day_overrun_minutes": 30,
    "advert_year_window": 3,
    "advert_repeat_penalty_hours": 6,
    "series_rest_weeks": 4,
    "weekend_kids_breakfast": True,
    "admin_password_hash": None,
    "channel_switch_static": True,
    # player
    "keymap": {},                      # action -> [evdev key names]; empty = built-in defaults
    "nav_keys_change_channel": True,   # up/down = channel +/- when the guide is closed (OSMC remote has no channel keys)
    "nav_keys_change_volume": True,    # left/right = volume when the guide is closed
    "badge_seconds": 5,
    "pi_hwdec": "drm-prime,v4l2m2m-copy",
    "audio_device": "auto",
    # local cache on the attached drive
    "cache_dir": "",                   # e.g. /mnt/cache/pitv ; empty = disabled
    "cache_max_gb": 200,
    "cache_copy_mbps": 0,              # 0 = unlimited
    "prefetch_hours": 4,
    # acquisition of missing programmes and transcoding (see docs/PLAN.md §7)
    "acquire_enabled": False,
    "acquire_dir": "",                 # empty = <cache_dir>/acquired
    "acquire_providers": ["archive"],  # archive (archive.org) and/or url
    "acquire_fill_gaps": False,        # queue missing episodes between the ones on disk
    "acquire_hours": "00:00-23:59",
    "transcode_enabled": False,
    "transcode_hours": "01:00-07:00",
    "transcode_max_height": 720,
    "transcode_bitrate_kbps": 4000,
    # maintenance
    "scan_hour": 4,
    "history_keep_days": 180,
}

DEFAULT_CHANNELS = [
    {"number": 1, "name": "PiTV One", "short_name": "One", "colour": "#e63946", "ads_enabled": 0,
     "pattern": "show", "description": "Mainstream: drama, sitcoms, light entertainment, afternoon films",
     "kind_weights": {"tv": 0.75, "movie": 0.25}},
    {"number": 2, "name": "PiTV Two", "short_name": "Two", "colour": "#457b9d", "ads_enabled": 0,
     "pattern": "show", "description": "Alternative: documentaries, cult, older films, comedy",
     "kind_weights": {"tv": 0.6, "movie": 0.4}},
    {"number": 3, "name": "PiTV Three", "short_name": "Three", "colour": "#f4a261", "ads_enabled": 1,
     "pattern": "show, ad, ad", "description": "Commercial: soaps, quiz, action drama, kids' teatime",
     "kind_weights": {"tv": 0.8, "movie": 0.2}},
    {"number": 4, "name": "PiTV Four", "short_name": "Four", "colour": "#2a9d8f", "ads_enabled": 1,
     "pattern": "show, ad, ad", "description": "Alternative commercial: comedy, imports, films, late night",
     "kind_weights": {"tv": 0.55, "movie": 0.45}},
]


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables and seed defaults. Safe to call on every start."""
    conn.executescript(SCHEMA)  # executescript commits on its own; seed inside a tx after
    _migrate(conn)
    with tx(conn):
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                         (key, json.dumps(value)))
        if conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 0:
            for ch in DEFAULT_CHANNELS:
                conn.execute(
                    "INSERT INTO channels(number, name, short_name, colour, ads_enabled, pattern,"
                    " description, kind_weights) VALUES (?,?,?,?,?,?,?,?)",
                    (ch["number"], ch["name"], ch["short_name"], ch["colour"], ch["ads_enabled"],
                     ch["pattern"], ch["description"], json.dumps(ch["kind_weights"])))


# Columns added after the first release: (table, column, DDL). Applied when missing.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("schedule", "subtitle", "TEXT NOT NULL DEFAULT ''"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction (the connection runs in autocommit mode otherwise)."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --- settings -------------------------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return DEFAULT_SETTINGS.get(key, default)
    return json.loads(row["value"])


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value)))


def all_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    out = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        out[row["key"]] = json.loads(row["value"])
    return out


# --- row helpers ------------------------------------------------------------------------

def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for key in ("genres", "overrides", "anchor_days", "era_weights", "genre_weights",
                "kind_weights", "daypart_profile", "details"):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except ValueError:
                pass
    return d


def rows_to_dicts(rows) -> list[dict[str, Any]]:
    return [row_to_dict(r) for r in rows]  # type: ignore[misc]


def effective(row: dict[str, Any]) -> dict[str, Any]:
    """Apply the JSON overrides column on top of scanned values."""
    out = dict(row)
    overrides = row.get("overrides") or {}
    if isinstance(overrides, str):
        overrides = json.loads(overrides)
    for k, v in overrides.items():
        out[k] = v
    return out


def now_ts() -> int:
    return int(time.time())


def run_log_start(conn: sqlite3.Connection, kind: str) -> int:
    cur = conn.execute("INSERT INTO run_log(kind, started_at) VALUES (?, ?)", (kind, now_ts()))
    return int(cur.lastrowid)


def run_log_finish(conn: sqlite3.Connection, run_id: int, status: str, summary: str,
                   details: list[str]) -> None:
    conn.execute("UPDATE run_log SET finished_at = ?, status = ?, summary = ?, details = ?"
                 " WHERE id = ?", (now_ts(), status, summary, json.dumps(details[-500:]), run_id))
