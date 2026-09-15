"""Channel line-ups: the list of series and films each channel carries.

The line-up is the source of truth for which programme belongs to which channel. Library
items (a series or film in the catalogue) belong to exactly one channel; external
entries name material that is not on disk yet, which the scheduler may place ahead of time
and request from pitv_content. `shows.home_channel_id` and `media.home_channel_id` are
derived from this table so the scheduler's hot path stays a column comparison.

Generation assigns unassigned library items to channels by the channels' allowed and
excluded genre lists, least loaded (in hours) first. Rules never mention a channel by name
or number; everything comes from the channel rows. Every change is mirrored to a JSON file
in the data directory so the line-up survives a database rebuild and can be edited offline.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from .db import all_settings, now_ts, row_to_dict, rows_to_dicts, tx
from .player.cache import MediaCache

log = logging.getLogger("pitv.lineup")

PROGRAMME_CONTENT = ("general", "cartoons")   # channel content types that carry a line-up


def _genres_of(row: dict[str, Any]) -> set[str]:
    g = row.get("genres")
    if isinstance(g, str):
        try:
            g = json.loads(g)
        except ValueError:
            g = []
    return {str(x).lower() for x in (g or [])}


def channel_fit(channel: dict[str, Any], genres: set[str]) -> float | None:
    """How well an item suits a channel, or None when the channel must not carry it.

    The score is the share of the channel's allowed genres the item matches, so a narrowly
    defined channel attracts what it specialises in: an Animation/Children series fits a
    channel allowing only cartoon genres (1 of 3) better than a general channel that also
    allows Children (1 of 8). Items with no genre information (common with NFO-less files) are
    accepted by general channels at a token score, so they are spread by load instead of being
    dropped."""
    allowed = {str(g).lower() for g in (channel.get("allowed_genres") or [])}
    excluded = {str(g).lower() for g in (channel.get("excluded_genres") or [])}
    if excluded & genres:
        return None
    if not genres:
        return 0.01 if channel.get("content") == "general" else None
    if not allowed:
        return 0.05
    matched = len(allowed & genres)
    return matched / len(allowed) if matched else None


def channel_accepts(channel: dict[str, Any], genres: set[str]) -> bool:
    return channel_fit(channel, genres) is not None


def programme_channels(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [row_to_dict(r) for r in conn.execute(
        "SELECT * FROM channels WHERE enabled = 1 AND content IN (%s) ORDER BY number"
        % ",".join("?" * len(PROGRAMME_CONTENT)), PROGRAMME_CONTENT)]


def nas_only_for(channel: dict[str, Any], settings: dict[str, Any]) -> bool:
    override = channel.get("nas_only") or "inherit"
    if override == "yes":
        return True
    if override == "no":
        return False
    return bool(settings.get("nas_only", True))


# --- membership -------------------------------------------------------------------------

def _bucket(category: str | None) -> str:
    """Sport is balanced separately from everything else: a channel that takes the long sport
    series must still get its share of ordinary programmes, or its weekdays run dry."""
    return "sport" if category == "sport" else "general"


def _load_hours(conn: sqlite3.Connection, channel_ids: list[int]) -> dict[tuple[int, str], float]:
    load = {(cid, b): 0.0 for cid in channel_ids for b in ("general", "sport")}
    for r in conn.execute(
            "SELECT l.channel_id, COALESCE(sh.category, 'general') AS category, COALESCE(SUM(m.duration), 0) AS secs"
            " FROM lineup l LEFT JOIN shows sh ON sh.id = l.show_id"
            " LEFT JOIN media m ON (m.show_id = l.show_id OR m.id = l.media_id) AND m.missing = 0"
            " GROUP BY l.channel_id, category"):
        key = (r["channel_id"], _bucket(r["category"]))
        if key in load:
            load[key] += (r["secs"] or 0) / 3600
    return load


def _insert(conn: sqlite3.Connection, channel_id: int, kind: str, key: str, title: str, year: int | None, *,
            show_id: int | None = None, media_id: int | None = None, genres: list[str] | None = None,
            source: str = "library", transient: int = 0, episode_minutes: int | None = None,
            pinned: int = 0) -> int:
    cur = conn.execute(
        "INSERT INTO lineup(channel_id, kind, show_id, media_id, key, title, year, genres, source, transient,"
        " episode_minutes, pinned, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET channel_id = excluded.channel_id, pinned = excluded.pinned,"
        " updated_at = excluded.created_at",
        (channel_id, kind, show_id, media_id, key, title, year, json.dumps(genres or []), source, transient,
         episode_minutes, pinned, now_ts()))
    row = conn.execute("SELECT id FROM lineup WHERE key = ?", (key,)).fetchone()
    return int(row["id"]) if row else int(cur.lastrowid)


def generate(conn: sqlite3.Connection, rebalance: bool = False) -> dict[str, int]:
    """Give every library series and film a channel. With rebalance, unpinned entries are
    redistributed; otherwise only items with no entry are placed."""
    channels = programme_channels(conn)
    result = {"assigned": 0, "unmatched": 0, "channels": len(channels)}
    if not channels:
        return result
    with tx(conn):
        if rebalance:
            conn.execute("DELETE FROM lineup WHERE pinned = 0 AND source = 'library'")
        taken_shows = {r[0] for r in conn.execute("SELECT show_id FROM lineup WHERE show_id IS NOT NULL")}
        taken_films = {r[0] for r in conn.execute("SELECT media_id FROM lineup WHERE media_id IS NOT NULL")}
        load = _load_hours(conn, [c["id"] for c in channels])
        items: list[dict[str, Any]] = []
        for r in rows_to_dicts(conn.execute(
                "SELECT s.id, s.title, s.year, s.genres, s.certificate, s.kids, s.category,"
                " (SELECT COALESCE(SUM(duration), 0) FROM media m WHERE m.show_id = s.id AND m.missing = 0) AS secs"
                " FROM shows s WHERE s.excluded = 0 AND s.missing = 0")):
            if r["id"] not in taken_shows and r["secs"]:
                items.append({"kind": "show", "id": r["id"], "title": r["title"], "year": r["year"],
                              "genres": _genres_of(r), "secs": r["secs"], "cert": r["certificate"], "kids": r["kids"],
                              "bucket": _bucket(r["category"])})
        for r in rows_to_dicts(conn.execute(
                "SELECT id, title, year, genres, certificate, duration FROM media"
                " WHERE kind = 'movie' AND excluded = 0 AND missing = 0 AND duration IS NOT NULL")):
            if r["id"] not in taken_films:
                items.append({"kind": "movie", "id": r["id"], "title": r["title"], "year": r["year"],
                              "genres": _genres_of(r), "secs": r["duration"], "cert": r["certificate"], "kids": 0,
                              "bucket": "general"})
        # Big items first so the hours balance out; kids and certificates alternate as a tie-break.
        order = {"U": 0, "PG": 1, "12": 2, "12A": 2, "15": 3, "18": 4}
        items.sort(key=lambda i: (-i["secs"], -int(i["kids"] or 0), order.get((i["cert"] or "PG").upper(), 1), i["title"]))
        for it in items:
            fits = [(c, f) for c in channels if (f := channel_fit(c, it["genres"])) is not None]
            if not fits:
                result["unmatched"] += 1
                _flag(conn, it, "No channel accepts its genres (%s)" % ", ".join(sorted(it["genres"])) or "none")
                continue
            # Cheapest channel wins: its load after taking the item, scaled by how poorly it fits.
            hours = it["secs"] / 3600
            b = it["bucket"]
            target = min(fits, key=lambda cf: ((load[(cf[0]["id"], b)] + hours) / cf[1], cf[0]["number"]))[0]
            key = f"{it['kind']}:{it['id']}"
            _insert(conn, target["id"], it["kind"], key, it["title"], it["year"],
                    show_id=it["id"] if it["kind"] == "show" else None,
                    media_id=it["id"] if it["kind"] == "movie" else None, genres=sorted(it["genres"]))
            load[(target["id"], b)] += hours
            result["assigned"] += 1
        sync_home_channels(conn)
    write_mirror(conn)
    return result


def _flag(conn: sqlite3.Connection, item: dict[str, Any], note: str) -> None:
    if item["kind"] == "show":
        conn.execute("UPDATE media SET attention = ? WHERE show_id = ? AND (attention IS NULL OR attention NOT LIKE 'No channel%')",
                     (note, item["id"]))
    else:
        conn.execute("UPDATE media SET attention = ? WHERE id = ?", (note, item["id"]))


def sync_home_channels(conn: sqlite3.Connection) -> None:
    """Derive the home_channel_id columns from the line-up (NULL when not carried anywhere)."""
    conn.execute("UPDATE shows SET home_channel_id = (SELECT channel_id FROM lineup l WHERE l.show_id = shows.id AND l.enabled = 1)")
    conn.execute("UPDATE media SET home_channel_id = (SELECT channel_id FROM lineup l WHERE l.media_id = media.id AND l.enabled = 1)"
                 " WHERE kind = 'movie'")


# --- editing -------------------------------------------------------------------------------

def add(conn: sqlite3.Connection, channel_id: int, *, show_id: int | None = None, media_id: int | None = None,
        title: str | None = None, year: int | None = None, kind: str | None = None, genres: list[str] | None = None,
        transient: bool | None = None, episode_minutes: int | None = None, source: str = "manual") -> dict[str, Any]:
    """Add (or move) an entry. Library items are identified by show_id/media_id; anything else
    is an external entry that pitv_content will be asked to fetch."""
    with tx(conn):
        if show_id is not None:
            row = conn.execute("SELECT id, title, year, genres FROM shows WHERE id = ?", (show_id,)).fetchone()
            if not row:
                raise ValueError("show not found")
            lid = _insert(conn, channel_id, "show", f"show:{show_id}", row["title"], row["year"], show_id=show_id,
                          genres=sorted(_genres_of(dict(row))), pinned=1)
        elif media_id is not None:
            row = conn.execute("SELECT id, title, year, genres FROM media WHERE id = ? AND kind = 'movie'", (media_id,)).fetchone()
            if not row:
                raise ValueError("film not found")
            lid = _insert(conn, channel_id, "movie", f"movie:{media_id}", row["title"], row["year"], media_id=media_id,
                          genres=sorted(_genres_of(dict(row))), pinned=1)
        else:
            if not title or kind not in ("show", "movie"):
                raise ValueError("title and kind are required for an external entry")
            key = f"ext:{kind}:{title.strip().lower()}:{year or ''}"
            lid = _insert(conn, channel_id, kind, key, title.strip(), year, genres=genres or [], source=source,
                          transient=1 if transient is None else int(transient), episode_minutes=episode_minutes, pinned=1)
        sync_home_channels(conn)
    write_mirror(conn)
    return entry(conn, lid)


def update(conn: sqlite3.Connection, lineup_id: int, fields: dict[str, Any]) -> dict[str, Any]:
    allowed = {"channel_id", "enabled", "transient", "remove_after_airing", "episode_minutes", "next_episode", "notes",
               "pinned", "year", "genres"}
    sets = {}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("enabled", "transient", "remove_after_airing", "pinned"):
            v = int(bool(v))
        elif k == "genres":
            v = json.dumps([str(g) for g in (v or [])])
        elif k in ("channel_id", "episode_minutes", "next_episode", "year") and v is not None:
            v = int(v)
        sets[k] = v
    if "channel_id" in sets:
        sets["pinned"] = 1
    if sets:
        sets["updated_at"] = now_ts()
        with tx(conn):
            conn.execute("UPDATE lineup SET %s WHERE id = ?" % ", ".join(f"{k} = ?" for k in sets), (*sets.values(), lineup_id))
            sync_home_channels(conn)
        write_mirror(conn)
    return entry(conn, lineup_id)


def remove(conn: sqlite3.Connection, lineup_id: int) -> None:
    with tx(conn):
        conn.execute("DELETE FROM lineup WHERE id = ?", (lineup_id,))
        sync_home_channels(conn)
    write_mirror(conn)


# --- reading ---------------------------------------------------------------------------------

def entry(conn: sqlite3.Connection, lineup_id: int) -> dict[str, Any]:
    rows = entries(conn, lineup_id=lineup_id)
    if not rows:
        raise ValueError("line-up entry not found")
    return rows[0]


def entries(conn: sqlite3.Connection, channel_id: int | None = None, lineup_id: int | None = None) -> list[dict[str, Any]]:
    sql = ("SELECT l.*, c.number AS channel_number, c.name AS channel_name,"
           " (SELECT COUNT(*) FROM media m WHERE m.show_id = l.show_id AND m.missing = 0) AS episodes_on_disk,"
           " (SELECT COUNT(*) FROM wanted w WHERE w.lineup_id = l.id AND w.status IN ('queued','searching','downloading','transcoding')) AS wanted_open,"
           " (SELECT COUNT(*) FROM wanted w WHERE w.lineup_id = l.id AND w.status = 'done') AS wanted_done,"
           " (SELECT COUNT(*) FROM schedule s WHERE s.wanted_id IN (SELECT id FROM wanted WHERE lineup_id = l.id) AND s.media_id IS NULL) AS placeholders"
           " FROM lineup l JOIN channels c ON c.id = l.channel_id WHERE 1 = 1")
    params: list[Any] = []
    if channel_id is not None:
        sql += " AND l.channel_id = ?"
        params.append(channel_id)
    if lineup_id is not None:
        sql += " AND l.id = ?"
        params.append(lineup_id)
    sql += " ORDER BY c.number, l.kind, l.title"
    out = []
    for r in rows_to_dicts(conn.execute(sql, params)):
        r["on_disk"] = bool(r["show_id"] and r["episodes_on_disk"]) or bool(r["media_id"])
        # External entries were added by hand or from pitv_content's catalogue; a series keeps
        # asking for its next episode even once some have arrived.
        r["external"] = r["source"] != "library"
        out.append(r)
    return out


def options(conn: sqlite3.Connection, q: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """Choices for the add-to-line-up dropdown: library series and films, with the channel that
    currently carries each (moving is explicit). pitv_content's catalogue is merged by the API layer."""
    like = f"%{q}%"
    out = []
    for r in conn.execute(
            "SELECT s.id, s.title, s.year, l.channel_id, c.number AS channel_number,"
            " (SELECT COUNT(*) FROM media m WHERE m.show_id = s.id AND m.missing = 0) AS episodes"
            " FROM shows s LEFT JOIN lineup l ON l.show_id = s.id LEFT JOIN channels c ON c.id = l.channel_id"
            " WHERE s.missing = 0 AND s.excluded = 0 AND s.title LIKE ? ORDER BY s.title LIMIT ?", (like, limit)):
        out.append({"type": "show", "show_id": r["id"], "title": r["title"], "year": r["year"], "episodes": r["episodes"],
                    "channel_id": r["channel_id"], "channel_number": r["channel_number"], "on_disk": True})
    for r in conn.execute(
            "SELECT m.id, m.title, m.year, l.channel_id, c.number AS channel_number FROM media m"
            " LEFT JOIN lineup l ON l.media_id = m.id LEFT JOIN channels c ON c.id = l.channel_id"
            " WHERE m.kind = 'movie' AND m.missing = 0 AND m.excluded = 0 AND m.title LIKE ? ORDER BY m.title LIMIT ?", (like, limit)):
        out.append({"type": "movie", "media_id": r["id"], "title": r["title"], "year": r["year"],
                    "channel_id": r["channel_id"], "channel_number": r["channel_number"], "on_disk": True})
    return out


