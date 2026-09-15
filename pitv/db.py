"""SQLite schema, migrations, default data and small helpers.

The database is the source of truth for everything the admin UI can change. Time values
in the schedule are integer UNIX timestamps so lookups are cheap and unambiguous; local
times are derived for display using the configured timezone.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

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

-- Mirror of pitv_content's sources as published in its library index (docs/CONTENT_CONTRACT.md).
-- PiTV does not read these folders; the rows give imported items a source and the admin a view.
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    uid TEXT UNIQUE,               -- pitv_content's source id
    type TEXT NOT NULL CHECK (type IN ('tv', 'movie', 'advert', 'ident', 'music')),
    name TEXT NOT NULL,
    path TEXT NOT NULL,            -- root on the Pi (NAS mount or cache folder)
    remote TEXT,                   -- e.g. smb://synologynas/tvshows/
    location TEXT NOT NULL DEFAULT 'nas',        -- nas | cache
    category TEXT NOT NULL DEFAULT 'general',   -- general | sport | kids; series inherit it
    enabled INTEGER NOT NULL DEFAULT 1,
    last_indexed_at INTEGER,       -- last index import that included this source
    index_summary TEXT
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
    content TEXT NOT NULL DEFAULT 'general',   -- general | music | cartoons
    allowed_genres TEXT,                       -- JSON list; empty/NULL = any genre
    excluded_genres TEXT,                      -- JSON list
    nas_only TEXT NOT NULL DEFAULT 'inherit',  -- inherit | yes | no : may this channel schedule material not yet on disk
    family_safe_ads INTEGER NOT NULL DEFAULT 0, -- only child-friendly adverts (cartoon channels default on)
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS shows (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,   -- NULL for series fetched online
    path TEXT NOT NULL UNIQUE,     -- the index uid, or a fetched:show: key for series fetched online
    title TEXT NOT NULL,
    year INTEGER,                  -- premiered year from the index
    certificate TEXT,
    genres TEXT,                   -- JSON list
    plot TEXT,
    kids INTEGER NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT 'general',   -- general | sport | kids | cartoon
    home_channel_id INTEGER REFERENCES channels(id) ON DELETE SET NULL,   -- derived from lineup
    mode TEXT NOT NULL DEFAULT 'auto' CHECK (mode IN ('auto', 'strip', 'weekly')),
    anchor_time TEXT,              -- 'HH:MM' for strip/weekly
    anchor_days TEXT,              -- JSON list of weekday ints (0=Mon) for strip/weekly
    rest_weeks INTEGER NOT NULL DEFAULT 4,
    excluded INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    overrides TEXT NOT NULL DEFAULT '{}',   -- JSON: admin edits that beat indexed values
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,   -- NULL for material fetched online
    kind TEXT NOT NULL CHECK (kind IN ('episode', 'movie', 'advert', 'ident', 'music')),
    show_id INTEGER REFERENCES shows(id) ON DELETE CASCADE,
    season INTEGER,
    episode INTEGER,
    title TEXT NOT NULL,
    year INTEGER,
    uid TEXT UNIQUE,               -- index uid (nas:/cache:) or fetched:<wanted id>
    origin TEXT NOT NULL DEFAULT 'nas',   -- nas | cache | online: where the original lives
    path TEXT NOT NULL UNIQUE,     -- the original: NAS file (fallback only) or, for cache/online, the cache file
    cache_path TEXT,               -- the cache copy pitv_content delivered (what playback uses)
    cache_vcodec TEXT,             -- the delivered copy's codec and interlacing, which decide how it is decoded
    cache_interlaced INTEGER,
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
    artist TEXT,                   -- music videos
    concert INTEGER NOT NULL DEFAULT 0,   -- music: full concert / live show
    family_safe INTEGER NOT NULL DEFAULT 1,   -- adverts: 0 = alcohol/tobacco/adult; never on a family channel
    home_channel_id INTEGER REFERENCES channels(id) ON DELETE SET NULL,   -- movies: derived from lineup
    transient INTEGER NOT NULL DEFAULT 0,      -- fetched for a line-up entry; lives only in the cache
    excluded INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    attention TEXT,                -- reason this item needs a look, or NULL
    overrides TEXT NOT NULL DEFAULT '{}',
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
    subtitle TEXT NOT NULL DEFAULT '',    -- episode title, or '(1983) PG' for a film
    block TEXT,                           -- music: consecutive slots with the same block show as one programme
    wanted_id INTEGER                     -- placeholder for material pitv_content is to fetch; bound to media when it lands
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

CREATE TABLE IF NOT EXISTS wanted (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('episode', 'movie', 'advert', 'music')),
    title TEXT NOT NULL,
    year INTEGER,
    season INTEGER,
    episode INTEGER,
    show_id INTEGER REFERENCES shows(id) ON DELETE SET NULL,
    provider TEXT NOT NULL DEFAULT 'auto',     -- auto | url (a URL in ref pins the source)
    ref TEXT,                                  -- URL of a specific page or file for pitv_content
    genre TEXT,                                -- music: destination genre folder
    artist TEXT,
    lineup_id INTEGER,                         -- raised for a channel line-up entry
    transient INTEGER NOT NULL DEFAULT 0,      -- keep only in the cache
    status TEXT NOT NULL DEFAULT 'queued',     -- queued | done | failed (set from pitv_content reports)
    progress REAL NOT NULL DEFAULT 0,
    message TEXT,
    dest_path TEXT,
    auto INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS lineup (
    id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('show', 'movie')),
    show_id INTEGER REFERENCES shows(id) ON DELETE CASCADE,   -- library series
    media_id INTEGER REFERENCES media(id) ON DELETE CASCADE,  -- library film
    key TEXT NOT NULL UNIQUE,      -- show:<id> | movie:<id> | ext:show:<title>:<year> | ext:movie:<title>:<year>
    title TEXT NOT NULL,
    year INTEGER,
    genres TEXT,                   -- JSON list (externals)
    source TEXT NOT NULL DEFAULT 'library',   -- library | catalogue | manual
    episode_minutes INTEGER,       -- externals: expected length
    next_episode INTEGER NOT NULL DEFAULT 1,  -- externals: next episode number to request
    transient INTEGER NOT NULL DEFAULT 0,
    remove_after_airing INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    pinned INTEGER NOT NULL DEFAULT 0,        -- set by hand; rebalance leaves it alone
    notes TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS lineup_channel ON lineup(channel_id);

CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,            -- catalogue | schedule | readiness | content
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    status TEXT NOT NULL DEFAULT 'running',
    summary TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '[]'   -- JSON list of messages
);
"""

