"""Radio Times style text listing of a broadcast day."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from ..db import all_settings, enabled_channels, now_ts
from .rules import broadcast_day_for, day_bounds, tz_of

TAGS = {"advert": "  [ad]", "ident": "  [ident]", "filler": "  [filler]"}


def day_slots(conn: sqlite3.Connection, channel_id: int, day: date, include_replay: bool) -> list[sqlite3.Row]:
    q = ("SELECT start_ts, end_ts, kind, replay, title, subtitle FROM schedule"
         " WHERE channel_id = ? AND day = ?")
    if not include_replay:
        q += " AND replay = 0"
    q += " ORDER BY start_ts"
    return conn.execute(q, (channel_id, day.isoformat())).fetchall()


def print_listing(conn: sqlite3.Connection, day: str | None = None,
                  channel_numbers: list[int] | None = None, show_ads: bool = False,
                  overnight: bool = False) -> None:
    settings = all_settings(conn)
    tz = tz_of(conn)
    d = date.fromisoformat(day) if day else broadcast_day_for(now_ts(), settings, tz)
    channels = enabled_channels(conn)
    if channel_numbers:
        channels = [c for c in channels if c["number"] in channel_numbers]
    day_start = day_bounds(d, settings, tz)[0]
    print(f"=== {d.strftime('%A %d %B %Y')} ===")
    for ch in channels:
        print(f"\n--- {ch['number']} {ch['name']}  ({ch['pattern'] or 'bands alone'}) ---")
        rows = day_slots(conn, ch["id"], d, overnight)
        if not rows:
            print("  (no schedule)")
            continue
        for r in rows:
            if not show_ads and r["kind"] in ("advert", "ident"):
                continue
            start = datetime.fromtimestamp(r["start_ts"], tz)
            mins = (r["end_ts"] - r["start_ts"]) // 60
            replay = " (replay)" if r["replay"] else ""
            # Overnight replays fall on the next calendar day, so they carry the weekday.
            hhmm = start.strftime("%a %H:%M" if r["replay"] or r["start_ts"] < day_start else "%H:%M")
            sub = f"  {r['subtitle']}" if r["subtitle"] else ""
            print(f"  {hhmm:<10}{r['title']}{sub}{TAGS.get(r['kind'], '')}{replay}   {mins}m")
