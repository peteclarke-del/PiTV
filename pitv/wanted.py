"""The wanted list: what PiTV asks pitv_content to find. PiTV never downloads or encodes;
it publishes wanted items in the content manifest and records the results from reports.

Two kinds of request. A wanted row names one title (a missing episode, a song someone asked
for) and travels in the manifest. A band request names no title at all: it says that a stretch
of a channel's day wants short items of certain genres and decades, and asks pitv_content to go
and find some (contract section 2). Bands are how a channel of music videos is built, and a
library of concert films cannot fill one."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from . import tool_client
from .db import DEFAULT_SETTINGS, genre_list, get_setting, now_ts, rows_to_dicts, tx
from .scheduler import bands

log = logging.getLogger("pitv.wanted")


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

# --- material for bands ---------------------------------------------------------------------

BAND_ITEM_MINUTES = 4                  # rough length of a band item, for judging how many a band needs
BAND_FETCH_GAP = 20 * 3600             # leave this long before asking for the same band again
BAND_ITEM_KINDS = {"music": ("music",), "episode": ("episode",), "movie": ("movie",)}


def band_needs(conn: sqlite3.Connection, settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Bands the library cannot fill, the worst short of material first.

    A band wants short items of its own genres and decades: a two hour "Disco Lunch" needs
    perhaps thirty of them. A share of full-length concert films satisfies none of that, so the
    band falls back to whatever fits and plays the same few films all day. This counts what the
    library actually holds for each band and reports the shortfall.

    What a band asks for is configuration, not something this module knows: the channel says
    what pitv_content should fetch for it (`fetch_kind`, for instance shows, cartoons, sport or
    music), and a band may name its own instead. A channel that asks for nothing is left alone,
    however thin its bands, because its material comes from somewhere else."""
    limit = 60 * int(settings.get("band_item_max_minutes", 15))
    channels = {c["id"]: c for c in rows_to_dicts(conn.execute("SELECT * FROM channels WHERE enabled = 1"))}
    now = now_ts()
    out = []
    for channel_id, band_list in bands.load(conn).items():
        channel = channels.get(channel_id)
        if channel is None:
            continue
        for band in band_list:
            kind = band.fetch or (channel.get("fetch_kind") or "")
            item_kinds = sorted({k for b in band.kinds for k in BAND_ITEM_KINDS.get(b, ())})
            if not kind or not item_kinds:
                continue
            want = max(1, (band.minutes or 60) // BAND_ITEM_MINUTES)
            have = _matching_items(conn, item_kinds, band.genres, band.decades, limit)
            if have < want and now - (band.last_fetch_at or 0) >= BAND_FETCH_GAP:
                out.append({"band": band, "channel": channel, "kind": kind, "have": have, "want": want})
    return sorted(out, key=lambda n: n["have"] - n["want"])


def _matching_items(conn: sqlite3.Connection, kinds: list[str], genres: tuple[str, ...],
                    decades: tuple[int, ...], limit_seconds: int) -> int:
    """How many items in the library a band could actually use."""
    rows = conn.execute(
        f"SELECT genres, year FROM media WHERE kind IN ({','.join('?' * len(kinds))})"
        " AND missing = 0 AND excluded = 0 AND duration > 0 AND duration <= ?",
        (*kinds, limit_seconds)).fetchall()
    wanted = {g.lower() for g in genres}
    count = 0
    for r in rows:
        if decades and ((r["year"] or 0) // 10) * 10 not in decades:
            continue
        if wanted and wanted.isdisjoint(g.lower() for g in genre_list(r["genres"])):
            continue
        count += 1
    return count


def request_band_material(conn: sqlite3.Connection, settings: dict[str, Any]) -> dict[str, Any]:
    """Ask pitv_content for material for the band that needs it most.

    One band at a time: pitv_content runs one job at a time, and these runs fetch from YouTube,
    which objects to a crowd. The band is stamped either way, so a band whose genre nothing can
    satisfy does not block the rest night after night."""
    needs = band_needs(conn, settings)
    if not needs:
        return {"status": "ok", "asked": 0, "summary": "every band has material"}
    need = needs[0]
    band = need["band"]
    decades = sorted(band.decades)
    body: dict[str, Any] = {"mode": "catalogue", "kind": need["kind"],
                            "count": min(40, need["want"] - need["have"]),
                            "max_minutes": int(settings.get("band_item_max_minutes", 15))}
    if band.genres:
        body["genres"] = list(band.genres[:12])
    if decades:
        body["years"] = [decades[0], decades[-1] + 9]
    url = get_setting(conn, "content_tool_url") or DEFAULT_SETTINGS["content_tool_url"]
    status, payload = tool_client.request(url, "POST", "run", body=body, timeout=15)
    with tx(conn):
        conn.execute("UPDATE band SET last_fetch_at = ? WHERE id = ?", (now_ts(), band.id))
    if status == 409:
        return {"status": "ok", "asked": 0, "summary": "pitv_content is busy; will ask again"}
    if status >= 400 or not isinstance(payload, dict) or not payload.get("ok"):
        reason = (payload or {}).get("error") or (payload or {}).get("errors") or f"HTTP {status}"
        log.warning("band %s: pitv_content refused the request: %s", band.name, reason)
        return {"status": "error", "asked": 0, "summary": f"{band.name}: {reason}"}
    summary = (f"{band.name} ({need['kind']}): asked for {body['count']} "
               f"{', '.join(body.get('genres') or ['any'])} items"
               f"{' from ' + str(body['years'][0]) + ' to ' + str(body['years'][1]) if decades else ''}")
    log.info("band material: %s", summary)
    return {"status": "ok", "asked": 1, "summary": summary, "job_id": payload.get("job_id")}
