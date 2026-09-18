"""SQLite schema, migrations, default data and small helpers.

The database is the source of truth for everything the admin UI can change. Time values
in the schedule are integer UNIX timestamps so lookups are cheap and unambiguous; local
times are derived for display using the configured timezone.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import genres

log = logging.getLogger("pitv.db")

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
    path TEXT NOT NULL,            -- root as pitv_content indexes it (NAS mount or cache folder)
    mount TEXT,                    -- where this machine finds that root, when it differs; PiTV's own
    remote TEXT,                   -- e.g. smb://synologynas/tvshows/
    location TEXT NOT NULL DEFAULT 'nas',        -- nas | cache
    category TEXT NOT NULL DEFAULT 'general',   -- scheduling default: general | sport; legacy kids is imported as audience
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
    content TEXT NOT NULL DEFAULT 'general',   -- what the channel is for; general channels also take untagged material
    kids_any_time INTEGER NOT NULL DEFAULT 0,  -- children's programmes are not held to the kids cutoff
    decades TEXT,                  -- JSON list of decade start years the channel plays; empty = any
    band_item_repeat_hours INTEGER,    -- band repeat gaps; NULL follows the global settings
    band_feature_repeat_days INTEGER,
    short_episode_minutes INTEGER,     -- short-episode runs; NULL follows the global settings
    short_episode_run_minutes INTEGER,
    fetch_kind TEXT,                   -- what pitv_content should fetch for this channel's bands; NULL = nothing
    band_item_max_minutes INTEGER,     -- longest item its bands treat as one of their own; NULL = settings
    strict_matching INTEGER NOT NULL DEFAULT 0,   -- 1: only items whose genre and year are known and allowed
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
    category TEXT NOT NULL DEFAULT 'general',   -- scheduling class: general | sport
    home_channel_id INTEGER REFERENCES channels(id) ON DELETE SET NULL,   -- derived from lineup
    mode TEXT NOT NULL DEFAULT 'auto' CHECK (mode IN ('auto', 'strip', 'weekly')),
    anchor_time TEXT,              -- 'HH:MM' for strip/weekly
    anchor_days TEXT,              -- JSON list of weekday ints (0=Mon) for strip/weekly
    rest_weeks INTEGER NOT NULL DEFAULT 4,
    excluded INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    enriched TEXT NOT NULL DEFAULT '{}',    -- JSON: trusted online metadata, below admin overrides
    metadata_checked_at INTEGER,
    metadata_source TEXT,
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
    channel_hint INTEGER,          -- idents: channel number the file was made for, as indexed
    artist TEXT,                   -- music videos
    concert INTEGER NOT NULL DEFAULT 0,   -- music: full concert / live show
    family_safe INTEGER NOT NULL DEFAULT 1,   -- adverts: 0 = alcohol/tobacco/adult; never on a family channel
    home_channel_id INTEGER REFERENCES channels(id) ON DELETE SET NULL,   -- movies: from lineup; idents: see assign_ident_channels
    transient INTEGER NOT NULL DEFAULT 0,      -- fetched for a line-up entry; lives only in the cache
    excluded INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    attention TEXT,                -- reason this item needs a look, or NULL
    enriched TEXT NOT NULL DEFAULT '{}',    -- JSON: trusted online metadata, below admin overrides
    metadata_checked_at INTEGER,
    metadata_source TEXT,
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
    match TEXT,                    -- externals: the title as confirmed online in the admin (JSON; lineup.clean_match)
    created_at INTEGER NOT NULL,
    updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS lineup_channel ON lineup(channel_id);

-- Bands: a stretch of a channel's day given one title and filled with several items, such as
-- an hour of disco videos called "Disco Lunch" or a morning of cartoons. The guide shows the
-- band as one programme; the scheduler chooses what goes in it (pitv/scheduler/bands.py).
CREATE TABLE IF NOT EXISTS band (
    id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    start TEXT NOT NULL,           -- HH:MM in the broadcast day
    minutes INTEGER,               -- length; NULL runs to the next band or the end of the day
    days TEXT,                     -- JSON list of weekdays (0 = Monday); empty or NULL = every day
    fill TEXT,                     -- JSON: what may go in it (kinds, genres, decades, category, feature)
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS band_channel ON band(channel_id);

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
# The music channel a fresh install ships with, as ordinary bands: nothing in the scheduler
# knows about music, only about bands and what may fill them.
def _band(start: str, name: str, genres: list[str] | None = None, decades: list[int] | None = None,
          feature: bool = False) -> dict[str, Any]:
    return {"start": start, "name": name,
            "fill": {"kinds": ["music"], "genres": genres or [], "decades": decades or [], "feature": feature}}


# What a channel is for. A label: the scheduler follows the channel's pattern, genres, decades
# and bands, never this.
# A catalogue row PiTV may use at all: on disk and not excluded by the admin. Every query that
# picks material to schedule, list or count starts from this.
LIVE = "missing = 0 AND excluded = 0"

CHANNEL_CONTENT = ("general", "music", "cartoons", "documentaries", "films", "sport", "kids")

DEFAULT_MUSIC_BANDS = [
    _band("08:00", "Seventies Breakfast", decades=[1970]),
    _band("09:30", "Eighties Pop", ["pop", "new wave", "synth"], [1980]),
    _band("11:00", "Nineties Morning", decades=[1990]),
    _band("12:30", "Disco & Soul", ["disco", "funk", "soul", "motown"], [1970, 1980]),
    _band("13:30", "Concert", feature=True),
    _band("15:30", "Noughties", decades=[2000]),
    _band("17:00", "Eighties Chart Show", decades=[1980]),
    _band("18:30", "Rock & Metal", ["rock", "hard rock", "metal", "heavy metal", "punk", "glam"]),
    _band("20:30", "Concert", feature=True),
    _band("22:30", "Nineties Indie & Dance", ["indie", "dance", "electronic", "britpop"], [1990]),
    _band("23:30", "Late Soul", ["soul", "r&b", "reggae", "jazz", "blues"]),
]
# Genres that mark children's programming (kids cutoff at 21:00, kids-friendly dayparts).

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
    "adult_advert_keywords": ["beer", "lager", "ale", "cider", "wine", "whisky", "whiskey", "vodka", "gin", "rum",
                              "brandy", "cinzano", "martini", "guinness", "hofmeister", "carling", "heineken",
                              "stella", "fosters", "castlemaine", "skol", "harp", "tennents", "bacardi", "smirnoff",
                              "cigar", "cigarette", "tobacco", "hamlet", "benson", "silk cut", "marlboro", "rothmans",
                              "embassy", "condom", "durex", "lingerie", "adult", "18+", "xxx", "bookmaker", "betting",
                              "casino", "lottery"],
    # Bands (a titled stretch of a day filled with several items, pitv/scheduler/bands.py)
    "band_feature_repeat_days": 14,    # a long item (a concert, a film) is not repeated within this
    "band_item_repeat_hours": 36,      # nor a short one (a video, an episode) within this
    "max_break_minutes": 4,            # the longest run of adverts, however wide the gap to fill
    "short_episode_minutes": 20,       # anything shorter than a normal slot joins following episodes
    "short_episode_run_minutes": 20,   # ... until the run reaches about this length
    "band_fetch": True,                # ask pitv_content for material when a band has too little
    "band_fetch_hours": [1, 2, 3, 4, 5],   # hours it may queue top-ups; pitv_content serialises the work
    "band_item_max_minutes": 15,       # a band runs several short items; anything this long is a feature
    "band_fit_minutes": 10,            # an item "meets" a stretch when it ends within this of the stretch's end
    "band_feature_overrun_minutes": 20,  # how far past its band a feature may run when nothing closer fits
    "band_card_message": "More is on its way.",  # first line of a band's holding card when it is short of material
    "band_fetch_gap_hours": 6,         # leave this long before asking for the same band again
    "band_fetch_min": 20,              # the fewest items a top-up asks for: a run costs a search either way
    "band_fetch_max": 60,              # and the most, so one band cannot take the whole night
    "content_fetch_kinds": [],         # what pitv_content said it can fetch, kept for when it is down
    "movie_repeat_days": 21,
    "series_cadence_days": 7,          # ordinary series aim for the same weekday next week
    "series_cadence_bonus": 4.0,       # preference near that target; strips/weekly anchors are explicit
    "genre_repeat_penalty": 0.4,
    "duration_tolerance_minutes": 5,
    "start_rounding_minutes": 5,
    "advert_year_window": 3,
    "advert_repeat_penalty_hours": 6,
    "series_rest_weeks": 4,
    "weekend_kids_breakfast": True,
    "admin_password_hash": None,
    "channel_switch_static": True,
    # streaming the channels over HTTP (pitv/stream.py)
    "streaming_enabled": True,
    "stream_segment_seconds": 4,       # shorter starts sooner and drifts less; longer is steadier
    "stream_idle_seconds": 60,         # a stream stops this long after the last request for it
    "stream_max_streams": 2,           # at once; a Pi 4 re-encodes at most one comfortably
    "stream_encoder": "",              # empty: h264_v4l2m2m on the Pi, libx264 elsewhere
    # player
    "keymap": {},                      # action -> [evdev key names]; empty = built-in defaults
    "nav_keys_change_channel": True,   # up/down = channel +/- when the guide is closed (OSMC remote has no channel keys)
    "nav_keys_change_volume": True,    # left/right = volume when the guide is closed
    "badge_seconds": 5,
    "osd_safe_margin": 0.07,           # fraction of the screen kept clear on every edge (CRT overscan)
    "osd_scale": 1.25,                 # text size multiplier; 1.25 suits a small 4:3 CRT at 576 lines
    "drm_connector": "",               # e.g. "Composite-1" or "HDMI-A-1"; empty = mpv default
    "display_profile": "crt_pal",       # the set PiTV drives; see pitv/display.py
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
    "external_lead_hours": 23,         # nearer slots favour files already local/NAS
    "external_episode_minutes": 30,    # expected length when a line-up entry does not say
    "external_weight": 1.0,            # remote entries compete equally once there is time to fetch them
    "transient_keep_days": 7,          # fetched transient files are deleted this long after airing
    # folders the admin folder picker may browse, e.g. for pitv_content source roots (plus the cache, acquire and home dirs)
    "browse_roots": ["/mnt", "/media", "/srv"],
    "content_tool_url": "http://127.0.0.1:8081",   # pitv_content's local API (index, sources, settings, run, log)
    "nas_fallback": True,              # play the NAS original when the cache copy is missing or unplayable
    "player_keepalive": True,          # the web service starts the player whenever it finds it stopped
    # resilience
    "clock_wait_seconds": 120,         # at boot, wait this long for NTP before tuning (no RTC on the Pi)
    "memory_limit_mb": 700,            # the player restarts itself above this; systemd also caps it
    # maintenance
    "readiness_hours": [6, 7],         # verify tomorrow's files exist and substitute what is missing
    "catalogue_hour": 4,            # daily import of pitv_content's library index
    "history_keep_days": 180,
}
# Stored settings that are not user configuration and so have no default.
_EXTRA_SETTING_KEYS = {"session_secret"}

DEFAULT_CHANNELS = [
    {"number": 1, "name": "PiTV One", "short_name": "One", "colour": "#e63946", "ads_enabled": 0,
     "pattern": "show", "description": "Mainstream: drama, sitcoms, light entertainment, afternoon films",
     "kind_weights": {"tv": 0.75, "movie": 0.25},
     "allowed_genres": ["Drama", "Comedy", "Family", "Adventure", "Romance", "Game Show", "History"],
     "fetch_kind": "shows"},
    {"number": 2, "name": "PiTV Two", "short_name": "Two", "colour": "#457b9d", "ads_enabled": 0,
     "pattern": "show", "description": "Alternative: documentaries, cult, older films, comedy",
     "kind_weights": {"tv": 0.6, "movie": 0.4},
     "allowed_genres": ["Documentary", "Science Fiction", "Fantasy", "Mystery", "Comedy", "Horror", "Thriller", "Sport"],
     "fetch_kind": "shows"},
    {"number": 3, "name": "PiTV Three", "short_name": "Three", "colour": "#f4a261", "ads_enabled": 1,
     "pattern": "show, ad, ad", "description": "Commercial: soaps, quiz, action drama, kids' teatime",
     "kind_weights": {"tv": 0.8, "movie": 0.2},
     "allowed_genres": ["Soap", "Game Show", "Action", "Crime", "Drama", "Children", "Sport", "Comedy"],
     "fetch_kind": "shows"},
    {"number": 4, "name": "PiTV Four", "short_name": "Four", "colour": "#2a9d8f", "ads_enabled": 1,
     "pattern": "show, ad, ad", "description": "Alternative commercial: comedy, imports, films, late night",
     "kind_weights": {"tv": 0.55, "movie": 0.45},
     "allowed_genres": ["Comedy", "Science Fiction", "Thriller", "Horror", "Documentary", "Crime", "Action"],
     "fetch_kind": "shows"},
    {"number": 5, "name": "PiTV Music", "short_name": "Music", "colour": "#b5179e", "ads_enabled": 0,
     "pattern": "", "description": "Music videos by genre and decade, with two full concerts a day",
     "kind_weights": {"tv": 1.0, "movie": 0.0}, "content": "music", "bands": DEFAULT_MUSIC_BANDS,
     "decades": [1970, 1980, 1990, 2000], "fetch_kind": "music"},
    {"number": 6, "name": "PiTV Toons", "short_name": "Toons", "colour": "#ffb703", "ads_enabled": 1,
     "pattern": "show, show, ad, ad", "description": "Cartoons all day; child-friendly adverts only",
     "kind_weights": {"tv": 1.0, "movie": 0.0}, "content": "cartoons", "family_safe_ads": 1, "kids_any_time": 1,
     "allowed_genres": ["Animation", "Cartoon", "Anime", "Children"], "fetch_kind": "cartoons"},
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
    """Create tables, migrate and seed defaults. Safe to call on every start."""
    conn.executescript(SCHEMA)  # executescript commits on its own, so it runs before any tx
    _migrate(conn)
    with tx(conn):
        _migrate_settings(conn)
        _canonical_genres(conn)
        _canonical_programme_classification(conn)
        _seed_channel_genres(conn)
        _seed_fetch_kinds(conn)
        assign_ident_channels(conn)
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
        conn.executemany("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                         [(key, json.dumps(value)) for key, value in DEFAULT_SETTINGS.items()])
        if conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 0:
            for ch in DEFAULT_CHANNELS:
                channel_id = insert_row(conn, "channels", {
                    "number": ch["number"], "name": ch["name"], "short_name": ch["short_name"],
                    "colour": ch["colour"], "ads_enabled": ch["ads_enabled"], "pattern": ch["pattern"],
                    "description": ch["description"], "kind_weights": json.dumps(ch["kind_weights"]),
                    "content": ch.get("content", "general"), "family_safe_ads": ch.get("family_safe_ads", 0),
                    "kids_any_time": ch.get("kids_any_time", 0), "decades": json.dumps(ch.get("decades") or []),
                    "allowed_genres": json.dumps(ch.get("allowed_genres") or []),
                    "fetch_kind": ch.get("fetch_kind")})
                for band in ch.get("bands") or []:
                    conn.execute("INSERT INTO band(channel_id, name, start, minutes, days, fill, enabled, created_at)"
                                 " VALUES (?,?,?,?,?,?,1,?)",
                                 (channel_id, band["name"], band["start"], band.get("minutes"), "[]",
                                  json.dumps(band["fill"]), now_ts()))


# Columns added after the first release: (table, column, DDL). Applied when missing. Every name
# here is composed into ALTER TABLE, so the list is code, never data.
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
    ("sources", "mount", "TEXT"),
    ("sources", "location", "TEXT NOT NULL DEFAULT 'nas'"),
    ("media", "uid", "TEXT"),
    ("media", "origin", "TEXT NOT NULL DEFAULT 'nas'"),
    ("media", "cache_path", "TEXT"),
    ("media", "cache_vcodec", "TEXT"),
    ("media", "cache_interlaced", "INTEGER"),
    ("lineup", "match", "TEXT"),
    ("channels", "kids_any_time", "INTEGER NOT NULL DEFAULT 0"),
    ("channels", "decades", "TEXT"),
    ("channels", "band_item_repeat_hours", "INTEGER"),
    ("channels", "band_feature_repeat_days", "INTEGER"),
    ("channels", "short_episode_minutes", "INTEGER"),
    ("channels", "short_episode_run_minutes", "INTEGER"),
    ("band", "last_fetch_at", "INTEGER"),
    ("channels", "fetch_kind", "TEXT"),
    ("channels", "band_item_max_minutes", "INTEGER"),
    ("channels", "strict_matching", "INTEGER NOT NULL DEFAULT 0"),
    ("shows", "enriched", "TEXT NOT NULL DEFAULT '{}'"),
    ("shows", "metadata_checked_at", "INTEGER"),
    ("shows", "metadata_source", "TEXT"),
    ("media", "enriched", "TEXT NOT NULL DEFAULT '{}'"),
    ("media", "metadata_checked_at", "INTEGER"),
    ("media", "metadata_source", "TEXT"),
]


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return row["sql"] if row else ""


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({_ident(table)})")]


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
    a transaction). `table` is always one of the literals in `_migrate_steps`."""
    table = _ident(table)
    create_new = _create_statement(table).replace(f"CREATE TABLE IF NOT EXISTS {table} (", f"CREATE TABLE {table}__new (", 1)
    old_cols = _columns(conn, table)
    conn.execute(f"DROP TABLE IF EXISTS {table}__new")
    conn.execute(create_new)
    new_cols = set(_columns(conn, f"{table}__new"))
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
                log.warning("%d rows reference missing parents after migration: %s",
                            len(fk_errors), [tuple(r) for r in fk_errors[:5]])
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_steps(conn: sqlite3.Connection) -> None:
    # Carry pitv_content's old transcode pointer into cache_path before the media table is
    # rebuilt without it.
    media_cols = set(_columns(conn, "media"))
    if "transcoded_path" in media_cols:
        if "cache_path" not in media_cols:
            conn.execute("ALTER TABLE media ADD COLUMN cache_path TEXT")
        conn.execute("UPDATE media SET cache_path = transcoded_path WHERE cache_path IS NULL")
    # Rebuilds: widened CHECK constraints (music), source_id made nullable for material fetched
    # online (SQLite cannot relax NOT NULL in place), and renamed scan-era columns.
    sources_sql, media_sql = _table_sql(conn, "sources"), _table_sql(conn, "media")
    if "'music'" not in sources_sql or "last_scan_summary" in sources_sql:
        _rebuild_table(conn, "sources")
    if "'music'" not in media_sql or "transcoded_path" in media_cols or "source_id INTEGER NOT NULL" in media_sql:
        _rebuild_table(conn, "media")
    if "source_id INTEGER NOT NULL" in _table_sql(conn, "shows"):
        _rebuild_table(conn, "shows")
    wanted_sql = _table_sql(conn, "wanted")
    if wanted_sql and "'music'" not in wanted_sql:
        _rebuild_table(conn, "wanted")
    for table, column, ddl in MIGRATIONS:
        if column not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {_ident(table)} ADD COLUMN {_ident(column)} {ddl}")
    for stmt in _index_statements():      # rebuilt tables lose their indexes
        conn.execute(stmt)
    # Indexes on columns MIGRATIONS may have just added: created here, once the columns exist,
    # rather than in SCHEMA, which runs before any migration.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS media_uid ON media(uid)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS sources_uid ON sources(uid)")
    # Placeholder slots are found by their request: binding a delivery, line-up progress and
    # withdrawing requests. Few slots have one, so the index holds only those.
    conn.execute("CREATE INDEX IF NOT EXISTS schedule_wanted ON schedule(wanted_id) WHERE wanted_id IS NOT NULL")
    # Tables of work PiTV no longer does itself: pitv_content probes and transcodes.
    conn.execute("DROP TABLE IF EXISTS probe_cache")
    conn.execute("DROP TABLE IF EXISTS transcode_queue")
    _migrate_music_blocks(conn)