def genre_facets(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Genres present in the library with counts, for the channel editor's multi-select."""
    facets: dict[str, dict[str, int]] = {}
    for kind, sql in (("shows", "SELECT genres FROM shows WHERE missing = 0 AND excluded = 0"),
                      ("movies", "SELECT genres FROM media WHERE kind = 'movie' AND missing = 0 AND excluded = 0")):
        for r in conn.execute(sql):
            for g in json.loads(r["genres"] or "[]"):
                facets.setdefault(str(g), {"shows": 0, "movies": 0})[kind] += 1
    return dict(sorted(facets.items()))


# --- JSON mirror --------------------------------------------------------------------------------

def mirror_path(conn: sqlite3.Connection) -> Path | None:
    """lineups.json beside the database file, so every database has its own mirror."""
    for row in conn.execute("PRAGMA database_list"):
        if row["name"] == "main" and row["file"]:
            return Path(row["file"]).parent / "lineups.json"
    return None  # in-memory database: no mirror


def export(conn: sqlite3.Connection) -> dict[str, Any]:
    channels = {c["id"]: c for c in rows_to_dicts(conn.execute("SELECT id, number, name, content, allowed_genres, excluded_genres, nas_only FROM channels ORDER BY number"))}
    doc: dict[str, Any] = {"schema": 1, "exported_ts": now_ts(), "nas_only": all_settings(conn).get("nas_only", True), "channels": []}
    for cid, c in channels.items():
        doc["channels"].append({
            "number": c["number"], "name": c["name"], "content": c["content"],
            "allowed_genres": c["allowed_genres"] or [], "excluded_genres": c["excluded_genres"] or [], "nas_only": c["nas_only"],
            "lineup": [{k: e[k] for k in ("kind", "title", "year", "source", "transient", "remove_after_airing",
                                          "episode_minutes", "next_episode", "enabled", "pinned", "notes", "genres")}
                       for e in entries(conn, channel_id=cid)],
        })
    return doc


def write_mirror(conn: sqlite3.Connection) -> None:
    path = mirror_path(conn)
    if path is None:
        return
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(export(conn), indent=2))
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not write line-up mirror: %s", exc)


