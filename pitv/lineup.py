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
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from . import genres as genre_rules
from .db import (
    LIVE,
    all_settings,
    as_bool,
    as_int,
    as_text,
    data_path,
    effective,
    genre_list,
    get_setting,
    now_ts,
    rows_to_dicts,
    tx,
    update_row,
    write_data_file,
)
from .scheduler.rules import in_decades, normalise_cert, parse_pattern
from .scheduler.slots import slot_titles

log = logging.getLogger("pitv.lineup")

PROGRAMME_TOKENS = {"show", "tv", "movie"}   # a channel carrying one of these has a line-up
MIRROR = "lineups.json"
EXTERNAL_SOURCES = ("catalogue", "manual")  # entries not generated from the catalogue (source 'library')
_MATCH_SOURCE = re.compile(r"^[a-z0-9_-]{1,40}$")
_IMDB = re.compile(r"^tt\d{5,10}$")


def clean_match(value: Any) -> dict[str, str] | None:
    """The identity a title was confirmed as online (pitv_content's lookup, contract section 8):
    {source, id, url?, imdb?}. Anything else in the document is dropped; None when unusable."""
    if not isinstance(value, dict):
        return None
    source, ident = as_text(value.get("source")), as_text(value.get("id"))
    if not source or not _MATCH_SOURCE.match(source) or not ident or len(ident) > 100:
        return None
    out = {"source": source, "id": ident}
    url = as_text(value.get("url"))
    if url and len(url) <= 500 and url.startswith(("https://", "http://")):
        out["url"] = url
    imdb = as_text(value.get("imdb"))
    if imdb and _IMDB.match(imdb):
        out["imdb"] = imdb
    return out


FACET_KINDS = ("episode", "movie", "music")   # the kinds a band can draw on; see facets()


def _genre_set(value: Any) -> set[str]:
    return {g.lower() for g in genre_list(value)}


def claimed_types(channels: list[dict[str, Any]]) -> frozenset[str]:
    """The programme types some themed channel among `channels` takes as its own. A type nobody
    claims (sport with no sports channel, documentaries with no documentary channel) stays
    with the general channels, so removing a themed channel never orphans its material."""
    return frozenset(t for c in channels if (c.get("content") or "general") != "general"
                     for t in genre_rules.THEME_TYPES.get(c.get("content") or "", ()))


def channel_fit(channel: dict[str, Any], genres: set[str], ptype: str, *, kids: bool = False,
                claimed: frozenset[str] = frozenset(), year: int | None = None, end_year: int | None = None) -> float | None:
    """How well an item suits a channel, or None when the channel must not carry it.

    What the item is decides whether it belongs (docs/PLAN.md section 4.2): a themed channel
    takes its own type and nothing else, whatever the item's genres say, and a children's
    channel takes what is flagged for children. The general channels take films and series,
    and any type no themed channel claims.

    Genres only steer among the general channels. Both sides are read in PiTV's own genre
    spelling. The score is the share of the channel's allowed genres the item matches, so a
    drama goes to the general channel that specialises in drama; an item with no genre
    information (common with NFO-less files) is accepted at a token score and spread by load.
    A channel's excluded genres bar an item anywhere, and so do its decades: the scheduler never
    airs what falls outside them (`rules.in_decades`, the same test), so a title placed there
    would sit on the shelf unused while the channel ran short."""
    excluded = {str(g).lower() for g in (channel.get("excluded_genres") or [])}
    if excluded & genres:
        return None
    decades = [int(d) for d in (channel.get("decades") or []) if str(d).isdigit()]
    if not in_decades(year, decades, end_year):
        return None
    theme = channel.get("content") or "general"
    if theme != "general":
        own = genre_rules.THEME_TYPES.get(theme)
        return 1.0 if (ptype in own if own is not None else kids) else None
    if ptype not in genre_rules.THEME_TYPES["general"] and ptype in claimed:
        return None
    allowed = {str(g).lower() for g in (channel.get("allowed_genres") or [])}
    if not genres:
        return 0.01
    if not allowed:
        return 0.05
    matched = len(allowed & genres)
    return matched / len(allowed) if matched else None