def _migrate_music_blocks(conn: sqlite3.Connection) -> None:
    """The music channel's blocks become ordinary bands: the scheduler no longer knows what a
    music channel is. Runs once, on a database that still has the old setting."""
    row = conn.execute("SELECT value FROM settings WHERE key = 'music_blocks'").fetchone()
    if row is None or conn.execute("SELECT COUNT(*) FROM band").fetchone()[0]:
        return
    try:
        blocks = json.loads(row[0]) or []
        decades_row = conn.execute("SELECT value FROM settings WHERE key = 'music_decades'").fetchone()
        channel_decades = [int(d) for d in json.loads(decades_row[0])] if decades_row else []
    except (TypeError, ValueError):
        return
    conn.execute("UPDATE channels SET kids_any_time = 1 WHERE content = 'cartoons'")
    if channel_decades:      # the old music_decades setting becomes the channel's own
        conn.execute("UPDATE channels SET decades = ? WHERE content = 'music' AND decades IS NULL",
                     (json.dumps(channel_decades),))
    channels = conn.execute("SELECT id FROM channels WHERE content = 'music'").fetchall()
    for (channel_id,) in channels:
        for b in blocks:
            if not isinstance(b, dict) or not b.get("start"):
                continue
            fill = {"kinds": ["music"], "genres": [str(g).lower() for g in (b.get("genres") or [])],
                    "decades": [int(d) for d in (b.get("decades") or [])] or channel_decades,
                    "feature": bool(b.get("concert"))}
            conn.execute("INSERT INTO band(channel_id, name, start, minutes, days, fill, enabled, created_at)"
                         " VALUES (?,?,?,?,?,?,1,?)",
                         (channel_id, str(b.get("name") or "Music"), str(b["start"]), None, "[]",
                          json.dumps(fill), now_ts()))
        # Its day is its bands; the pattern would otherwise try to place programmes it has none of.
        conn.execute("UPDATE channels SET pattern = '' WHERE id = ?", (channel_id,))
    if channels:
        log.info("music blocks moved to bands on %d channel(s)", len(channels))