def import_doc(conn: sqlite3.Connection, doc: dict[str, Any]) -> dict[str, int]:
    """Apply a line-up document: channels matched by number; library entries matched by title
    and year against the catalogue, everything else becomes an external entry."""
    result = {"entries": 0, "unknown_channels": 0}
    channels = {r["number"]: r["id"] for r in conn.execute("SELECT id, number FROM channels")}
    with tx(conn):
        for ch in doc.get("channels") or []:
            cid = channels.get(int(ch.get("number", 0)))
            if cid is None:
                result["unknown_channels"] += 1
                continue
            fields = {}
            for k in ("allowed_genres", "excluded_genres"):
                if k in ch:
                    fields[k] = json.dumps(list(ch[k] or []))
            if ch.get("nas_only") in ("inherit", "yes", "no"):
                fields["nas_only"] = ch["nas_only"]
            if fields:
                conn.execute("UPDATE channels SET %s WHERE id = ?" % ", ".join(f"{k} = ?" for k in fields), (*fields.values(), cid))
            for e in ch.get("lineup") or []:
                title, year, kind = str(e.get("title", "")).strip(), e.get("year"), e.get("kind", "show")
                if not title:
                    continue
                show = media = None
                if kind == "show":
                    show = conn.execute("SELECT id FROM shows WHERE lower(title) = lower(?) AND (year = ? OR ? IS NULL) AND missing = 0",
                                        (title, year, year)).fetchone()
                else:
                    media = conn.execute("SELECT id FROM media WHERE kind = 'movie' AND lower(title) = lower(?) AND (year = ? OR ? IS NULL) AND missing = 0",
                                         (title, year, year)).fetchone()
                if show:
                    lid = _insert(conn, cid, "show", f"show:{show['id']}", title, year, show_id=show["id"], pinned=int(bool(e.get("pinned", 1))))
                elif media:
                    lid = _insert(conn, cid, "movie", f"movie:{media['id']}", title, year, media_id=media["id"], pinned=int(bool(e.get("pinned", 1))))
                else:
                    lid = _insert(conn, cid, kind, f"ext:{kind}:{title.lower()}:{year or ''}", title, year,
                                  genres=list(e.get("genres") or []), source=e.get("source") or "manual",
                                  transient=int(bool(e.get("transient", True))), episode_minutes=e.get("episode_minutes"), pinned=1)
                conn.execute("UPDATE lineup SET enabled = ?, remove_after_airing = ?, next_episode = ?, notes = ? WHERE id = ?",
                             (int(bool(e.get("enabled", True))), int(bool(e.get("remove_after_airing", False))),
                              int(e.get("next_episode") or 1), str(e.get("notes") or ""), lid))
                result["entries"] += 1
        sync_home_channels(conn)
    write_mirror(conn)
    return result