# Weekday pattern of a mid-1980s UK schedule. Weights: tv/movie by kind, kids and sport by
# category (sport weight below 1 discourages it, above 1 builds a block).
DEFAULT_DAYPARTS = [
    {"name": "Breakfast", "start": "08:00", "tv": 1.0, "movie": 0.1, "kids": 1.5, "sport": 0.1, "max_minutes": 40},
    {"name": "Morning", "start": "09:30", "tv": 1.0, "movie": 0.3, "kids": 0.5, "sport": 0.1},
    {"name": "Lunchtime", "start": "12:00", "tv": 1.0, "movie": 0.2, "kids": 0.3, "sport": 0.2, "max_minutes": 40},
    {"name": "Matinee", "start": "13:30", "tv": 0.4, "movie": 2.0, "kids": 0.5, "sport": 0.3},
    {"name": "Children's", "start": "15:30", "tv": 1.0, "movie": 0.1, "kids": 6.0, "sport": 0.05, "max_minutes": 35},
    {"name": "Early evening", "start": "17:30", "tv": 1.0, "movie": 0.1, "kids": 0.4, "sport": 0.2, "max_minutes": 40},
    {"name": "Prime time", "start": "19:00", "tv": 1.0, "movie": 0.6, "kids": 0.05, "sport": 0.3},
    {"name": "Post-watershed", "start": "21:00", "tv": 1.0, "movie": 1.2, "kids": 0.0, "sport": 0.5},
    {"name": "Late", "start": "22:30", "tv": 0.8, "movie": 1.5, "kids": 0.0, "sport": 2.5},   # Sportsnight / Midweek Sports Special
]