# Every column and JSON field holding genres: (table, column) for plain JSON lists, and the
# fields inside `band.fill` and the `overrides` blobs, which hold a list under a key.
_GENRE_COLUMNS = (("shows", "genres"), ("media", "genres"), ("lineup", "genres"),
                  ("channels", "allowed_genres"), ("channels", "excluded_genres"))


def _canonical_genres(conn: sqlite3.Connection) -> None:
    """Bring genres already stored to the one spelling PiTV now uses.

    Rows written before the vocabulary existed hold whatever their source called a genre, so a
    channel allowing Children would still miss a series tagged Kids until its next import. This
    rewrites them once, in place, and is a no-operation afterwards."""
    for table, column in _GENRE_COLUMNS:
        for row in conn.execute(f"SELECT id, {_ident(column)} AS g FROM {_ident(table)}"
                                f" WHERE {_ident(column)} IS NOT NULL AND {_ident(column)} != ''").fetchall():
            names = genre_list(row["g"])
            after = json.dumps(names)
            if after != row["g"]:
                conn.execute(f"UPDATE {_ident(table)} SET {_ident(column)} = ? WHERE id = ?", (after, row["id"]))
    for table in ("shows", "media"):
        for column in ("enriched", "overrides"):
            for row in conn.execute(f"SELECT id, {_ident(column)} AS doc FROM {_ident(table)}"
                                    f" WHERE {_ident(column)} IS NOT NULL AND {_ident(column)} != ''").fetchall():
                try:
                    doc = json.loads(row["doc"])
                except ValueError:
                    continue
                if not isinstance(doc, dict) or "genres" not in doc:
                    continue
                doc["genres"] = genre_list(doc["genres"])
                after = json.dumps(doc)
                if after != row["doc"]:
                    conn.execute(f"UPDATE {_ident(table)} SET {_ident(column)} = ? WHERE id = ?",
                                 (after, row["id"]))
    for row in conn.execute("SELECT id, fill FROM band WHERE fill IS NOT NULL AND fill != ''").fetchall():
        try:
            fill = json.loads(row["fill"])
        except ValueError:
            continue
        if not isinstance(fill, dict):
            continue
        fill["genres"] = genre_list(fill.get("genres"))
        after = json.dumps(fill)
        if after != row["fill"]:
            conn.execute("UPDATE band SET fill = ? WHERE id = ?", (after, row["id"]))