def restore_if_empty(conn: sqlite3.Connection) -> bool:
    """After a database rebuild, bring the line-up back from the JSON mirror."""
    if conn.execute("SELECT 1 FROM lineup LIMIT 1").fetchone():
        return False
    path = mirror_path(conn)
    if path is None or not path.exists():
        return False
    try:
        import_doc(conn, json.loads(path.read_text()))
        log.info("line-up restored from %s", path)
        return True
    except (OSError, ValueError) as exc:
        log.warning("could not restore line-up from %s: %s", path, exc)
        return False


# --- material arriving from pitv_content -------------------------------------------------------

def attach_delivery(conn: sqlite3.Connection, wanted_id: int, media_id: int) -> dict[int, int]:
    """Hand a delivered file (already a catalogue entry) to its line-up entry and to the
    placeholder slots that requested it. Runs inside the caller's transaction.

    A fetched film becomes an ordinary library entry. A fetched series stays external, so later
    placements keep requesting the next episode, but links to its show so the show is owned by
    the entry's channel and never generated onto another one. Each bound slot takes the file's
    real length (a film is never cut off). Returns {channel_id: earliest change} for the caller
    to rebuild from."""
    from .scheduler.build import slot_titles
    w = conn.execute("SELECT lineup_id FROM wanted WHERE id = ?", (wanted_id,)).fetchone()
    media = dict(conn.execute("SELECT m.*, s.title AS show_title FROM media m LEFT JOIN shows s ON s.id = m.show_id"
                              " WHERE m.id = ?", (media_id,)).fetchone())
    entry = conn.execute("SELECT * FROM lineup WHERE id = ?", (w["lineup_id"],)).fetchone() if w and w["lineup_id"] else None
    if entry is not None:
        if media["kind"] == "episode" and media["show_id"] and entry["show_id"] != media["show_id"]:
            conn.execute("DELETE FROM lineup WHERE show_id = ? AND id != ?", (media["show_id"], entry["id"]))
            conn.execute("UPDATE lineup SET show_id = ?, updated_at = ? WHERE id = ?", (media["show_id"], now_ts(), entry["id"]))
        elif media["kind"] == "movie" and entry["media_id"] is None:
            conn.execute("DELETE FROM lineup WHERE media_id = ? AND id != ?", (media_id, entry["id"]))
            conn.execute("UPDATE lineup SET media_id = ?, key = ?, source = 'library', updated_at = ? WHERE id = ?",
                         (media_id, f"movie:{media_id}", now_ts(), entry["id"]))
        sync_home_channels(conn)
    title, subtitle = slot_titles(media, media.get("show_title"))
    real = int(round(float(media.get("duration") or 0)))
    changed: dict[int, int] = {}
    for sl in conn.execute("SELECT id, channel_id, start_ts, end_ts, replay FROM schedule WHERE wanted_id = ? AND media_id IS NULL",
                           (wanted_id,)).fetchall():
        end = sl["end_ts"]
        if real and not sl["replay"] and sl["start_ts"] + real != end:
            at = min(end, sl["start_ts"] + real)
            end = sl["start_ts"] + real
            changed[sl["channel_id"]] = min(changed.get(sl["channel_id"], at), at)
        conn.execute("UPDATE schedule SET media_id = ?, end_ts = ?, title = ?, subtitle = ? WHERE id = ?",
                     (media_id, end, title, subtitle, sl["id"]))
    return changed