# Saturday: children's television all morning, Grandstand / World of Sport all afternoon,
# family entertainment early evening, prime-time variety, Match of the Day late.
DEFAULT_DAYPARTS_SATURDAY = [
    {"name": "Saturday morning", "start": "08:00", "tv": 1.0, "movie": 0.1, "kids": 6.0, "sport": 0.1, "max_minutes": 60},
    {"name": "Saturday lunchtime", "start": "12:00", "tv": 1.0, "movie": 0.2, "kids": 1.0, "sport": 2.0},
    {"name": "Saturday sport", "start": "12:30", "tv": 0.3, "movie": 0.3, "kids": 0.2, "sport": 8.0},
    {"name": "Saturday teatime", "start": "17:15", "tv": 1.0, "movie": 0.3, "kids": 1.5, "sport": 0.4},
    {"name": "Saturday prime time", "start": "19:00", "tv": 1.0, "movie": 0.8, "kids": 0.05, "sport": 0.3},
    {"name": "Saturday post-watershed", "start": "21:00", "tv": 1.0, "movie": 1.2, "kids": 0.0, "sport": 0.5},
    {"name": "Saturday late", "start": "22:15", "tv": 0.6, "movie": 1.5, "kids": 0.0, "sport": 4.0},   # Match of the Day
]

# Sunday: quiet morning, lunchtime light entertainment, afternoon sport (The Big Match) or
# the Sunday afternoon film, classic serial at teatime, drama in the evening, late film.
DEFAULT_DAYPARTS_SUNDAY = [
    {"name": "Sunday morning", "start": "08:00", "tv": 1.0, "movie": 0.2, "kids": 2.5, "sport": 0.1, "max_minutes": 60},
    {"name": "Sunday lunchtime", "start": "12:00", "tv": 1.0, "movie": 0.3, "kids": 0.5, "sport": 0.5},
    {"name": "Sunday afternoon", "start": "14:00", "tv": 0.5, "movie": 1.5, "kids": 0.3, "sport": 5.0},
    {"name": "Sunday teatime", "start": "17:00", "tv": 1.0, "movie": 0.5, "kids": 1.0, "sport": 2.0},
    {"name": "Sunday evening", "start": "19:00", "tv": 1.0, "movie": 0.6, "kids": 0.05, "sport": 0.2},
    {"name": "Sunday post-watershed", "start": "21:00", "tv": 1.0, "movie": 1.2, "kids": 0.0, "sport": 0.3},
    {"name": "Sunday late", "start": "22:30", "tv": 0.6, "movie": 1.5, "kids": 0.0, "sport": 1.0},
]

# A music channel's day: blocks by genre/decade with two full concerts. Genres match the
# folder/file names in the music share (case-insensitive); empty lists mean "anything".
DEFAULT_MUSIC_BLOCKS = [
    {"start": "08:00", "name": "Seventies Breakfast", "genres": [], "decades": [1970]},
    {"start": "09:30", "name": "Eighties Pop", "genres": ["pop", "new wave", "synth"], "decades": [1980]},
    {"start": "11:00", "name": "Nineties Morning", "genres": [], "decades": [1990]},
    {"start": "12:30", "name": "Disco & Soul", "genres": ["disco", "funk", "soul", "motown"], "decades": [1970, 1980]},
    {"start": "13:30", "name": "Concert", "genres": [], "decades": [], "concert": True},
    {"start": "15:30", "name": "Noughties", "genres": [], "decades": [2000]},
    {"start": "17:00", "name": "Eighties Chart Show", "genres": [], "decades": [1980]},
    {"start": "18:30", "name": "Rock & Metal", "genres": ["rock", "hard rock", "metal", "heavy metal", "punk", "glam"], "decades": []},
    {"start": "20:30", "name": "Concert", "genres": [], "decades": [], "concert": True},
    {"start": "22:30", "name": "Nineties Indie & Dance", "genres": ["indie", "dance", "electronic", "britpop"], "decades": [1990]},
    {"start": "23:30", "name": "Late Soul", "genres": ["soul", "r&b", "reggae", "jazz", "blues"], "decades": []},
]
CARTOON_GENRES = ["animation", "cartoon", "anime", "animated"]
# Genres that mark children's programming (kids cutoff at 21:00, kids-friendly dayparts).
KIDS_GENRES = {"animation", "children", "children's", "kids", "family", "cartoon"}