def _canonical_programme_classification(conn: sqlite3.Connection) -> None:
    """Migrate the old mixed category model to orthogonal scheduling/audience/genre fields.

    ``kids`` and ``cartoon`` used to be accepted in ``shows.category`` even though the scheduler
    actually uses the children flag and genres for those facts.  Preserve their meaning in
    ``kids`` and leave only general/sport in the scheduling-class column.
    """
    for row in conn.execute("SELECT id, category, genres, kids FROM shows").fetchall():
        names = genre_list(row["genres"])
        legacy_child = str(row["category"] or "").casefold() in ("kids", "cartoon")
        kids = int(bool(row["kids"]) or legacy_child or genres.is_childrens(names))
        category = genres.scheduling_class(row["category"], names)
        if kids != row["kids"] or category != row["category"]:
            conn.execute("UPDATE shows SET kids = ?, category = ? WHERE id = ?", (kids, category, row["id"]))
    # Scheduling class is a direct admin/index field, never online enrichment. Early development
    # builds briefly wrote it into the lower-precedence metadata layer; remove those stale copies.
    for row in conn.execute("SELECT id, enriched FROM shows WHERE enriched IS NOT NULL AND enriched != '{}'"):
        try:
            enriched = json.loads(row["enriched"])
        except (TypeError, ValueError):
            continue
        if isinstance(enriched, dict) and "category" in enriched:
            enriched.pop("category", None)
            conn.execute("UPDATE shows SET enriched = ? WHERE id = ?", (json.dumps(enriched), row["id"]))


