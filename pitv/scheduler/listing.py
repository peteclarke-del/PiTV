"""Radio Times style text listing of a broadcast day."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from ..db import all_settings, enabled_channels, now_ts
from .rules import broadcast_day_for, day_bounds, tz_of


def day_slots(conn: sqlite3.Connection, channel_id: int, day: date, include_replay: bool) -> list[sqlite3.Row]:
    q = ("SELECT s.*, m.year AS myear, m.certificate AS mcert, m.duration AS mduration, m.kind AS mkind"
         " FROM schedule s LEFT JOIN media m ON m.id = s.media_id"
         " WHERE s.channel_id = ? AND s.day = ?")
    if not include_replay:
        q += " AND s.replay = 0"
    q += " ORDER BY s.start_ts"
    return conn.execute(q, (channel_id, day.isoformat())).fetchall()


def print_listing(conn: sqlite3.Connection, day: str | None = None,
                  channel_numbers: list[int] | None = None, show_ads: bool = False,
                  overnight: bool = False) -> None:
    settings = all_settings(conn)
    tz = tz_of(conn)
    d = datetime.strptime(day, "%Y-%m-%d").date() if day else broadcast_day_for(now_ts(), settings, tz)
    channels = enabled_channels(conn)
    if channel_numbers:
        channels = [c for c in channels if c["number"] in channel_numbers]
    day_start, day_end, _ = day_bounds(d, settings, tz)
    print(f"=== {d.strftime('%A %d %B %Y')} ===")
    for ch in channels:
        print(f"\n--- {ch['number']} {ch['name']}  ({ch['pattern']}{', ads' if ch['ads_enabled'] else ''}) ---")
        rows = day_slots(conn, ch["id"], d, overnight)
        if not rows:
            print("  (no schedule)")
            continue
        for r in rows:
            if not show_ads and r["kind"] in ("advert", "ident"):
                continue
            start = datetime.fromtimestamp(r["start_ts"], tz)
            mins = (r["end_ts"] - r["start_ts"]) // 60
            tag = {"advert": "  [ad]", "ident": "  [ident]", "filler": "  [filler]"}.get(r["kind"], "")
            replay = " (replay)" if r["replay"] else ""
            hhmm = start.strftime("%H:%M")
            if r["replay"] or start.timestamp() < day_start:
                hhmm = start.strftime("%a %H:%M")
            sub = f"  {r['subtitle']}" if r["subtitle"] else ""
            print(f"  {hhmm:<10}{r['title']}{sub}{tag}{replay}   {mins}m")