DEFAULT_SETTINGS: dict[str, Any] = {
    "timezone": "Europe/London",
    "day_start": "08:00",
    "day_end": "00:00",
    "horizon_days": 7,
    "rebuild_when_days_left": 2,
    # Programmes: anything goes, with a healthy mix either side of 1980. Adverts: 80s/90s only.
    "era_weights": {"1920-1979": 0.45, "1980-1989": 0.40, "1990-1999": 0.15},
    "unknown_year_weight": 0.2,        # programmes with no year found still get scheduled, at this weight
    "advert_era_weights": {"1980-1989": 0.85, "1990-1999": 0.15},
    "watershed": {"U": "00:00", "PG": "00:00", "12": "20:00", "12A": "20:00", "15": "21:00", "18": "22:00"},
    "tv_watershed": {"15": "21:00", "18": "22:00"},
    "show_daily_limit": 2,
    "show_repeat_penalty": 0.3,
    "unknown_movie_certificate": "15",
    "unknown_tv_certificate": "PG",
    "kids_cutoff": "21:00",
    "kind_weights": {"tv": 0.7, "movie": 0.3},
    "dayparts": DEFAULT_DAYPARTS,
    "dayparts_saturday": DEFAULT_DAYPARTS_SATURDAY,
    "dayparts_sunday": DEFAULT_DAYPARTS_SUNDAY,
    "era_pool_normalise": 0.5,         # 0 = weight per item; 1 = eras share airtime by weight regardless of library size
    "sport_back_to_back_weekends": True,
    "music_blocks": DEFAULT_MUSIC_BLOCKS,
    "music_decades": [1970, 1980, 1990, 2000],   # the music channel plays these decades only
    "adult_advert_keywords": ["beer", "lager", "ale", "cider", "wine", "whisky", "whiskey", "vodka", "gin", "rum",
                              "brandy", "cinzano", "martini", "guinness", "hofmeister", "carling", "heineken",
                              "stella", "fosters", "castlemaine", "skol", "harp", "tennents", "bacardi", "smirnoff",
                              "cigar", "cigarette", "tobacco", "hamlet", "benson", "silk cut", "marlboro", "rothmans",
                              "embassy", "condom", "durex", "lingerie", "adult", "18+", "xxx", "bookmaker", "betting",
                              "casino", "lottery"],
    "music_concert_repeat_days": 14,
    "music_video_repeat_hours": 36,
    "cartoon_genres": CARTOON_GENRES,
    "movie_repeat_days": 21,
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
    "osd_safe_margin": 0.07,           # fraction of the screen kept clear on every edge (CRT overscan)
    "osd_scale": 1.25,                 # text size multiplier; 1.25 suits a small 4:3 CRT at 576 lines
    "drm_connector": "",               # e.g. "Composite-1" or "HDMI-A-1"; empty = mpv default
    "display_aspect": "4:3",           # the physical screen shape; 720x576 PAL has non-square pixels, mpv needs to know
    "pi_hwdec": "drm-prime,v4l2m2m-copy",
    "audio_device": "auto",
    # local cache on the attached drive
    "cache_dir": "",                   # e.g. /mnt/cache/pitv ; empty = disabled
    "cache_max_gb": 600,               # pitv_content fills the cache; PiTV only evicts (LRU) under this cap
    # requests to pitv_content (which fetches, encodes and fills the cache; see docs/PLAN.md §7)
    "acquire_dir": "",                 # empty = <cache_dir>/acquired ; where pitv_content files what it fetches
    "acquire_fill_gaps": False,        # queue missing episodes between the ones on disk
    # line-ups: what each channel carries. NAS-only restricts scheduling to material on disk.
    "nas_only": True,
    "external_lead_days": 2,           # material not on disk is scheduled at least this many days ahead
    "external_episode_minutes": 30,    # expected length when a line-up entry does not say
    "external_weight": 0.7,            # relative to library programmes when choosing
    "transient_keep_days": 7,          # fetched transient files are deleted this long after airing
    # folders the admin folder picker may browse, e.g. for pitv_content source roots (plus the cache, acquire and home dirs)
    "browse_roots": ["/mnt", "/media", "/srv"],
    "content_profile": {"width": 768, "height": 576, "vcodec": "h264", "acodec": "aac", "max_bitrate_kbps": 4000,
                        "deinterlace": "if_interlaced"},
    "content_tool_url": "http://127.0.0.1:8081",   # pitv_content's local API (index, sources, settings, run, log)
    "nas_fallback": True,              # play the NAS original when the cache copy is missing or unplayable
    # resilience
    "clock_wait_seconds": 120,         # at boot, wait this long for NTP before tuning (no RTC on the Pi)
    "memory_limit_mb": 700,            # the player restarts itself above this; systemd also caps it
    # maintenance
    "readiness_hours": [6, 7],         # verify tomorrow's files exist and substitute what is missing
    "catalogue_hour": 4,            # daily import of pitv_content's library index
    "history_keep_days": 180,
}