def _seed_channel_genres(conn: sqlite3.Connection) -> None:
    """Channels created before line-ups had no genre lists. Give the shipped default channels
    (matched on number and unchanged name) their default lists once; anything renamed or added
    by the user is left alone."""
    conn.executemany("UPDATE channels SET allowed_genres = ? WHERE number = ? AND name = ? AND allowed_genres IS NULL",
                     [(json.dumps(ch["allowed_genres"]), ch["number"], ch["name"])
                      for ch in DEFAULT_CHANNELS if ch.get("allowed_genres")])
    # `Children` is the catalogue/provider genre used for non-animated children's series. It
    # belongs in the cartoons channel's programme vocabulary alongside Animation/Cartoon/Anime.
    # Upgrade only the shipped old trio, leaving customised genre lists untouched.
    old = {"animation", "cartoon", "anime"}
    for row in conn.execute("SELECT id, allowed_genres FROM channels WHERE content = 'cartoons'"):
        genres = genre_list(row["allowed_genres"])
        if {g.lower() for g in genres} == old:
            conn.execute("UPDATE channels SET allowed_genres = ? WHERE id = ?",
                         (json.dumps([*genres, "Children"]), row["id"]))


# What a channel of each content label would ask pitv_content to fetch for its bands. A seed for
# channels that predate the column, and for new ones; every channel can be set by hand afterwards.
FETCH_KIND_FOR_CONTENT = {"music": "music", "cartoons": "cartoons", "kids": "cartoons", "sport": "sport",
                          "general": "shows", "documentaries": "shows", "films": ""}


