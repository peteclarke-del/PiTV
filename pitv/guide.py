"""What is on: schedule lookups shared by the player's on-screen guide and the web API.

Both guides read the same schedule table and must always agree, so the query, the
"current programme" rules and the merging of bands live here rather than in either
consumer. Results are plain dicts of the slot row plus the media columns below.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .db import get_setting
from .scheduler.bands import ITEM_MINUTES

# Slot columns plus the media columns either guide or the player needs. `media_kind` keeps the
# slot's own `kind` visible, and `media_path` names the original as distinct from `cache_path`.
SLOT_QUERY = ("SELECT s.*, m.path AS media_path, m.cache_path, m.cache_vcodec, m.cache_interlaced, m.origin,"
              " m.vcodec, m.interlaced, m.plot,"
              " m.year, m.certificate, m.duration, m.show_id, m.season, m.episode, m.hwdec, m.genres,"
              " m.kind AS media_kind"
              " FROM schedule s LEFT JOIN media m ON m.id = s.media_id")

# A band can hold this many short items, and `next_programmes` must read every
# slot of a block to merge it into one entry.
_SLOTS_PER_ENTRY = 40


def slot_at(conn: sqlite3.Connection, channel_id: int, ts: int) -> dict[str, Any] | None:
    """The slot (programme, advert, ident or filler) on a channel at `ts`."""
    row = conn.execute(SLOT_QUERY + " WHERE s.channel_id = ? AND s.start_ts <= ? AND s.end_ts > ?"
                       " ORDER BY s.start_ts DESC LIMIT 1", (channel_id, ts, ts)).fetchone()
    return dict(row) if row else None


def next_programmes(conn: sqlite3.Connection, channel_id: int, after: int, n: int = 12) -> list[dict[str, Any]]:
    """The next `n` guide entries (programmes only, bands merged) starting at or after `after`."""
    rows = conn.execute(SLOT_QUERY + " WHERE s.channel_id = ? AND s.start_ts >= ? AND s.kind = 'programme'"
                        " ORDER BY s.start_ts LIMIT ?", (channel_id, after, n * _SLOTS_PER_ENTRY)).fetchall()
    return collapse_blocks([dict(r) for r in rows], feature_seconds(conn))[:n]


def block_entry(conn: sqlite3.Connection, slot: dict[str, Any], ts: int) -> dict[str, Any] | None:
    """For a slot inside a band, the merged entry containing `ts`, with whatever is playing at
    `ts` in `video_title` / `video_id`. None when the slot is not part of a band."""
    if not slot.get("block"):
        return None
    rows = conn.execute(SLOT_QUERY + " WHERE s.channel_id = ? AND s.block = ? AND s.replay = ?"
                        " AND s.end_ts > ? AND s.start_ts < ? ORDER BY s.start_ts",
                        (slot["channel_id"], slot["block"], slot["replay"], ts - 12 * 3600, ts + 12 * 3600)).fetchall()
    for entry in collapse_blocks([dict(r) for r in rows], feature_seconds(conn)):
        if entry["start_ts"] <= ts < entry["end_ts"]:
            entry["video_title"] = slot["title"]
            entry["video_id"] = slot["id"]
            return entry
    return None


def feature_seconds(conn: sqlite3.Connection) -> int:
    """The length at which a band item is billed by its own name: the same boundary the
    scheduler places band items by (`band_item_max_minutes`), so the guide bills by the rule
    the builder used, and a seventeen minute item is a feature to both or to neither."""
    return 60 * int(get_setting(conn, "band_item_max_minutes", ITEM_MINUTES) or ITEM_MINUTES)


def collapse_blocks(slots: list[dict[str, Any]], feature: int = ITEM_MINUTES * 60) -> list[dict[str, Any]]:
    """Merge consecutive slots that share a `block` into one guide entry.

    A band is one programme in the guide, so a two hour "Disco Lunch" is a single entry with the
    number of programmes in it and whatever is playing in `video_title`. Where one programme
    fills most of the band, though, that programme is what the band is: a concert billed as
    "Concert" tells the viewer nothing, so the entry takes its title and keeps the band's name
    underneath. Captions inside a band (a stretch it could not fill) are not programmes."""
    out: list[dict[str, Any]] = []
    for sl in slots:
        prev = out[-1] if out else None
        if (sl.get("block") and prev and prev.get("block") == sl["block"] and prev["channel_id"] == sl["channel_id"]
                and prev["end_ts"] == sl["start_ts"] and prev.get("replay") == sl.get("replay")):
            prev["end_ts"] = sl["end_ts"]
            if sl["kind"] == "programme":
                prev["items"] += 1
                if not prev.get("video_title"):
                    prev["video_title"] = sl["title"]
                _note_longest(prev, sl)
            continue
        entry = dict(sl)
        if sl.get("block"):
            entry["items"] = 1 if sl["kind"] == "programme" else 0
            entry["video_title"] = sl["title"] if sl["kind"] == "programme" else ""
            entry["_longest"] = ("", 0)
            if sl["kind"] == "programme":
                _note_longest(entry, sl)
        out.append(entry)
    for entry in out:
        if not entry.get("block"):
            continue
        title, seconds = entry.pop("_longest", ("", 0))
        # A band led by something long is billed as that: a concert or a film inside a two hour
        # stretch is what the viewer is being offered, and "Concert" tells them nothing. Either
        # it fills half the band, or it is long enough to be a programme in its own right.
        leads = bool(title) and (seconds * 2 >= entry["end_ts"] - entry["start_ts"] or seconds >= feature)
        entry["title"] = title if leads else entry["block"]
        if leads:
            entry["subtitle"] = entry["block"]
        elif entry["items"]:
            entry["subtitle"] = ""
        # A band that placed nothing is its holding card for the whole stretch, and the card
        # says so ("More is on its way. Service resumes at ..."). Blanking the subtitle threw
        # that away and billed an empty two hours exactly as it bills a full one, so the guide
        # read "2 Wheels & More" for a stretch that was waiting for its first video.
    return out


def _note_longest(entry: dict[str, Any], slot: dict[str, Any]) -> None:
    """Remember the longest programme in a band, which may be what the band is really billing."""
    seconds = slot["end_ts"] - slot["start_ts"]
    if seconds > entry["_longest"][1]:
        entry["_longest"] = (slot["title"], seconds)
