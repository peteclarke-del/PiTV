"""The wanted list: what PiTV asks pitv_content to find. PiTV never downloads or encodes;
it publishes wanted items in the content manifest and records the results from reports."""

from __future__ import annotations

import sqlite3

from .db import now_ts, tx


def queue_gaps(conn: sqlite3.Connection) -> int:
    """Queue episodes missing between the first and last episode of each season on disk.
    Three queries in all, however large the library: this runs on every maintenance pass."""
    shows = {r["id"]: r for r in conn.execute("SELECT id, title, year FROM shows WHERE missing = 0 AND excluded = 0")}
    seasons: dict[tuple[int, int], set[int]] = {}
    for e in conn.execute("SELECT show_id, season, episode FROM media WHERE show_id IS NOT NULL AND missing = 0"
                          " AND season IS NOT NULL AND episode IS NOT NULL ORDER BY show_id, season"):
        if e["show_id"] in shows:
            seasons.setdefault((e["show_id"], e["season"]), set()).add(e["episode"])
    asked = {(r["show_id"], r["season"], r["episode"]) for r in conn.execute(
        "SELECT show_id, season, episode FROM wanted WHERE show_id IS NOT NULL")}
    now = now_ts()
    rows = []
    for (show_id, season), have in seasons.items():
        if season == 0 or season > 1900:
            continue  # specials, and date-based seasons (year = season) have no fixed count
        show = shows[show_id]
        rows += [(show["title"], show["year"], season, ep, show_id, now)
                 for ep in range(min(have), max(have)) if ep not in have and (show_id, season, ep) not in asked]
    if rows:
        with tx(conn):
            conn.executemany("INSERT INTO wanted(kind, title, year, season, episode, show_id, provider, auto, created_at)"
                             " VALUES ('episode', ?, ?, ?, ?, ?, 'auto', 1, ?)", rows)
    return len(rows)
