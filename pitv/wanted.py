"""The wanted list: what PiTV asks pitv_content to find. PiTV never downloads or encodes;
it publishes wanted items in the content manifest and records the results from reports."""

from __future__ import annotations

import sqlite3

from .db import now_ts, tx


def queue_gaps(conn: sqlite3.Connection) -> int:
    """Queue episodes missing between the first and last episode of each season on disk."""
    added = 0
    shows = conn.execute("SELECT id, title, year FROM shows WHERE missing = 0 AND excluded = 0").fetchall()
    for show in shows:
        eps = conn.execute("SELECT season, episode FROM media WHERE show_id = ? AND missing = 0 AND season IS NOT NULL"
                           " AND episode IS NOT NULL ORDER BY season, episode", (show["id"],)).fetchall()
        by_season: dict[int, set[int]] = {}
        for e in eps:
            by_season.setdefault(e["season"], set()).add(e["episode"])
        for season, have in by_season.items():
            if season == 0 or season > 1900:
                continue  # specials, and date-based seasons (year = season) have no fixed count
            for ep in range(min(have), max(have)):
                if ep in have:
                    continue
                exists = conn.execute("SELECT 1 FROM wanted WHERE show_id = ? AND season = ? AND episode = ?",
                                      (show["id"], season, ep)).fetchone()
                if exists:
                    continue
                with tx(conn):
                    conn.execute("INSERT INTO wanted(kind, title, year, season, episode, show_id, provider, auto, created_at)"
                                 " VALUES ('episode', ?, ?, ?, ?, ?, 'auto', 1, ?)",
                                 (show["title"], show["year"], season, ep, show["id"], now_ts()))
                added += 1
    return added