DEFAULT_CHANNELS = [
    {"number": 1, "name": "PiTV One", "short_name": "One", "colour": "#e63946", "ads_enabled": 0,
     "pattern": "show", "description": "Mainstream: drama, sitcoms, light entertainment, afternoon films",
     "kind_weights": {"tv": 0.75, "movie": 0.25},
     "allowed_genres": ["Drama", "Comedy", "Family", "Adventure", "Romance", "Game Show", "History"]},
    {"number": 2, "name": "PiTV Two", "short_name": "Two", "colour": "#457b9d", "ads_enabled": 0,
     "pattern": "show", "description": "Alternative: documentaries, cult, older films, comedy",
     "kind_weights": {"tv": 0.6, "movie": 0.4},
     "allowed_genres": ["Documentary", "Science Fiction", "Fantasy", "Mystery", "Comedy", "Horror", "Thriller", "Sport"]},
    {"number": 3, "name": "PiTV Three", "short_name": "Three", "colour": "#f4a261", "ads_enabled": 1,
     "pattern": "show, ad, ad", "description": "Commercial: soaps, quiz, action drama, kids' teatime",
     "kind_weights": {"tv": 0.8, "movie": 0.2},
     "allowed_genres": ["Soap", "Game Show", "Action", "Crime", "Drama", "Children", "Sport", "Comedy"]},
    {"number": 4, "name": "PiTV Four", "short_name": "Four", "colour": "#2a9d8f", "ads_enabled": 1,
     "pattern": "show, ad, ad", "description": "Alternative commercial: comedy, imports, films, late night",
     "kind_weights": {"tv": 0.55, "movie": 0.45},
     "allowed_genres": ["Comedy", "Science Fiction", "Thriller", "Horror", "Documentary", "Crime", "Action"]},
    {"number": 5, "name": "PiTV Music", "short_name": "Music", "colour": "#b5179e", "ads_enabled": 0,
     "pattern": "show", "description": "Music videos by genre and decade, with two full concerts a day",
     "kind_weights": {"tv": 1.0, "movie": 0.0}, "content": "music"},
    {"number": 6, "name": "PiTV Toons", "short_name": "Toons", "colour": "#ffb703", "ads_enabled": 1,
     "pattern": "show, show, ad, ad", "description": "Cartoons all day; child-friendly adverts only",
     "kind_weights": {"tv": 1.0, "movie": 0.0}, "content": "cartoons", "family_safe_ads": 1,
     "allowed_genres": ["Animation", "Cartoon", "Anime"]},
]


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI runs a sync dependency and its endpoint on whatever
    # threadpool threads are free, so one request's connection legitimately crosses threads.
    # A connection must still never be used by two threads at once; the player keeps its own
    # on the main thread and every background thread opens its own.
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
    _migrate_settings(conn)
    with tx(conn):
        _seed_channel_genres(conn)
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
                    " description, kind_weights, content, family_safe_ads, allowed_genres) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (ch["number"], ch["name"], ch["short_name"], ch["colour"], ch["ads_enabled"],
                     ch["pattern"], ch["description"], json.dumps(ch["kind_weights"]), ch.get("content", "general"),
                     ch.get("family_safe_ads", 0), json.dumps(ch.get("allowed_genres") or [])))


