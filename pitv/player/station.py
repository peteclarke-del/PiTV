"""What a television's front end asks of the station that schedules for it.

PiTV runs all in one, and also with the front end on a small machine of its own that only plays
the station's streams (docs/SPLIT_PLAN.md). The player is the front end in both. Everything it
needs to know, as opposed to everything it draws and plays, comes through `Station`, so that it
never assumes a database or media files are beside it.

`LocalStation` answers from the database on the same machine, which is the all-in-one install
and exactly what the player did before this seam existed. A station reached over the network
answers the same questions from its HTTP API; that implementation arrives with the receiver.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import tzinfo
from pathlib import Path
from typing import Any, Protocol

from .. import db as dbm
from ..db import all_settings, enabled_channels
from ..guide import block_entry, next_programmes, slot_at
from ..scheduler.clock import tz_of

log = logging.getLogger("pitv.player")

# What a station that cannot answer raises. The player carries on with what it last knew: a
# database that is locked for a moment, or a station across the network that is away, must never
# stop a television.
UNAVAILABLE: tuple[type[BaseException], ...] = (sqlite3.Error, OSError)


class Station(Protocol):
    """The questions a front end asks. Every answer is plain data: dicts as the guide gives them."""

    def settings(self) -> dict[str, Any]:
        """The settings, read afresh (the owner may have changed them)."""

    def timezone(self) -> tzinfo:
        """The broadcast timezone every on-screen time is shown in."""

    def channels(self) -> list[dict[str, Any]]:
        """The enabled channels, in number order."""

    def slot_at(self, channel_id: int, ts: int) -> dict[str, Any] | None:
        """What is on a channel at a moment: a programme, an advert, an ident or a card."""

    def block_entry(self, slot: dict[str, Any], ts: int) -> dict[str, Any] | None:
        """Where a band's slot sits in its band, for the badge and the guide."""

    def next_programmes(self, channel_id: int, after: int, count: int) -> list[dict[str, Any]]:
        """The next `count` programmes on a channel starting after a moment."""

    def watched(self, slot: dict[str, Any], at: int) -> int | None:
        """Record that a programme went on air on this television; returns a handle to close it."""

    def watched_until(self, handle: int, at: int) -> None:
        """Close a record opened by `watched`."""

    def close(self) -> None:
        """Release whatever the station holds open."""


class LocalStation:
    """The station on this machine: the schedule database, read directly.

    History is bookkeeping. A locked or failing database must never stop a television, so the
    two writes log and carry on."""

    def __init__(self, db_path: Path) -> None:
        self.conn = dbm.connect(db_path)
        dbm.init_db(self.conn)

    def settings(self) -> dict[str, Any]:
        return all_settings(self.conn)

    def timezone(self) -> tzinfo:
        return tz_of(self.conn)

    def channels(self) -> list[dict[str, Any]]:
        return enabled_channels(self.conn)

    def slot_at(self, channel_id: int, ts: int) -> dict[str, Any] | None:
        return slot_at(self.conn, channel_id, ts)

    def block_entry(self, slot: dict[str, Any], ts: int) -> dict[str, Any] | None:
        return block_entry(self.conn, slot, ts)

    def next_programmes(self, channel_id: int, after: int, count: int) -> list[dict[str, Any]]:
        return next_programmes(self.conn, channel_id, after, count)

    def watched(self, slot: dict[str, Any], at: int) -> int | None:
        try:
            with dbm.tx(self.conn):
                cur = self.conn.execute("INSERT INTO history(channel_id, media_id, schedule_id, started_at, title) VALUES (?,?,?,?,?)",
                                        (slot["channel_id"], slot["media_id"], slot["id"], at, slot["title"]))
            return int(cur.lastrowid)
        except sqlite3.Error as exc:
            log.warning("could not record history for '%s': %s", slot["title"], exc)
            return None

    def watched_until(self, handle: int, at: int) -> None:
        try:
            with dbm.tx(self.conn):
                self.conn.execute("UPDATE history SET ended_at = ? WHERE id = ?", (at, handle))
        except sqlite3.Error as exc:
            log.warning("could not close history entry %s: %s", handle, exc)

    def close(self) -> None:
        self.conn.close()