def carries_programmes(channel: dict[str, Any]) -> bool:
    """Whether a channel schedules programmes of its own, and so needs a line-up. A channel
    whose pattern is empty is built from its bands alone (a music channel, say)."""
    text = (channel.get("pattern") or "").strip()
    return bool(text) and bool(PROGRAMME_TOKENS & set(parse_pattern(text)))


def programme_channels(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [c for c in rows_to_dicts(conn.execute("SELECT * FROM channels WHERE enabled = 1 ORDER BY number"))
            if carries_programmes(c)]


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


def _cheapest(fits: list[tuple[dict[str, Any], float]], load: dict[tuple[int, str], float], bucket: str,
              hours: float) -> dict[str, Any]:
    """The channel an item goes to: the lowest load after taking it, scaled by how poorly it fits."""
    return min(fits, key=lambda cf: ((load[(cf[0]["id"], bucket)] + hours) / cf[1], cf[0]["number"]))[0]


def best_channel(conn: sqlite3.Connection, genres: list[str] | None, hours: float = 1.0, *,
                 ptype: str = "series", kids: bool = False, year: int | None = None) -> int | None:
    """The channel the generator would give a new item of this type with these genres, or None
    when no channel accepts it."""
    channels = programme_channels(conn)
    claimed = claimed_types(channels)
    fits = [(c, f) for c in channels
            if (f := channel_fit(c, _genre_set(genres), ptype, kids=kids, claimed=claimed, year=year)) is not None]
    if not fits:
        return None
    return _cheapest(fits, _load_hours(conn, [c["id"] for c in channels]), "general", hours)["id"]


def _insert(conn: sqlite3.Connection, channel_id: int, kind: str, key: str, title: str, year: int | None, *,
            show_id: int | None = None, media_id: int | None = None, genres: list[str] | None = None,
            source: str = "library", transient: int = 0, episode_minutes: int | None = None,
            pinned: int = 0, match: dict[str, str] | None = None, programme_type: str | None = None,
            episode_count: int | None = None, certificate: str | None = None) -> int:
    conn.execute(
        "INSERT INTO lineup(channel_id, kind, show_id, media_id, key, title, year, genres, source, transient,"
        " episode_minutes, pinned, match, programme_type, episode_count, certificate, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET channel_id = excluded.channel_id, pinned = excluded.pinned,"
        " match = COALESCE(excluded.match, lineup.match),"
        " programme_type = COALESCE(excluded.programme_type, lineup.programme_type),"
        " episode_count = COALESCE(excluded.episode_count, lineup.episode_count),"
        " certificate = COALESCE(excluded.certificate, lineup.certificate), updated_at = excluded.created_at",
        (channel_id, kind, show_id, media_id, key, title, year, json.dumps(genre_list(genres or [])), source, transient,
         episode_minutes, pinned, json.dumps(match) if match else None, programme_type, episode_count,
         normalise_cert(certificate), now_ts()))
    return int(conn.execute("SELECT id FROM lineup WHERE key = ?", (key,)).fetchone()["id"])   # inserted or updated


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
        # What the index did not say and an online check or the owner has since supplied counts
        # here too (`effective`), or a series that arrived with no genres stays where it fell.
        for row in conn.execute(
                "SELECT s.*,"
                " (SELECT COALESCE(SUM(duration), 0) FROM media m WHERE m.show_id = s.id AND m.missing = 0) AS secs,"
                " (SELECT MAX(COALESCE(season, 1)) FROM media m WHERE m.show_id = s.id AND m.missing = 0) AS seasons"
                " FROM shows s WHERE s.excluded = 0 AND s.missing = 0"):
            r = effective(dict(row))
            if r["id"] not in taken_shows and r["secs"]:
                items.append({"kind": "show", "id": r["id"], "title": r["title"], "year": r["year"],
                              "genres": _genre_set(r["genres"]), "secs": r["secs"], "cert": r["certificate"], "kids": r["kids"],
                              "bucket": _bucket(r["category"]),
                              # as the scheduler reckons it: first year plus a year a season
                              "end_year": r["year"] + max(int(r["seasons"] or 1), 1) - 1 if r["year"] else None,
                              "ptype": genre_rules.programme_type("show", genre_list(r["genres"]), r["category"], r.get("programme_type"))})
        for row in conn.execute(f"SELECT * FROM media WHERE kind = 'movie' AND {LIVE} AND duration IS NOT NULL"):
            r = effective(dict(row))
            if r["id"] not in taken_films:
                items.append({"kind": "movie", "id": r["id"], "title": r["title"], "year": r["year"],
                              "genres": _genre_set(r["genres"]), "secs": r["duration"], "cert": r["certificate"], "kids": 0,
                              "bucket": "general",
                              "ptype": genre_rules.programme_type("movie", genre_list(r["genres"]), None, r.get("programme_type"))})
        # Big items first so the hours balance out; kids and certificates alternate as a tie-break.
        order = {"U": 0, "PG": 1, "12": 2, "12A": 2, "15": 3, "18": 4}
        items.sort(key=lambda i: (-i["secs"], -int(i["kids"] or 0), order.get((i["cert"] or "PG").upper(), 1), i["title"]))
        claimed = claimed_types(channels)
        for it in items:
            fits = [(c, f) for c in channels
                    if (f := channel_fit(c, it["genres"], it["ptype"], kids=bool(it["kids"]), claimed=claimed,
                                         year=it["year"], end_year=it.get("end_year"))) is not None]
            if not fits:
                result["unmatched"] += 1
                _flag(conn, it, f"No channel accepts a {it['ptype']} with its genres ({', '.join(sorted(it['genres'])) or 'none'})")
                continue
            hours = it["secs"] / 3600
            b = it["bucket"]
            target = _cheapest(fits, load, b, hours)
            key = f"{it['kind']}:{it['id']}"
            _insert(conn, target["id"], it["kind"], key, it["title"], it["year"],
                    show_id=it["id"] if it["kind"] == "show" else None,
                    media_id=it["id"] if it["kind"] == "movie" else None, genres=sorted(it["genres"]))
            load[(target["id"], b)] += hours
            result["assigned"] += 1
        sync_home_channels(conn)
    write_mirror(conn)
    return result


def place_again(conn: sqlite3.Connection, *, show_id: int | None = None, media_id: int | None = None) -> None:
    """Give a library title its channel again after the owner has said what it is. The new type
    is the owner's word on where it belongs, so it outranks an earlier pin."""
    with tx(conn):
        conn.execute("DELETE FROM lineup WHERE source = 'library' AND show_id IS ? AND media_id IS ?", (show_id, media_id))
    generate(conn)


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

def add(conn: sqlite3.Connection, channel_id: int | None, *, show_id: int | None = None, media_id: int | None = None,
        title: str | None = None, year: int | None = None, kind: str | None = None, genres: list[str] | None = None,
        transient: bool | None = None, episode_minutes: int | None = None, source: str = "manual",
        match: Any = None, programme_type: str | None = None, episode_count: int | None = None,
        certificate: str | None = None) -> dict[str, Any]:
    """Add (or move) an entry. Library items are identified by show_id/media_id; anything else
    is an external entry that pitv_content will be asked to fetch. `programme_type` is the
    owner's word on what an external title is; without it the type is read from the genres.
    `episode_count` is how long a series ran, from the lookup; no episode past it is asked for.
    `certificate` is the match's; without one a title nobody holds yet would be free to air at
    any hour.
    Without a channel, an external entry goes where the generator would put that type."""
    if programme_type is not None and programme_type not in genre_rules.PROGRAMME_TYPES:
        raise ValueError(f"programme_type must be one of {', '.join(genre_rules.PROGRAMME_TYPES)}")
    if channel_id is None:
        if show_id is not None or media_id is not None:
            raise ValueError("a channel is required to move a library title")
        ptype = genre_rules.programme_type(kind, genres, None, programme_type)
        channel_id = best_channel(conn, genres, ptype=ptype, kids=genre_rules.is_childrens(genres or []), year=as_int(year))
        if channel_id is None:
            raise ValueError(f"no channel takes a {ptype} with these genres; choose one")
    if programme_type is None and show_id is None and media_id is None:
        # Put by hand on a channel with one type of its own, a title is that type unless the owner
        # says otherwise: the tags of a title nobody holds yet are often too thin to tell.
        theme = conn.execute("SELECT content FROM channels WHERE id = ?", (channel_id,)).fetchone()
        own = genre_rules.THEME_TYPES.get(theme["content"] if theme else "", ())
        if len(own) == 1 and genre_rules.programme_type(kind, genres) not in own:
            programme_type = own[0]
    with tx(conn):
        if show_id is not None:
            row = conn.execute("SELECT id, title, year, genres FROM shows WHERE id = ?", (show_id,)).fetchone()
            if not row:
                raise ValueError("show not found")
            lid = _insert(conn, channel_id, "show", f"show:{show_id}", row["title"], row["year"], show_id=show_id,
                          genres=sorted(_genre_set(row["genres"])), pinned=1)
        elif media_id is not None:
            row = conn.execute("SELECT id, title, year, genres FROM media WHERE id = ? AND kind = 'movie'", (media_id,)).fetchone()
            if not row:
                raise ValueError("film not found")
            lid = _insert(conn, channel_id, "movie", f"movie:{media_id}", row["title"], row["year"], media_id=media_id,
                          genres=sorted(_genre_set(row["genres"])), pinned=1)
        else:
            if not title or kind not in ("show", "movie"):
                raise ValueError("title and kind are required for an external entry")
            key = f"ext:{kind}:{title.strip().lower()}:{year or ''}"
            lid = _insert(conn, channel_id, kind, key, title.strip(), year, genres=genres or [], source=source,
                          transient=1 if transient is None else int(transient), episode_minutes=episode_minutes, pinned=1,
                          match=clean_match(match), programme_type=programme_type,
                          episode_count=as_int(episode_count) if kind == "show" else None, certificate=certificate)
        sync_home_channels(conn)
    write_mirror(conn)
    return entry(conn, lid)


_EDITABLE = {"channel_id", "enabled", "transient", "remove_after_airing", "episode_minutes", "next_episode", "notes",
             "pinned", "year", "genres", "programme_type", "episode_count", "certificate"}
_FLAGS = {"enabled", "transient", "remove_after_airing", "pinned"}


def update(conn: sqlite3.Connection, lineup_id: int, fields: dict[str, Any]) -> dict[str, Any]:
    """Edit an entry from the admin. Only the columns in _EDITABLE are written, whatever else
    the request carries; moving an entry to another channel pins it there. A value that is not
    a number where one is needed raises ValueError."""
    sets: dict[str, Any] = {}
    for k, v in fields.items():
        if k not in _EDITABLE:
            continue
        if k in _FLAGS:
            v = int(bool(v))
        elif k == "genres":
            v = json.dumps(genre_list(v))
        elif k == "notes":
            v = "" if v is None else str(v)
        elif k == "certificate":
            v = normalise_cert(str(v)) if v not in (None, "") else None
        elif k == "programme_type":
            v = str(v).strip().casefold() if v not in (None, "") else None     # empty: read it from the genres
            if v is not None and v not in genre_rules.PROGRAMME_TYPES:
                raise ValueError(f"programme_type must be one of {', '.join(genre_rules.PROGRAMME_TYPES)}")
        elif v is not None:
            v = int(v)
        sets[k] = v
    if "channel_id" in sets:
        sets["pinned"] = 1
    if sets:
        sets["updated_at"] = now_ts()
        with tx(conn):
            update_row(conn, "lineup", lineup_id, sets)
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
    """Line-up entries with their channel and progress: episodes on disk, requests open and
    delivered, and placeholder slots still waiting for a file. The request and placeholder
    counts are aggregated once per call, in one pass over each table, rather than per entry."""
    sql = ("SELECT l.*, c.number AS channel_number, c.name AS channel_name,"
           " (SELECT COUNT(*) FROM media m WHERE m.show_id = l.show_id AND m.missing = 0) AS episodes_on_disk,"
           " COALESCE(w.n_open, 0) AS wanted_open, COALESCE(w.n_done, 0) AS wanted_done,"
           " COALESCE(p.n, 0) AS placeholders"
           " FROM lineup l JOIN channels c ON c.id = l.channel_id"
           " LEFT JOIN (SELECT lineup_id, SUM(status = 'queued') AS n_open, SUM(status = 'done') AS n_done"
           "            FROM wanted WHERE lineup_id IS NOT NULL GROUP BY lineup_id) w ON w.lineup_id = l.id"
           " LEFT JOIN (SELECT wq.lineup_id, COUNT(*) AS n FROM schedule s JOIN wanted wq ON wq.id = s.wanted_id"
           "            WHERE s.media_id IS NULL GROUP BY wq.lineup_id) p ON p.lineup_id = l.id")
    where, params = [], []
    if channel_id is not None:
        where.append("l.channel_id = ?")
        params.append(channel_id)
    if lineup_id is not None:
        where.append("l.id = ?")
        params.append(lineup_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    out = rows_to_dicts(conn.execute(sql + " ORDER BY c.number, l.kind, l.title", params))
    for r in out:
        r["on_disk"] = bool(r["show_id"] and r["episodes_on_disk"]) or bool(r["media_id"])
        # External entries were added by hand or from pitv_content's catalogue; a series keeps
        # asking for its next episode even once some have arrived.
        r["external"] = r["source"] != "library"
        r["match"] = clean_match(json.loads(r["match"])) if isinstance(r.get("match"), str) else None
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


def facets(conn: sqlite3.Connection) -> dict[str, Any]:
    """What the library and added catalogue entries hold, by kind and classification.

    The admin offers these as the only choices for a channel's genres and a band's genres and
    decades, so nobody can type a genre no item carries and then wonder why the band is empty.
    Counts are per kind (`episode`, `movie`, `music`) because a band draws on the kinds it names:
    a band of music videos should not be offered Westerns."""
    genres: dict[str, dict[str, int]] = {}
    decades: dict[str, dict[str, int]] = {}
    concerts = 0
    for kind, sql in (("episode", f"SELECT genres, year, 0 AS concert FROM shows WHERE {LIVE}"),
                      ("movie", f"SELECT genres, year, 0 AS concert FROM media WHERE kind = 'movie' AND {LIVE}"),
                      ("music", f"SELECT genres, year, concert FROM media WHERE kind = 'music' AND {LIVE}")):
        for r in conn.execute(sql):
            for g in genre_list(r["genres"]):
                genres.setdefault(g, dict.fromkeys(FACET_KINDS, 0))[kind] += 1
            if r["year"]:
                decades.setdefault(str((r["year"] // 10) * 10), dict.fromkeys(FACET_KINDS, 0))[kind] += 1
            concerts += int(r["concert"] or 0)
    # Added titles are part of the programme catalogue too. Including them here makes the same
    # genre names available in their editor and the channel editor; previously `Children` could
    # be saved on an entry but was invisible in the channel's genre picker.
    for r in conn.execute("SELECT kind, genres, year FROM lineup"
                          " WHERE source != 'library' AND enabled = 1 AND kind IN ('show', 'movie')"):
        kind = "episode" if r["kind"] == "show" else "movie"
        for g in genre_list(r["genres"]):
            genres.setdefault(g, dict.fromkeys(FACET_KINDS, 0))[kind] += 1
        if r["year"]:
            decades.setdefault(str((r["year"] // 10) * 10), dict.fromkeys(FACET_KINDS, 0))[kind] += 1
    return {"genres": dict(sorted(genres.items())),
            "decades": dict(sorted(decades.items(), key=lambda kv: int(kv[0]))), "concerts": concerts}


# --- JSON mirror --------------------------------------------------------------------------------

_EXPORTED = ("kind", "title", "year", "source", "transient", "remove_after_airing", "episode_minutes", "next_episode",
             "enabled", "pinned", "notes", "genres", "match", "programme_type", "episode_count", "certificate")


def export(conn: sqlite3.Connection) -> dict[str, Any]:
    by_channel: dict[int, list[dict[str, Any]]] = {}
    for e in entries(conn):
        by_channel.setdefault(e["channel_id"], []).append({k: e[k] for k in _EXPORTED})
    channels = rows_to_dicts(conn.execute(
        "SELECT id, number, name, content, allowed_genres, excluded_genres, nas_only FROM channels ORDER BY number"))
    return {"schema": 1, "exported_ts": now_ts(), "nas_only": get_setting(conn, "nas_only", True), "channels": [
        {"number": c["number"], "name": c["name"], "content": c["content"], "allowed_genres": c["allowed_genres"] or [],
         "excluded_genres": c["excluded_genres"] or [], "nas_only": c["nas_only"], "lineup": by_channel.get(c["id"], [])}
        for c in channels]}


def write_mirror(conn: sqlite3.Connection) -> None:
    """lineups.json beside the database, so the line-up survives a database rebuild."""
    write_data_file(conn, MIRROR, lambda: export(conn))


def _import_entry(conn: sqlite3.Connection, channel_id: int, e: Any) -> bool:
    """One entry of a line-up document; False when it cannot be read."""
    title = (as_text(e.get("title")) or "").strip() if isinstance(e, dict) else ""
    kind = e.get("kind", "show") if title else None
    if kind not in ("show", "movie"):
        return False
    year = as_int(e.get("year"))
    pinned = int(as_bool(e.get("pinned"), True))
    if kind == "show":
        row = conn.execute("SELECT id FROM shows WHERE lower(title) = lower(?) AND (year = ? OR ? IS NULL) AND missing = 0",
                           (title, year, year)).fetchone()
        lid = _insert(conn, channel_id, kind, f"show:{row['id']}", title, year, show_id=row["id"], pinned=pinned) \
            if row else None
    else:
        row = conn.execute("SELECT id FROM media WHERE kind = 'movie' AND lower(title) = lower(?) AND (year = ? OR ? IS NULL)"
                           " AND missing = 0", (title, year, year)).fetchone()
        lid = _insert(conn, channel_id, kind, f"movie:{row['id']}", title, year, media_id=row["id"], pinned=pinned) \
            if row else None
    if lid is None:
        # Not in the catalogue: an external entry, even if the document calls it a library one.
        source = e.get("source") if e.get("source") in EXTERNAL_SOURCES else "manual"
        lid = _insert(conn, channel_id, kind, f"ext:{kind}:{title.lower()}:{year or ''}", title, year,
                      genres=genre_list(e.get("genres")), source=source,
                      transient=int(as_bool(e.get("transient"), True)), episode_minutes=as_int(e.get("episode_minutes")),
                      pinned=1, match=clean_match(e.get("match")),
                      programme_type=e.get("programme_type") if e.get("programme_type") in genre_rules.PROGRAMME_TYPES else None,
                      episode_count=as_int(e.get("episode_count")), certificate=as_text(e.get("certificate")))
    update_row(conn, "lineup", lid, {
        "enabled": int(as_bool(e.get("enabled"), True)), "remove_after_airing": int(as_bool(e.get("remove_after_airing"))),
        "next_episode": as_int(e.get("next_episode")) or 1, "notes": as_text(e.get("notes")) or ""})
    return True


def import_doc(conn: sqlite3.Connection, doc: dict[str, Any]) -> dict[str, int]:
    """Apply a line-up document (the mirror, or a file uploaded in the admin): channels matched
    by number; library entries matched by title and year against the catalogue, everything else
    becomes an external entry. Entries that cannot be read are skipped and counted."""
    listed = doc.get("channels") if isinstance(doc, dict) else None
    if not isinstance(listed, list):
        raise TypeError("expected a line-up document with a channels list")
    result = {"entries": 0, "skipped": 0, "unknown_channels": 0}
    channels = {r["number"]: r["id"] for r in conn.execute("SELECT id, number FROM channels")}
    with tx(conn):
        for ch in listed:
            cid = channels.get(as_int(ch.get("number"))) if isinstance(ch, dict) else None
            if cid is None:
                result["unknown_channels"] += 1
                continue
            fields = {k: json.dumps(genre_list(ch[k])) for k in ("allowed_genres", "excluded_genres") if k in ch}
            if ch.get("nas_only") in ("inherit", "yes", "no"):
                fields["nas_only"] = ch["nas_only"]
            update_row(conn, "channels", cid, fields)
            for e in ch.get("lineup") if isinstance(ch.get("lineup"), list) else []:
                result["entries" if _import_entry(conn, cid, e) else "skipped"] += 1
        sync_home_channels(conn)
    write_mirror(conn)
    return result


def restore_if_empty(conn: sqlite3.Connection) -> bool:
    """After a database rebuild, bring the line-up back from the JSON mirror."""
    if conn.execute("SELECT 1 FROM lineup LIMIT 1").fetchone():
        return False
    path = data_path(conn, MIRROR)
    if path is None or not path.exists():
        return False
    try:
        import_doc(conn, json.loads(path.read_text()))
    except (OSError, TypeError, ValueError) as exc:
        log.warning("could not restore line-up from %s: %s", path, exc)
        return False
    log.info("line-up restored from %s", path)
    return True


# --- material arriving from pitv_content -------------------------------------------------------

def attach_delivery(conn: sqlite3.Connection, wanted_id: int, media_id: int) -> dict[int, int]:
    """Hand a delivered file (already a catalogue entry) to its line-up entry and to the
    placeholder slots that requested it. Runs inside the caller's transaction.

    A fetched film becomes an ordinary library entry. A fetched series stays external, so later
    placements keep requesting the next episode, but links to its show so the show is owned by
    the entry's channel and never generated onto another one. Every bound slot, a repeat
    included, takes the file's real length: a film is never cut off, and a slot sized from the
    entry's nominal episode length does not run on as a holding card after a shorter file has
    ended. Returns {channel_id: earliest change} for the caller to rebuild from."""
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
    real = round(float(media.get("duration") or 0))
    changed: dict[int, int] = {}
    for sl in conn.execute("SELECT id, channel_id, start_ts, end_ts, replay FROM schedule WHERE wanted_id = ? AND media_id IS NULL",
                           (wanted_id,)).fetchall():
        end = sl["end_ts"]
        if real and sl["start_ts"] + real != end:
            at = min(end, sl["start_ts"] + real)
            end = sl["start_ts"] + real
            changed[sl["channel_id"]] = min(changed.get(sl["channel_id"], at), at)
        conn.execute("UPDATE schedule SET media_id = ?, end_ts = ?, title = ?, subtitle = ? WHERE id = ?",
                     (media_id, end, title, subtitle, sl["id"]))
    return changed


def _remove_cache_file(path: str, cache_root: Path | None) -> bool:
    """Delete `path` when, and only when, it is a file inside the cache. The parent folder is
    resolved (following symlinked folders) but the file name is not, so a symlink in the cache is
    removed as a link and never takes its target with it. Anything outside the cache, the NAS
    above all, is never PiTV's to delete. Returns whether the file is gone."""
    if cache_root is None:
        log.warning("transient file %s not removed: no cache_dir is set", path)
        return False
    p = Path(path)
    try:
        target = p.parent.resolve(strict=True) / p.name
    except FileNotFoundError:
        return True    # its folder has gone, so has the file
    except (OSError, RuntimeError) as exc:   # unreadable, or a symlink loop
        log.warning("transient file %s not removed: %s", path, exc)
        return False
    if p.name in ("", ".", "..") or not target.is_relative_to(cache_root):
        log.warning("transient file %s not removed: it is outside the cache %s", path, cache_root)
        return False
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not remove transient file %s: %s", path, exc)
        return False
    return True


def remove_aired_transients(conn: sqlite3.Connection, now: int | None = None) -> int:
    """Delete fetched transient files once they have aired and nothing still schedules them:
    straight away when the entry says remove-after-airing, otherwise after transient_keep_days.
    Acquired files live outside the cache's size-capped LRU area, so this is what bounds them.
    The catalogue entry is retired either way; a file that could not be deleted is logged."""
    now = now or now_ts()
    keep = int(get_setting(conn, "transient_keep_days", 7)) * 86400
    cache_dir = get_setting(conn, "cache_dir", "")
    cache_root = Path(cache_dir).resolve() if cache_dir else None
    expired = [r for r in conn.execute(
        "SELECT m.id, m.path, m.cache_path,"
        " EXISTS (SELECT 1 FROM wanted w JOIN lineup l ON l.id = w.lineup_id"
        "         WHERE w.dest_path = m.path AND l.remove_after_airing = 1) AS immediate,"
        " (SELECT MAX(h.ended_at) FROM history h WHERE h.media_id = m.id) AS last_aired"
        " FROM media m WHERE m.transient = 1 AND m.missing = 0"
        " AND NOT EXISTS (SELECT 1 FROM schedule s WHERE s.media_id = m.id AND s.end_ts > ?)", (now,)).fetchall()
        # never before it has been shown
        if r["last_aired"] and (r["immediate"] or now - r["last_aired"] >= keep)]
    for r in expired:
        removed = [_remove_cache_file(p, cache_root) for p in {r["path"], r["cache_path"]} - {None, ""}]
        if all(removed):
            log.info("removed transient file after airing: %s", r["path"])
    with tx(conn):
        conn.executemany("UPDATE media SET missing = 1 WHERE id = ?", [(r["id"],) for r in expired])
    return len(expired)


def evict_fetched(conn: sqlite3.Connection, needed: int = 0, now: int | None = None) -> int:
    """Make room by deleting fetched material, oldest aired first, when evicting cache copies
    has not been enough. Returns how many items went.

    Fetched episodes are kept after they air, so the library fills once and a later airing costs
    nothing. That has to end somewhere: when the cache folder is over its cap, or the drive
    cannot take what pitv_content is about to bring, the fetched items that aired longest ago
    go first, with a warning, until there is room. Those pitv_content filed as it found them go
    before those it had to re-encode (the index's `encoded`), for the reason copies go before
    encodes in `MediaCache.make_room`: getting one back costs a download, the other a download
    and most of an hour's encoding. `pi_can_play` cannot tell them apart, since a file encoded
    to the screen plays as readily as one that never needed it. Nothing scheduled ahead is touched, nor
    anything that has not aired yet (it was fetched for an airing to come), nor anything
    outside the cache. The catalogue row is retired; the title can always be fetched again."""
    from .player.cache import HEADROOM_BYTES, MediaCache, tree_bytes
    now = now or now_ts()
    cache = MediaCache.from_settings(all_settings(conn))
    if not cache.enabled or not cache.dir:
        return 0
    root = cache.dir.resolve()
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        return 0
    short = max(tree_bytes(root) + needed - cache.max_bytes, needed + HEADROOM_BYTES - free, 0)
    if short <= 0:
        return 0
    rows = conn.execute(
        "SELECT m.id, m.title, m.path, m.cache_path, MAX(h.ended_at) AS last_aired FROM media m"
        " JOIN history h ON h.media_id = m.id WHERE m.origin IN ('cache', 'online') AND m.missing = 0"
        " AND NOT EXISTS (SELECT 1 FROM schedule s WHERE s.media_id = m.id AND s.end_ts > ?)"
        " GROUP BY m.id ORDER BY COALESCE(m.encoded, 0), last_aired", (now,)).fetchall()
    gone: list[int] = []
    freed = 0
    for r in rows:
        if freed >= short:
            break
        paths = {r["path"], r["cache_path"]} - {None, ""}
        size = sum(Path(p).stat().st_size for p in paths if Path(p).is_file())
        if size and all(_remove_cache_file(p, root) for p in paths):
            freed += size
            gone.append(r["id"])
    with tx(conn):
        conn.executemany("UPDATE media SET missing = 1 WHERE id = ?", [(i,) for i in gone])
    log.warning("the cache drive was short of %d MB: removed %d fetched item(s), oldest aired first, freeing %d MB%s",
                short // 1024 ** 2, len(gone), freed // 1024 ** 2,
                "" if freed >= short else "; still short, and nothing else has aired that may go")
    return len(gone)