# Columns added after the first release: (table, column, DDL). Applied when missing.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("schedule", "subtitle", "TEXT NOT NULL DEFAULT ''"),
    ("schedule", "block", "TEXT"),
    ("sources", "category", "TEXT NOT NULL DEFAULT 'general'"),
    ("shows", "category", "TEXT NOT NULL DEFAULT 'general'"),
    ("media", "artist", "TEXT"),
    ("media", "concert", "INTEGER NOT NULL DEFAULT 0"),
    ("channels", "content", "TEXT NOT NULL DEFAULT 'general'"),
    ("channels", "family_safe_ads", "INTEGER NOT NULL DEFAULT 0"),
    ("media", "family_safe", "INTEGER NOT NULL DEFAULT 1"),
    ("wanted", "genre", "TEXT"),
    ("wanted", "artist", "TEXT"),
    ("wanted", "lineup_id", "INTEGER"),
    ("wanted", "transient", "INTEGER NOT NULL DEFAULT 0"),
    ("channels", "allowed_genres", "TEXT"),
    ("channels", "excluded_genres", "TEXT"),
    ("channels", "nas_only", "TEXT NOT NULL DEFAULT 'inherit'"),
    ("media", "home_channel_id", "INTEGER REFERENCES channels(id) ON DELETE SET NULL"),
    ("media", "transient", "INTEGER NOT NULL DEFAULT 0"),
    ("schedule", "wanted_id", "INTEGER"),
    ("sources", "uid", "TEXT"),
    ("sources", "location", "TEXT NOT NULL DEFAULT 'nas'"),
    ("media", "uid", "TEXT"),
    ("media", "origin", "TEXT NOT NULL DEFAULT 'nas'"),
    ("media", "cache_path", "TEXT"),
    ("media", "cache_vcodec", "TEXT"),
    ("media", "cache_interlaced", "INTEGER"),
]


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return row["sql"] if row else ""


def _create_statement(table: str) -> str:
    """The CREATE TABLE statement for `table` from SCHEMA, found by matching parentheses while
    skipping `--` comments (a comment may itself contain ");")."""
    head = f"CREATE TABLE IF NOT EXISTS {table} ("
    start = SCHEMA.index(head)
    depth, i = 0, start
    while i < len(SCHEMA):
        ch = SCHEMA[i]
        if SCHEMA.startswith("--", i):
            i = SCHEMA.index("\n", i)
            continue
        if ch == "'":
            i = SCHEMA.index("'", i + 1) + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return SCHEMA[start:i + 1] + ";"
        i += 1
    raise ValueError(f"unterminated CREATE TABLE for {table}")


def _index_statements() -> list[str]:
    return [line.strip() for line in SCHEMA.splitlines() if line.strip().startswith(("CREATE INDEX", "CREATE UNIQUE INDEX"))]


def _rebuild_table(conn: sqlite3.Connection, table: str) -> None:
    """Recreate a table from SCHEMA, keeping every column the two definitions share. Runs inside
    the caller's transaction; foreign keys must already be off (SQLite ignores that switch inside
    a transaction)."""
    create_new = _create_statement(table).replace(f"CREATE TABLE IF NOT EXISTS {table} (", f"CREATE TABLE {table}__new (", 1)
    old_cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    conn.execute(f"DROP TABLE IF EXISTS {table}__new")
    conn.execute(create_new)
    new_cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table}__new)")]
    common = ", ".join(c for c in old_cols if c in new_cols)
    conn.execute(f"INSERT INTO {table}__new ({common}) SELECT {common} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}__new RENAME TO {table}")


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to SCHEMA in one transaction: a failure leaves it exactly
    as it was, and the service can start on the old schema's data after the fix."""
    conn.execute("PRAGMA foreign_keys=OFF")   # outside the transaction, or SQLite ignores it
    try:
        with tx(conn):
            _migrate_steps(conn)
            fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk_errors:
                # Logged, not fatal: a television that will not start is worse than a stale row.
                logging.getLogger("pitv.db").warning("%d rows reference missing parents after migration: %s",
                                                     len(fk_errors), [tuple(r) for r in fk_errors[:5]])
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_steps(conn: sqlite3.Connection) -> None:
    # Carry pitv_content's old transcode pointer into cache_path before the media table is
    # rebuilt without it.
    media_cols = {r["name"] for r in conn.execute("PRAGMA table_info(media)")}
    if "transcoded_path" in media_cols:
        if "cache_path" not in media_cols:
            conn.execute("ALTER TABLE media ADD COLUMN cache_path TEXT")
        conn.execute("UPDATE media SET cache_path = transcoded_path WHERE cache_path IS NULL")
    # Rebuilds: widened CHECK constraints (music), source_id made nullable for material fetched
    # online (SQLite cannot relax NOT NULL in place), and renamed scan-era columns.
    if "'music'" not in _table_sql(conn, "sources") or "last_scan_summary" in _table_sql(conn, "sources"):
        _rebuild_table(conn, "sources")
    if "'music'" not in _table_sql(conn, "media") or "transcoded_path" in media_cols \
            or "source_id INTEGER NOT NULL" in _table_sql(conn, "media"):
        _rebuild_table(conn, "media")
    if "source_id INTEGER NOT NULL" in _table_sql(conn, "shows"):
        _rebuild_table(conn, "shows")
    if _table_sql(conn, "wanted") and "'music'" not in _table_sql(conn, "wanted"):
        _rebuild_table(conn, "wanted")
    for table, column, ddl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    for stmt in _index_statements():      # rebuilt tables lose their indexes
        conn.execute(stmt)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS media_uid ON media(uid)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS sources_uid ON sources(uid)")
    conn.execute("DROP TABLE IF EXISTS probe_cache")   # PiTV no longer probes files; pitv_content does


