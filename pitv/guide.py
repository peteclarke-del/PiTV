"""What is on: schedule lookups shared by the player's on-screen guide and the web API.

Both guides read the same schedule table and must always agree, so the query, the
"current programme" rules and the merging of music blocks live here rather than in either
consumer. Results are plain dicts of the slot row plus the media columns below.
"""

from __future__ import annotations

import sqlite3
from typing import Any

# Slot columns plus the media columns either guide or the player needs. `media_kind` keeps the
# slot's own `kind` visible, and `media_path` names the original as distinct from `cache_path`.
SLOT_QUERY = ("SELECT s.*, m.path AS media_path, m.cache_path, m.cache_vcodec, m.cache_interlaced, m.origin,"
              " m.vcodec, m.interlaced, m.plot,"
              " m.year, m.certificate, m.duration, m.show_id, m.season, m.episode, m.hwdec, m.genres,"
              " m.kind AS media_kind"
              " FROM schedule s LEFT JOIN media m ON m.id = s.media_id")

# A music block can hold this many short videos, and `next_programmes` must read every
# slot of a block to merge it into one entry.
_SLOTS_PER_ENTRY = 40


def slot_at(conn: sqlite3.Connection, channel_id: int, ts: int) -> dict[str, Any] | None:
    """The slot (programme, advert, ident or filler) on a channel at `ts`."""
    row = conn.execute(SLOT_QUERY + " WHERE s.channel_id = ? AND s.start_ts <= ? AND s.end_ts > ?"
                       " ORDER BY s.start_ts DESC LIMIT 1", (channel_id, ts, ts)).fetchone()
    return dict(row) if row else None


def next_programmes(conn: sqlite3.Connection, channel_id: int, after: int, n: int = 12) -> list[dict[str, Any]]:
    """The next `n` guide entries (programmes only, music blocks merged) starting at or after `after`."""
    rows = conn.execute(SLOT_QUERY + " WHERE s.channel_id = ? AND s.start_ts >= ? AND s.kind = 'programme'"
                        " ORDER BY s.start_ts LIMIT ?", (channel_id, after, n * _SLOTS_PER_ENTRY)).fetchall()
    return collapse_blocks([dict(r) for r in rows])[:n]


def block_entry(conn: sqlite3.Connection, slot: dict[str, Any], ts: int) -> dict[str, Any] | None:
    """For a music-channel slot, the merged block entry containing `ts`, with the video playing
    at `ts` in `video_title` / `video_id`. None when the slot is not part of a block."""
    if not slot.get("block"):
        return None
    rows = conn.execute(SLOT_QUERY + " WHERE s.channel_id = ? AND s.block = ? AND s.replay = ?"
                        " AND s.end_ts > ? AND s.start_ts < ? ORDER BY s.start_ts",
                        (slot["channel_id"], slot["block"], slot["replay"], ts - 12 * 3600, ts + 12 * 3600)).fetchall()
    for entry in collapse_blocks([dict(r) for r in rows]):
        if entry["start_ts"] <= ts < entry["end_ts"]:
            entry["video_title"] = slot["title"]
            entry["video_id"] = slot["id"]
            return entry
    return None


def collapse_blocks(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive slots that share a `block` (music channel) into one guide entry
    titled after the block, with the first video in `video_title` and a count in `items`."""
    out: list[dict[str, Any]] = []
    for sl in slots:
        prev = out[-1] if out else None
        if (sl.get("block") and prev and prev.get("block") == sl["block"] and prev["channel_id"] == sl["channel_id"]
                and prev["end_ts"] == sl["start_ts"] and prev.get("replay") == sl.get("replay")):
            prev["end_ts"] = sl["end_ts"]
            prev["items"] = prev.get("items", 1) + 1
            continue
        entry = dict(sl)
        if sl.get("block"):
            entry["video_title"] = sl["title"]
            entry["title"] = sl["block"]
            entry["subtitle"] = "Music videos"
            entry["items"] = 1
        out.append(entry)
    return out