def _seed_fetch_kinds(conn: sqlite3.Connection) -> None:
    """Channels created before bands could ask for material have nothing in `fetch_kind`. Fill it
    in from what each channel says it carries; a channel set to nothing asks for nothing."""
    for content, kind in FETCH_KIND_FOR_CONTENT.items():
        if kind:
            conn.execute("UPDATE channels SET fetch_kind = ? WHERE content = ? AND fetch_kind IS NULL",
                         (kind, content))


def _migrate_settings(conn: sqlite3.Connection) -> None:
    """Drop settings that no longer exist and fill in keys added to stored daypart rows."""
    # The old scheduler used calendar-day lead time and yesterday's slot. The replacement rules
    # are hour-accurate and target the following week. Preserve a customised bonus value, but the
    # 23-hour fetch boundary is deliberate rather than a conversion of the former two-day default.
    old_bonus = conn.execute("SELECT value FROM settings WHERE key = 'same_slot_bonus'").fetchone()
    if old_bonus and not conn.execute("SELECT 1 FROM settings WHERE key = 'series_cadence_bonus'").fetchone():
        conn.execute("INSERT INTO settings(key, value) VALUES ('series_cadence_bonus', ?)", (old_bonus["value"],))
    conn.executemany("DELETE FROM settings WHERE key = ?",
                     [(r["key"],) for r in conn.execute("SELECT key FROM settings").fetchall()
                      if r["key"] not in DEFAULT_SETTINGS and r["key"] not in _EXTRA_SETTING_KEYS])
    for key, defaults in (("dayparts", DEFAULT_DAYPARTS), ("dayparts_saturday", DEFAULT_DAYPARTS_SATURDAY),
                          ("dayparts_sunday", DEFAULT_DAYPARTS_SUNDAY)):
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        try:
            stored = json.loads(row["value"]) if row else None
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
            conn.execute("UPDATE settings SET value = ? WHERE key = ?", (json.dumps(stored), key))


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction (the connection runs in autocommit mode otherwise). Nested use joins
    the outer transaction."""
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
    """Defaults overlaid with stored values; keys that no longer exist are ignored."""
    out = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        if row["key"] in DEFAULT_SETTINGS or row["key"] in _EXTRA_SETTING_KEYS:
            out[row["key"]] = json.loads(row["value"])
    return out


# --- rows ----------------------------------------------------------------------------------

_JSON_COLUMNS = ("genres", "enriched", "overrides", "anchor_days", "era_weights", "genre_weights",
                 "kind_weights", "daypart_profile", "details", "allowed_genres", "excluded_genres",
                 "decades")


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """A row as a dict with its JSON columns decoded."""
    if row is None:
        return None
    d = dict(row)
    for key in _JSON_COLUMNS:
        if isinstance(d.get(key), str):
            try:
                d[key] = json.loads(d[key])
            except ValueError:
                pass  # not JSON after all: hand the raw text back rather than lose it
    return d


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [row_to_dict(r) for r in rows]  # type: ignore[misc]


_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


def _ident(name: str) -> str:
    """A table or column name about to be composed into SQL. Callers only pass names written in
    this package, never keys from an input document; the check turns a slip into an error
    instead of SQL."""
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"not an SQL identifier: {name!r}")
    return name


@lru_cache(maxsize=64)
def _insert_sql(table: str, columns: tuple[str, ...]) -> str:
    return (f"INSERT INTO {_ident(table)} ({', '.join(_ident(c) for c in columns)})"
            f" VALUES ({', '.join('?' * len(columns))})")


@lru_cache(maxsize=64)
def _update_sql(table: str, columns: tuple[str, ...]) -> str:
    return f"UPDATE {_ident(table)} SET {', '.join(f'{_ident(c)} = ?' for c in columns)} WHERE id = ?"


def insert_row(conn: sqlite3.Connection, table: str, fields: dict[str, Any]) -> int:
    """INSERT one row from {column: value}; values are always bound. Returns the new id."""
    cur = conn.execute(_insert_sql(table, tuple(fields)), tuple(fields.values()))
    return int(cur.lastrowid)


def update_row(conn: sqlite3.Connection, table: str, row_id: int, fields: dict[str, Any]) -> None:
    """UPDATE the row `row_id` from {column: value}; values are always bound."""
    if fields:
        conn.execute(_update_sql(table, tuple(fields)), (*fields.values(), row_id))


def find_id(conn: sqlite3.Connection, table: str, column: str, value: Any) -> int | None:
    """The id of the row whose `column` equals `value`, or None."""
    row = conn.execute(f"SELECT id FROM {_ident(table)} WHERE {_ident(column)} = ?", (value,)).fetchone()
    return int(row["id"]) if row else None


def assign_ident_channels(conn: sqlite3.Connection) -> None:
    """Give each ident without a channel the one whose number its file was made for. This runs
    once per ident: afterwards the channel is the admin's to change and follows the channel's
    id, so renumbering channels never strands an ident on the wrong one."""
    conn.execute("UPDATE media SET home_channel_id = (SELECT c.id FROM channels c WHERE c.number = media.channel_hint)"
                 " WHERE kind = 'ident' AND home_channel_id IS NULL AND channel_hint IS NOT NULL")


def enabled_channels(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return rows_to_dicts(conn.execute("SELECT * FROM channels WHERE enabled = 1 ORDER BY number"))


def effective(row: dict[str, Any]) -> dict[str, Any]:
    """Apply trusted online enrichment, then admin edits, on top of indexed values."""
    enriched = row.get("enriched") or {}
    if isinstance(enriched, str):
        enriched = json.loads(enriched)
    overrides = row.get("overrides") or {}
    if isinstance(overrides, str):
        overrides = json.loads(overrides)
    return {**row, **enriched, **overrides}


# --- values from documents another process wrote --------------------------------------------
# pitv_content's index and reports, and uploaded line-up files, are JSON from outside this
# process. These read one field each: a wrong type becomes None (or the default), so one bad
# value costs that value, not the whole import.

_SQLITE_INT_MAX = 2 ** 63 - 1


def as_float(value: Any) -> float | None:
    """A finite number (numeric text included); booleans, NaN and infinity are None."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def as_int(value: Any) -> int | None:
    """As `as_float`, truncated, and only when SQLite can store it."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value if abs(value) <= _SQLITE_INT_MAX else None
    number = as_float(value)
    return int(number) if number is not None and abs(number) <= _SQLITE_INT_MAX else None


def as_text(value: Any) -> str | None:
    """Text as it is, and numbers in their usual form (a certificate sent as 12 is "12")."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def as_bool(value: Any, default: bool = False) -> bool:
    """A yes/no: booleans and numbers as usual, text "1", "true", "yes" or "on"; else `default`."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def genre_list(value: Any) -> list[str]:
    """Genres as a list of names, from a list, a JSON column or a single name, each brought to
    the one spelling PiTV uses (`pitv/genres.py`): "Sci-Fi" and "science fiction" both arrive as
    Science Fiction, "Kids" as Children. Every genre PiTV stores or compares comes through here,
    so a channel that allows Children cannot miss a series tagged Kids."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            decoded = value
        value = decoded if isinstance(decoded, list) else [decoded if isinstance(decoded, str) else value]
    if not isinstance(value, list):
        return []
    return genres.canonical_all([name for g in value if (name := as_text(g))])