def _seed_channel_genres(conn: sqlite3.Connection) -> None:
    """Channels created before line-ups had no genre lists. Give the shipped default channels
    (matched on number and unchanged name) their default lists once; anything renamed or added
    by the user is left alone."""
    for ch in DEFAULT_CHANNELS:
        if ch.get("allowed_genres"):
            conn.execute("UPDATE channels SET allowed_genres = ? WHERE number = ? AND name = ? AND allowed_genres IS NULL",
                         (json.dumps(ch["allowed_genres"]), ch["number"], ch["name"]))


def _migrate_settings(conn: sqlite3.Connection) -> None:
    """Drop settings that no longer exist and fill in keys added to stored daypart rows."""
    stale = [r["key"] for r in conn.execute("SELECT key FROM settings")
             if r["key"] not in DEFAULT_SETTINGS and r["key"] not in _EXTRA_SETTING_KEYS]
    if stale:
        with tx(conn):
            conn.executemany("DELETE FROM settings WHERE key = ?", [(k,) for k in stale])
    for key, defaults in (("dayparts", DEFAULT_DAYPARTS), ("dayparts_saturday", DEFAULT_DAYPARTS_SATURDAY),
                          ("dayparts_sunday", DEFAULT_DAYPARTS_SUNDAY)):
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if not row:
            continue
        try:
            stored = json.loads(row["value"])
        except ValueError:
            continue
        if not isinstance(stored, list):
            continue
        by_name = {d.get("name"): d for d in defaults}
        changed = False
        for dp in stored:
            if isinstance(dp, dict) and "sport" not in dp:
                dp["sport"] = by_name.get(dp.get("name"), {}).get("sport", 0.2)
                changed = True
        if changed:
            with tx(conn):
                conn.execute("UPDATE settings SET value = ? WHERE key = ?", (json.dumps(stored), key))


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


_EXTRA_SETTING_KEYS = {"session_secret"}


def all_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    """Defaults overlaid with stored values; keys that no longer exist are ignored."""
    out = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        if row["key"] in DEFAULT_SETTINGS or row["key"] in _EXTRA_SETTING_KEYS:
            out[row["key"]] = json.loads(row["value"])
    return out


# --- row helpers ------------------------------------------------------------------------

def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for key in ("genres", "overrides", "anchor_days", "era_weights", "genre_weights",
                "kind_weights", "daypart_profile", "details", "allowed_genres", "excluded_genres"):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except ValueError:
                pass  # not JSON after all: hand the raw text back rather than lose it
    return d


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [row_to_dict(r) for r in rows]  # type: ignore[misc]


def enabled_channels(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return rows_to_dicts(conn.execute("SELECT * FROM channels WHERE enabled = 1 ORDER BY number"))


def effective(row: dict[str, Any]) -> dict[str, Any]:
    """Apply the JSON overrides column (admin edits) on top of indexed values."""
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