def remove_aired_transients(conn: sqlite3.Connection, now: int | None = None) -> int:
    """Delete fetched transient files once they have aired and nothing still schedules them:
    straight away when the entry says remove-after-airing, otherwise after transient_keep_days.
    Acquired files live outside the cache's size-capped LRU area, so this is what bounds them."""
    now = now or now_ts()
    settings = all_settings(conn)
    keep_days = int(settings.get("transient_keep_days", 7))
    rows = rows_to_dicts(conn.execute(
        "SELECT m.id, m.path, m.cache_path, COALESCE(l.remove_after_airing, 0) AS immediate,"
        " (SELECT MAX(h.ended_at) FROM history h WHERE h.media_id = m.id) AS last_aired"
        " FROM media m LEFT JOIN wanted w ON w.dest_path = m.path LEFT JOIN lineup l ON l.id = w.lineup_id"
        " WHERE m.transient = 1 AND m.missing = 0"
        " AND NOT EXISTS (SELECT 1 FROM schedule s WHERE s.media_id = m.id AND s.end_ts > ?)", (now,)))
    removed = 0
    for r in rows:
        if not r["last_aired"]:
            continue  # never removed before it has been shown
        if not r["immediate"] and now - r["last_aired"] < keep_days * 86400:
            continue
        # Only ever delete inside the cache: a NAS original is never PiTV's to remove.
        cache_root = MediaCache.from_settings(settings).dir
        for path in {r["path"], r.get("cache_path")} - {None}:
            if cache_root is None or not Path(path).resolve().is_relative_to(cache_root.resolve()):
                continue
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("could not remove transient file %s: %s", path, exc)
        with tx(conn):
            conn.execute("UPDATE media SET missing = 1 WHERE id = ?", (r["id"],))
        removed += 1
        log.info("removed transient file after airing: %s", r["path"])
    return removed