# --- files beside the database -------------------------------------------------------------

def data_path(conn: sqlite3.Connection, name: str) -> Path | None:
    """`name` in the database's directory, so every database has its own; None in memory."""
    for row in conn.execute("PRAGMA database_list"):
        if row["name"] == "main" and row["file"]:
            return Path(row["file"]).parent / name
    return None


def write_data_file(conn: sqlite3.Connection, name: str, build: Callable[[], Any]) -> None:
    """Rewrite the JSON file `name` beside the database. The document is written to a temporary
    file, synced and renamed over the old one, so a power cut leaves one version or the other,
    never a torn file. `build` runs only when there is somewhere to write. Failures are logged:
    the database stays the source of truth."""
    path = data_path(conn, name)
    if path is None:
        return
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(build(), f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not write %s: %s", path, exc)


# --- run log ---------------------------------------------------------------------------------

def now_ts() -> int:
    return int(time.time())


def run_log_start(conn: sqlite3.Connection, kind: str) -> int:
    cur = conn.execute("INSERT INTO run_log(kind, started_at) VALUES (?, ?)", (kind, now_ts()))
    return int(cur.lastrowid)


def run_log_finish(conn: sqlite3.Connection, run_id: int, status: str, summary: str,
                   details: list[str]) -> None:
    conn.execute("UPDATE run_log SET finished_at = ?, status = ?, summary = ?, details = ?"
                 " WHERE id = ?", (now_ts(), status, summary, json.dumps(details[-500:]), run_id))
