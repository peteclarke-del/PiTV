"""PiTV's half of the contract with pitv_content (docs/CONTENT_CONTRACT.md, schema 2).

The request manifest lists every file the schedule needs through the end of the next broadcast
day: copy or transcode it from the NAS, or fetch it online. Delivery reports record where each
file landed; material fetched online becomes a catalogue entry here and takes the placeholder
slots that asked for it. Reports come from another process and are read defensively: an entry
that cannot be used is counted as failed, never allowed to abort the rest of the report.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import display, tool_client, youtube
from .catalogue import KINDS, family_safe, write_mirror
from .db import (
    all_settings,
    as_bool,
    as_float,
    as_int,
    as_text,
    genre_list,
    get_setting,
    insert_row,
    now_ts,
    row_to_dict,
    rows_to_dicts,
    tx,
    update_row,
)
from .lineup import attach_delivery, clean_match
from .player.cache import MediaCache
from .player.hwdec import PI_HW_CODECS, is_raspberry_pi, pi_can_play
from .scheduler.horizon import rebuild_from
from .scheduler.rules import broadcast_day_for, day_bounds, keyword_pattern, normalise_cert, tz_of

MANIFEST_SCHEMA = 2
RESIZE_THRESHOLD = 30   # seconds; smaller differences between scheduled and delivered length are absorbed
# The earliest slot change on each (channel_id, broadcast day), which is what a rebuild needs.
SlotChanges = dict[tuple[int, str], int]
MAX_WANTED_ATTEMPTS = 3
# How pitv_content opens a message when it searched and found nothing. That is an answer, not a
# fault: uploads are retitled and new ones appear, so the request is asked again and uses no
# attempt. Everything else in a message is a fault, and the doctor tells the two apart by this
# same prefix, so the rule is written once rather than guessed at in two places.
MISS_PREFIX = "not found yet"
# How a run records work it never got to. pitv_content delivers in bands, scheduled before
# unscheduled, and a slice that runs out of time never reaches the last of them: nothing fails,
# nothing is logged, and the only symptom is a channel that stays empty, which is how 148
# requests sat untouched for a day. It says which bands it did not reach; PiTV keeps the account
# in the run log so the doctor can tell a slice that was merely busy from a band being starved.
UNREACHED = "not reached: "
# And how it records work it looked at and deliberately held for a later slice: a second
# re-encode in one slice, for instance, because one long one can take an evening. That is a rule
# working, not starvation, and the two were once reported as one fact, which made either
# impossible to judge. `most_slices` is the longest a request in that band has been waiting and
# `limit` the bound after which pitv_content takes it regardless, so a streak that climbs past
# the limit means the bound itself has stopped working.
HELD = "held over: "
APPLIED_REPORT_DAYS = 7      # report files, once applied, are kept this long for reference
UNAPPLIED_REPORT_DAYS = 30   # a report file that never applies is given up after this long
DEADLINE_LEAD = 15 * 60  # a file is due this long before it first airs
REMOTE_PRIORITY_HOURS = 24  # fetching is slower/less certain than copying, so begin one day earlier
# A request nothing is waiting for: a gap in a series that already plays, or an advert added by
# hand. Worth doing, worth doing after everything a channel is short of tonight. Far enough out
# that no real air time reaches it (a fortnight of four-hour steps is 84).
NOT_WAITED_ON = 1000
SCREEN_VALUES = "video_profile_values"  # the settings key pitv_content takes the screen profile's values under
# Typical running times per kind, so pitv_content can reject obviously wrong search hits.
WANTED_MINUTES = {"music": [2, 8], "advert": [0.1, 2], "episode": [20, 60], "movie": [70, 180]}
log = logging.getLogger("pitv.content")


def acquire_dir(settings: dict[str, Any]) -> str:
    """Where pitv_content files what it fetches: `acquire_dir`, else `<cache_dir>/acquired`."""
    cache = settings.get("cache_dir") or ""
    return settings.get("acquire_dir") or (str(Path(cache) / "acquired") if cache else "")


def manifest_window(settings: dict[str, Any], tz, now: int, days: int) -> int:
    """End of the manifest window: `days`=1 means through the end of the next broadcast day,
    so a run at 01:00 covers today's remainder and the whole of tomorrow (08:00 to 08:00)."""
    day = broadcast_day_for(now, settings, tz)
    return day_bounds(day + timedelta(days=max(1, days)), settings, tz)[2]


# --- request manifest ---------------------------------------------------------------------------

def _identity(row: dict[str, Any], show_title: str | None) -> dict[str, Any]:
    """What pitv_content matches a request on, for a media or a wanted row."""
    return {"kind": row["kind"], "show_title": show_title, "season": row.get("season"),
            "episode": row.get("episode"), "title": row["title"], "year": row.get("year"),
            "artist": row.get("artist")}


def _fetch_fields(w: dict[str, Any], show_title: str | None, acquire: str,
                  networks: list[str] | None = None) -> dict[str, Any]:
    """The search pitv_content is asked to run, and where to file what it finds.

    The duration window says what a programme of this kind runs to, so an upload of the wrong
    length is refused rather than filed under a title it does not belong to. It assumes a
    broadcaster gave the programme a slot, which is true of everything except a creator's
    channel: the same creator posts a two minute clip on Tuesday and a seventy minute one on
    Thursday, and there is no length that is wrong for them. A television episode's twenty to
    sixty rejected most of a channel and kept only what happened to land inside it.

    Such a request is sent without a window and pitv_content falls back to what its own listing
    says the video runs to, which is the only authority on it. This is the same lesson as the
    shorts: a channel is not a series, and a rule that fits a series is wrong for it in whichever
    direction it is applied."""
    hints = _search_hints(w, show_title, networks)
    search = {"phrase": hints[0], "hints": hints[1:],
              # A music video must be the exact release; a film or episode may carry a nearby year.
              "year_tolerance": 0 if w["kind"] == "music" else 2}
    if not youtube.is_channel(_match(w.get("lineup_match"))):
        search["duration_minutes"] = WANTED_MINUTES.get(w["kind"], [1, 240])
    return {"search": search,
            "dest_dir": _wanted_dest({**w, "title": show_title or w["title"]}, acquire)}


def _media_request(m: dict[str, Any], cache: MediaCache, acquire: str) -> dict[str, Any] | None:
    """The request for a catalogue file: copy or transcode a NAS original into the cache, or
    fetch again material that only ever lived in the cache and has gone.

    Fetching again is only honest where a request stands behind the row (`requested_by`): a
    title somebody named, which pitv_content can find again by that identity. A row that came
    from the index of a band collection is whatever an open search happened to find, and its
    title and year are pitv_content's reading of an upload; sent back as a request they read
    as an instruction, and the search returns some other upload to be filed under the same
    guess. Such a row has no request: the index will report it gone, readiness replaces its
    slot, and the band asks for more material in the ordinary way."""
    copy = cache.cache_copy(m)
    base = {"media_id": m["id"], "wanted_id": None, "uid": m.get("uid"), **_identity(m, m.get("show_title")),
            "duration": m.get("duration"), "already_cached": copy is not None, "transient": bool(m.get("transient"))}
    if m.get("origin", "nas") == "nas":
        original = Path(m["path"])
        # What the Pi can play is copied as it is; only the rest is re-encoded to the profile.
        transcode = not pi_can_play(m)
        name = f"{m['id']}_{original.stem}{'.mp4' if transcode else original.suffix}"
        return {**base, "action": "transcode" if transcode else "copy",
                "source": {"path": m["path"], "vcodec": m.get("vcodec"), "height": m.get("height"),
                           "interlaced": bool(m.get("interlaced")), "size": m.get("size")},
                "target": str(copy or (cache.dir / name if cache.dir else name))}
    if copy is not None:
        return {**base, "action": "copy", "source": None, "target": str(copy)}
    if not m.get("requested_by"):
        return None
    return {**base, "action": "fetch", "source": None,
            **_fetch_fields(m, m.get("show_title"), acquire, _networks(m.get("networks")))}


def _match(raw: Any) -> dict[str, str] | None:
    """A line-up entry's confirmed identity, as stored (JSON text) or already decoded."""
    try:
        return clean_match(json.loads(raw) if isinstance(raw, str) else raw)
    except ValueError:
        return None


def _wanted_request(w: dict[str, Any], show_title: str | None, acquire: str) -> dict[str, Any]:
    """The request for a wanted row: find it online and file it under `dest_dir`."""
    return {"media_id": None, "wanted_id": w["id"], "uid": None, **_identity(w, show_title),
            "genre": w.get("genre"), "ref": w.get("ref"), "match": _match(w.get("lineup_match")),
            "action": "fetch", "source": None,
            "already_cached": False, "transient": bool(w.get("transient")), "attempts": w.get("attempts", 0),
            **_fetch_fields(w, show_title, acquire, _networks(w.get("networks")))}


def manifest(conn: sqlite3.Connection, days: int = 1, now: int | None = None) -> dict[str, Any]:
    """The request manifest (contract section 2): everything scheduled between `now` and the end
    of the manifest window, plus the wanted rows not tied to a slot."""
    settings = all_settings(conn)
    tz = tz_of(conn)
    now = now or now_ts()
    cache = MediaCache.from_settings(settings)
    acquire = acquire_dir(settings)
    horizon = manifest_window(settings, tz, now, days)
    profile = display.content_profile(settings)
    items: dict[str, dict[str, Any]] = {}

    def add(request_id: str, slot: dict[str, Any],
            build: Callable[[dict[str, Any]], dict[str, Any] | None]) -> None:
        """One request per file however many slots and channels use it, timed by its first airing."""
        it = items.get(request_id)
        if it is None:
            first_air = slot["start_ts"]
            request = build(slot)
            if request is None:
                return
            priority = int(max(0.0, (first_air - now) / 3600) // 4)
            remote = request.get("action") == "fetch"
            if remote:
                # A NAS item remains playable through nas_fallback while its cache copy is
                # prepared; a remote-only item has no such escape hatch.  Keep the full lead
                # adjustment (including negative values) so an imminent fetch sorts ahead of
                # overdue NAS copies/transcodes instead of tying them at priority zero.
                priority -= REMOTE_PRIORITY_HOURS // 4
            it = items[request_id] = {**request, "request_id": request_id, "channels": [],
                                      "first_air_ts": first_air, "deadline_ts": first_air - DEADLINE_LEAD,
                                      "priority": priority, "remote_required": remote}
        if slot["channel"] not in it["channels"]:
            it["channels"].append(slot["channel"])

    for r in rows_to_dicts(conn.execute(
            "SELECT s.start_ts, c.number AS channel, c.networks AS networks, m.*, sh.title AS show_title,"
            " (SELECT w.id FROM wanted w WHERE w.dest_path IS NOT NULL AND w.dest_path IN (m.path, m.cache_path)"
            "  LIMIT 1) AS requested_by FROM schedule s"
            " JOIN media m ON m.id = s.media_id JOIN channels c ON c.id = s.channel_id"
            " LEFT JOIN shows sh ON sh.id = m.show_id"
            " WHERE s.end_ts > ? AND s.start_ts < ? AND m.missing = 0 AND s.kind != 'filler'"
            " ORDER BY s.start_ts", (now, horizon))):
        add(f"m:{r['id']}", r, lambda m: _media_request(m, cache, acquire))

    # Placeholders: line-up material not on disk, requested by wanted row. These are listed for
    # the whole built schedule, not only the manifest window. A copy takes seconds and cache
    # space, so it waits until the day before; a fetch takes pitv_content a good part of an
    # hour, and told only a day ahead it was given sixty new episodes each morning and delivered
    # three. Listed a week ahead with their air times, they are worked in the order they air.
    for r in rows_to_dicts(conn.execute(
            "SELECT s.start_ts, s.end_ts, c.number AS channel, c.networks AS networks, w.*,"
            " l.title AS lineup_title, l.match AS lineup_match"
            " FROM schedule s"
            " JOIN wanted w ON w.id = s.wanted_id JOIN channels c ON c.id = s.channel_id"
            " LEFT JOIN lineup l ON l.id = w.lineup_id"
            # The attempts ceiling applies here too. It used to guard only the requests with no
            # slot, so one a slot was waiting for went out again on every manifest however often
            # it had failed: two videos deleted from their site were asked for twenty-five times,
            # each costing a search that could only fail. What a slot cannot have, readiness
            # replaces or the channel cards; asking again is not what fixes it.
            " WHERE s.media_id IS NULL AND s.end_ts > ? AND w.status != 'done' AND w.attempts < ?"
            " ORDER BY s.start_ts", (now, MAX_WANTED_ATTEMPTS))):
        add(f"w:{r['id']}", r, lambda w: {
            **_wanted_request(w, w["lineup_title"] if w["kind"] == "episode" else None, acquire),
            "duration": w["end_ts"] - w["start_ts"]})

    # Requests not tied to a slot: adverts and music videos added by hand, series gaps, and the
    # episodes a band asked its line-up for before anything is scheduled to air them. A line-up
    # row carries the series title on the entry rather than in `shows`, exactly as the scheduled
    # placeholders above read it; without that the request is filed and searched for as
    # "Episode 1" rather than under the series it belongs to.
    scheduled = {it["wanted_id"] for it in items.values() if it.get("wanted_id")}
    # Nothing here has an air time of its own, so it is judged by the gap it would fill: the
    # soonest holding card the entry could go into (`wanted.card_waiting_for`), on the same
    # scale as the items above so the two lists can be read together. Ranking by what raised a
    # request instead goes stale, because "a line-up raised it" becomes true of everything;
    # a card on screen at eight tomorrow does not.
    from .wanted import card_waiting_for  # imported here: wanted reads this module's manifest
    waiting = card_waiting_for(conn, now)
    def urgency(w: dict[str, Any]) -> int:
        at = waiting.get(w["lineup_id"]) if w.get("lineup_id") else None
        if at is None:
            return NOT_WAITED_ON
        return int(max(0.0, (at - now) / 3600) // 4) - REMOTE_PRIORITY_HOURS // 4
    wanted = [{"request_id": f"w:{w['id']}", "priority": urgency(w),
               **_wanted_request(w, w.get("show_title") or (w.get("lineup_title") if w["kind"] == "episode" else None),
                                 acquire)}
              for w in rows_to_dicts(conn.execute(
                  "SELECT w.*, sh.title AS show_title, l.title AS lineup_title, l.match AS lineup_match,"
                  # A request belongs to a channel either way: an added title through its line-up
                  # entry, a gap through the series it fills. The channel's broadcasters are what
                  # the search is told to look under.
                  " COALESCE(cl.networks, cs.networks) AS networks FROM wanted w"
                  " LEFT JOIN shows sh ON sh.id = w.show_id LEFT JOIN lineup l ON l.id = w.lineup_id"
                  " LEFT JOIN channels cl ON cl.id = l.channel_id"
                  " LEFT JOIN channels cs ON cs.id = sh.home_channel_id"
                  " WHERE w.status IN ('queued', 'failed') AND w.attempts < ? ORDER BY w.id",
                  (MAX_WANTED_ATTEMPTS,)))
              if w["id"] not in scheduled]
    wanted.sort(key=lambda w: (w["priority"], w["wanted_id"]))
    return {"schema": MANIFEST_SCHEMA, "generated_ts": now, "horizon_ts": horizon, "days": days,
            "cache_dir": str(cache.dir) if cache.dir else "", "acquire_dir": acquire, "pi": is_raspberry_pi(),
            "free_bytes": cache.usage().get("free"), "cache_max_bytes": cache.max_bytes,
            "running_marker": str(cache.running_marker) if cache.dir else None,
            "reports_dir": str(cache.reports_dir) if cache.dir else None,
            "profile": profile, "items": sorted(items.values(), key=lambda i: (i["priority"], i["deadline_ts"])),
            "wanted": wanted}


def push_screen(settings: dict[str, Any]) -> str | None:
    """Tell pitv_content the screen it encodes to when it has no manifest to read it from (a
    catalogue run, a band helping). The profile's values go with its name, the same object the
    manifest carries, so `pitv/display.py` is the only table and a value changed there reaches
    what pitv_content fetches with no release of its own. A pitv_content from before it took the
    values refuses the key; it is then given the name alone, which it resolves against its own
    table as it always did. Returns why the push failed, or None."""
    profile = display.content_profile(settings)
    base = tool_client.base_url(settings)
    status, payload = tool_client.request(base, "PUT", "settings",
                                          body={"profile": profile["name"], SCREEN_VALUES: profile}, timeout=5)
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if status == 400 and isinstance(errors, dict) and set(errors) == {SCREEN_VALUES}:
        log.info("pitv_content does not take the screen's values yet; sending its name alone")
        status, payload = tool_client.request(base, "PUT", "settings", body={"profile": profile["name"]}, timeout=5)
    if status < 400:
        return None
    detail = (payload.get("error") or payload.get("errors")) if isinstance(payload, dict) else payload
    return f"HTTP {status}: {detail}"


def protect_manifest(conn: sqlite3.Connection, cache: MediaCache, now: int | None = None) -> None:
    """Keep eviction away from everything in the current manifest (contract section 5), and tell
    it which cached files pitv_content had to re-encode (those `pi_can_play` refuses as they
    are), so that plain copies go first."""
    cache.protect({Path(i["target"]).name for i in manifest(conn, days=1, now=now)["items"] if i.get("target")})
    cache.costly({Path(m["cache_path"]).name for m in rows_to_dicts(conn.execute(
        "SELECT cache_path, vcodec, height, interlaced FROM media WHERE cache_path IS NOT NULL AND origin = 'nas'"))
        if not pi_can_play(m)})


def _networks(raw: Any) -> list[str]:
    """A channel's broadcasters as stored (JSON text) or already decoded."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [str(n) for n in raw] if isinstance(raw, list) else []


def _search_hints(w: dict[str, Any], show_title: str | None, networks: list[str] | None = None) -> list[str]:
    """hints[0] is the exact search phrase pitv_content tries first; the rest are extra words.

    The extra words used to name two British broadcasters outright, whatever the request was
    for. That is the station's configuration and not something this code knows: it made every
    search for a creator's YouTube video carry "BBC" and "ITV", and it would be wrong for any
    station not modelled on British television. They now come from the broadcasters the
    request's own channel stands for (`channels.networks`), so a channel that names none sends
    none and a station built on other broadcasters sends those."""
    yr = f" {w['year']}" if w.get("year") else ""
    extra = [n for n in (networks or []) if str(n).strip()][:3]
    if w["kind"] == "music":
        artist = f"{w['artist']} " if w.get("artist") else ""
        return [f"{artist}{w['title']} official video".strip(), *extra]
    if w["kind"] == "advert":
        return [f"{w['title']}{yr} advert", "TV ad", *extra]
    if w["kind"] == "episode":
        se = ""
        if w.get("season") is not None and w.get("episode") is not None:
            se = f" S{int(w['season']):02d}E{int(w['episode']):02d}"
        return [f"{show_title or w['title']}{se}{yr} full episode", *extra]
    return [f"{w['title']}{yr} full film", *extra]


def _folder(name: str) -> str:
    """A title as a single folder name. Titles are typed by hand; a separator would nest folders
    and ".." would climb out of acquire_dir."""
    name = re.sub(r"[/\\\x00]", "-", name).strip()
    return name if name.strip(".") else "Unknown"


def _wanted_dest(w: dict[str, Any], acquire: str) -> str:
    base = Path(acquire) if acquire else Path("acquired")
    year = f" ({w['year']})" if w.get("year") else ""
    title = _folder(f"{w['title']}{year}")
    if w["kind"] == "episode":
        return str(base / "tvshows" / title / f"Season {int(w.get('season') or 1):02d}")
    if w["kind"] == "movie":
        return str(base / "movies" / title)
    if w["kind"] == "music":
        return str(base / "music videos" / _folder((w.get("genre") or "Unsorted").title()))
    return str(base / "ads" / str(w.get("year") or "unknown"))


# --- delivery reports ------------------------------------------------------------------------

def _earliest(changes: SlotChanges, key: tuple[int, str], at: int) -> None:
    changes[key] = min(changes.get(key, at), at)


def _resize_slots(conn: sqlite3.Connection, media_id: int, real: float) -> SlotChanges:
    """Give future slots of `media_id` its delivered length. Returns the earliest change on each
    channel-day for the caller to rebuild; differences under RESIZE_THRESHOLD are absorbed."""
    changed: SlotChanges = {}
    for sl in conn.execute("SELECT id, channel_id, day, start_ts, end_ts FROM schedule WHERE media_id = ? AND replay = 0"
                           " AND start_ts > ?", (media_id, now_ts())).fetchall():
        end = sl["start_ts"] + round(real)
        if abs(end - sl["end_ts"]) < RESIZE_THRESHOLD:
            continue
        conn.execute("UPDATE schedule SET end_ts = ? WHERE id = ?", (end, sl["id"]))
        _earliest(changed, (sl["channel_id"], sl["day"]), min(end, sl["end_ts"]))
    return changed


def _fetched_show(conn: sqlite3.Connection, w: dict[str, Any], meta: dict[str, Any], year: int | None) -> int:
    """The series a fetched episode belongs to: the one its line-up entry or its wanted row
    already names, else a series created (once) for material fetched online.

    A new series takes the line-up entry's genres as well as the ones the delivery carries. The
    entry's are the owner's own classification, made in the admin when the title was added, and
    they are the only ones that can be right about a creator's channel: pitv_content sees a
    YouTube video and says so, while the owner knows the channel is about food, or motorcycles,
    or comedy. Filed under the delivery's word alone, every channel's material read simply
    "YouTube", and a band asking for YouTube and Food could match nothing at all: twenty three
    videos sat unusable behind nine bands that each showed a card all day.

    The two are merged rather than one replacing the other. Both are true, the owner's is what
    the bands are written against, and a union only ever widens what can be placed."""
    entry = conn.execute("SELECT title, show_id, genres FROM lineup WHERE id = ?", (w["lineup_id"],)).fetchone() \
        if w.get("lineup_id") else None
    known = (entry["show_id"] if entry else None) or w.get("show_id")
    if known:
        return int(known)
    title = as_text(meta.get("show_title")) or (entry["title"] if entry else None) or w["title"]
    key = f"fetched:show:{title.lower()}:{year or ''}"
    row = conn.execute("SELECT id FROM shows WHERE path = ?", (key,)).fetchone()
    if row is not None:
        return int(row["id"])
    genres = genre_list(meta.get("genres")) + (genre_list(entry["genres"]) if entry else [])
    return insert_row(conn, "shows", {
        "source_id": None, "path": key, "title": title, "year": year, "certificate": normalise_cert(as_text(meta.get("certificate"))),
        "genres": json.dumps(sorted(dict.fromkeys(genres))), "plot": as_text(meta.get("plot")),
        "category": "general", "updated_at": now_ts()})


def _deliver_fetched(conn: sqlite3.Connection, wid: int, file: dict[str, Any], meta: dict[str, Any]) -> SlotChanges:
    """Material fetched online: create its catalogue entry from the report, then hand it to the
    line-up and the placeholder slots that asked for it. For an episode the request's season and
    episode win over the report's, so it is filed against what was asked for, except for a series
    with a confirmed match: PiTV asked for "the Nth episode" of something whose seasons it does not
    know, and pitv_content reports the real season, number and title from the episode list
    (contract section 3). The request keeps its N; the catalogue takes what is real. An advert's
    family safety follows the same rule as on import."""
    w = row_to_dict(conn.execute("SELECT * FROM wanted WHERE id = ?", (wid,)).fetchone())
    if w is None:
        return {}
    kind = meta["kind"] if meta.get("kind") in KINDS else w["kind"]
    # The upload's own year where pitv_content could read one, else the year PiTV asked with.
    # That fallback is right for a series, whose episodes really are of its year, and a guess
    # for a strand that ran for decades: a 2019 round of a championship, fetched against an
    # entry dated 1988 and carrying no readable year, would be filed as 1988 and pass an era
    # check it should fail. It is noted rather than refused, because the guess is usually right
    # and nobody can tell from here which case this is.
    read_year = as_int(meta.get("year"))
    year = read_year or w.get("year")
    path = file["path"]
    title = w["title"] if kind == "episode" else (as_text(meta.get("title")) or w["title"])
    season, episode = w.get("season"), w.get("episode")
    matched = kind == "episode" and w.get("lineup_id") and conn.execute(
        "SELECT 1 FROM lineup WHERE id = ? AND match IS NOT NULL", (w["lineup_id"],)).fetchone()
    if matched and as_int(meta.get("season")) is not None and as_int(meta.get("episode")) is not None:
        season, episode = as_int(meta.get("season")), as_int(meta.get("episode"))
        title = as_text(meta.get("title")) or title
    vcodec = as_text(file.get("vcodec"))
    fields = {
        "uid": as_text(meta.get("uid")) or f"fetched:{wid}", "source_id": None, "kind": kind,
        "show_id": _fetched_show(conn, w, meta, year) if kind == "episode" else None,
        "season": season if season is not None else as_int(meta.get("season")),
        "episode": episode if episode is not None else as_int(meta.get("episode")),
        "title": title, "year": year, "origin": "online", "path": path, "cache_path": path, "size": as_int(file.get("size")),
        "duration": as_float(file.get("duration")), "vcodec": vcodec, "acodec": as_text(file.get("acodec")),
        "width": as_int(file.get("width")), "height": as_int(file.get("height")),
        "interlaced": int(as_bool(file.get("interlaced"))), "hwdec": int((vcodec or "") in PI_HW_CODECS),
        "certificate": normalise_cert(as_text(meta.get("certificate"))), "genres": json.dumps(genre_list(meta.get("genres"))),
        "plot": as_text(meta.get("plot")), "artist": as_text(meta.get("artist")) or w.get("artist"),
        "attention": None if read_year or not year else "Year taken from the request, not the file",
        "concert": int(as_bool(meta.get("concert"))),
        "family_safe": family_safe({**meta, "title": title, "path": path},
                                   keyword_pattern(get_setting(conn, "adult_advert_keywords"))) if kind == "advert" else 1,
        "transient": int(bool(w.get("transient"))), "missing": 0, "updated_at": now_ts(),
    }
    existing = conn.execute("SELECT id FROM media WHERE uid = ? OR path = ?", (fields["uid"], path)).fetchone()
    if existing:
        media_id = int(existing["id"])
        update_row(conn, "media", media_id, fields)
    else:
        media_id = insert_row(conn, "media", fields)
    _wanted_done(conn, wid, "delivered by pitv_content", path)
    return attach_delivery(conn, wid, media_id)


def _wanted_done(conn: sqlite3.Connection, wid: int, message: str, dest_path: str | None = None) -> None:
    conn.execute("UPDATE wanted SET status = 'done', progress = 1, dest_path = COALESCE(?, dest_path), message = ?,"
                 " updated_at = ? WHERE id = ?", (dest_path, message, now_ts(), wid))


# pitv_content's word that a series ends before the episode asked for, with the length it knows
# (contract section 3): "no such episode: the series has 6".
_NO_SUCH_EPISODE = re.compile(r"^no such episode\b\D*(\d{1,4})?", re.IGNORECASE)


def _fail_wanted(conn: sqlite3.Connection, wid: int, message: str) -> None:
    msg = (message or "pitv_content failed")[:300]
    if ended := _NO_SUCH_EPISODE.match(msg):
        # Asking again cannot help, and the entry now knows where its run ends, so the scheduler
        # stops asking past it (runs.next_episode_number).
        conn.execute("UPDATE wanted SET status = 'failed', attempts = attempts + 1, message = ?, updated_at = ? WHERE id = ?",
                     (msg, now_ts(), wid))
        if ended.group(1):
            conn.execute("UPDATE lineup SET episode_count = ?, updated_at = ? WHERE id = (SELECT lineup_id FROM wanted WHERE id = ?)",
                         (int(ended.group(1)), now_ts(), wid))
    elif "bot check" in msg.lower() or "rate limit" in msg.lower() or msg.lower().startswith(MISS_PREFIX):
        # The provider, not the request, was the problem, or nothing on offer today says it is the
        # episode wanted (uploads are retitled and new ones appear): retry without using up an attempt.
        conn.execute("UPDATE wanted SET status = 'queued', message = ?, updated_at = ? WHERE id = ?", (msg, now_ts(), wid))
    else:
        conn.execute("UPDATE wanted SET status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'queued' END,"
                     " attempts = attempts + 1, message = ?, updated_at = ? WHERE id = ?",
                     (MAX_WANTED_ATTEMPTS, msg, now_ts(), wid))


def _entries(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The report's entries. Schema 1 carried fetched material in a separate `wanted` list with
    a bare `path` instead of a `file` block; both shapes are read the same way below."""
    return [e for key in ("items", "wanted") if isinstance(report.get(key), list)
            for e in report[key] if isinstance(e, dict)]


def _file_block(e: dict[str, Any]) -> dict[str, Any] | None:
    """The delivered file's properties, or None without a path to it."""
    file = e.get("file") if isinstance(e.get("file"), dict) else {"path": e.get("path")}
    return file if isinstance(file.get("path"), str) and file["path"] else None


def _unreached(run: dict[str, Any]) -> list[str]:
    """What the run never got to, and what it deliberately held back, as run-log details.

    These are two different facts and were once reported as one, which made them impossible to
    judge: a slice that ends before looking at a band is starved, while a request held over for
    a later slice is a rule working as intended, and a count of "not reached" that meant either
    could only be interpreted by guessing. They are now separate at source and stay separate
    here.

    The sentences are written on this side rather than taken from pitv_content's log, so their
    wording is PiTV's and stays stable for the doctor to read back across runs. A band with no
    work in it is not reported, so an entry always means requests that were ready."""
    out = []
    for band in run.get("unreached_bands") or []:
        if not isinstance(band, dict) or not as_int(band.get("requests")):
            continue
        name = as_text(band.get("name")) or f"band {as_int(band.get('band'))}"
        at, of = as_int(band.get("first_at")), as_int(band.get("of"))
        where = f", first at position {at} of {of}" if at and of else ""
        out.append(f"{UNREACHED}{band['requests']} request(s) in {name}, none reached{where}")
    for band in run.get("passed_over") or []:
        if not isinstance(band, dict) or not as_int(band.get("requests")):
            continue
        name = as_text(band.get("name")) or f"band {as_int(band.get('band'))}"
        slices, limit = as_int(band.get("most_slices")) or 0, as_int(band.get("limit")) or 0
        out.append(f"{HELD}{band['requests']} request(s) in {name}, longest waiting {slices} slice(s) of {limit}")
    return out



def _run_of(report: dict[str, Any]) -> tuple[dict[str, Any], str]:
    run = report.get("run") if isinstance(report.get("run"), dict) else {}
    return run, as_text(run.get("tool")) or "pitv_content"


# What each outcome of _apply_entry adds to the report's counts.
_TALLY = {"fetched": ("wanted_done", "created"), "linked": ("wanted_done",), "cached": ("items_done",),
          "wanted_failed": ("wanted_failed",), "items_failed": ("items_failed",)}


def _apply_entry(conn: sqlite3.Connection, e: dict[str, Any]) -> tuple[str | None, SlotChanges]:
    """Apply one report entry. Returns its outcome (a key of _TALLY, or None for an entry that
    changes nothing yet) and the earliest slot change on each channel-day, to rebuild from."""
    status = e.get("status")
    wid, mid = as_int(e.get("wanted_id")) or 0, as_int(e.get("media_id")) or 0
    file = _file_block(e)
    message = as_text(e.get("message")) or ""
    # How long the series ran, which pitv_content reads from the match's episode list and sends
    # with a delivery or a "no such episode" failure. The line-up entry keeps it, and the
    # scheduler asks for nothing past it.
    #
    # This path is why a channel may hold one at all. It arrives with every delivery and every
    # over-ask, so it is replaced as often as PiTV talks to pitv_content and follows a channel
    # that is still growing. What went stale was the count learned once from an online lookup
    # and never revisited; this one cannot, because the next answer overwrites it.
    total = as_int(e["meta"].get("episodes_total")) if isinstance(e.get("meta"), dict) else None
    if wid and total and total > 0:
        conn.execute("UPDATE lineup SET episode_count = ?, updated_at = ? WHERE episode_count IS NOT ?"
                     " AND id = (SELECT lineup_id FROM wanted WHERE id = ?)", (total, now_ts(), total, wid))
    existing_uid = as_text(e.get("existing_uid"))
    if status == "failed" and wid and existing_uid:
        # pitv_content found the request is already in the library (on the NAS): the request is
        # met by that file, not failed.
        existing = conn.execute("SELECT id FROM media WHERE uid = ? AND missing = 0", (existing_uid,)).fetchone()
        if existing is not None:
            _wanted_done(conn, wid, f"already in the library as {existing_uid}")
            return "linked", attach_delivery(conn, wid, int(existing["id"]))
    if status in ("done", "skipped") and file is not None and Path(file["path"]).is_file():
        if wid:
            meta = e.get("meta") if isinstance(e.get("meta"), dict) else {}
            # All or nothing per delivery, so a clash (its uid and its path already belong to
            # two different rows) leaves no half-filed entry behind.
            conn.execute("SAVEPOINT delivery")
            try:
                changes = _deliver_fetched(conn, wid, file, meta)
            except sqlite3.IntegrityError as exc:
                conn.execute("ROLLBACK TO delivery")
                message = f"delivery could not be recorded: {exc}"
            else:
                return "fetched", changes
            finally:
                conn.execute("RELEASE delivery")
        elif mid:
            interlaced = file.get("interlaced")
            update_row(conn, "media", mid, {
                "cache_path": file["path"], "cache_vcodec": as_text(file.get("vcodec")),
                "cache_interlaced": None if interlaced is None else int(as_bool(interlaced)),
                "updated_at": now_ts()})
            real = as_float(file.get("duration"))
            return "cached", _resize_slots(conn, mid, real) if real else {}
        else:
            return None, {}
    elif status == "done":
        message = message or "reported file does not exist"
    elif status != "failed":
        return None, {}   # a skip without a file is still being written: it arrives measured in a later report
    if wid:
        _fail_wanted(conn, wid, message)
        return "wanted_failed", {}
    log.error("pitv_content could not deliver media %s: %s", mid or "?", message)
    return "items_failed", {}


def apply_report(conn: sqlite3.Connection, report: dict[str, Any]) -> dict[str, Any]:
    """Record a delivery report (schema 2; schema 1 is still read during the transition) in one
    transaction, then rebuild the channel-days whose slots changed length."""
    if not isinstance(report, dict):
        raise TypeError("a delivery report is a JSON object")
    counts = {"items_done": 0, "items_failed": 0, "wanted_done": 0, "wanted_failed": 0, "created": 0}
    refill: SlotChanges = {}
    run, tool = _run_of(report)
    with tx(conn):
        for e in _entries(report):
            outcome, changes = _apply_entry(conn, e)
            for key in _TALLY.get(outcome or "", ()):
                counts[key] += 1
            for key, at in changes.items():
                _earliest(refill, key, at)
        started, finished = (as_int(run.get(k)) or now_ts() for k in ("started_ts", "finished_ts"))
        # The summary starts "<tool>:" because _already_applied recognises a run by it.
        summary = (f"{tool}: {counts['items_done']} cached, {counts['items_failed']} failed;"
                   f" fetched {counts['wanted_done']}, {counts['wanted_failed']} failed")
        details = [*_unreached(run), (as_text(run.get("log_tail")) or "")[-4000:]]
        conn.execute("INSERT INTO run_log(kind, started_at, finished_at, status, summary, details) VALUES (?,?,?,?,?,?)",
                     ("content", started, finished, "warning" if counts["items_failed"] or counts["wanted_failed"] else "ok",
                      summary, json.dumps(details)))
    # Each channel-day is rebuilt from its own earliest change. `rebuild_from` goes to the end of
    # one broadcast day, and keeping only a channel's earliest change rebuilt the first day a
    # delivered episode was booked on and left every later airing of it shorter than its slot:
    # PiTV Toons carried six-minute holes on four days after one Battle of the Planets delivery.
    for (channel_id, _day), from_ts in sorted(refill.items(), key=lambda kv: kv[1]):
        if from_ts > now_ts():
            rebuild_from(conn, channel_id, from_ts)
    if counts["created"]:
        write_mirror(conn)   # new catalogue entries
    return counts


def _already_applied(conn: sqlite3.Connection, report: dict[str, Any]) -> bool:
    """pitv_content posts each report and also drops it as a file; a run already recorded (same
    tool and start time) is not applied twice."""
    if not isinstance(report, dict):
        return False
    run, tool = _run_of(report)
    started = as_int(run.get("started_ts"))
    if not started:
        return False
    prefix = f"{tool}:"
    return conn.execute("SELECT 1 FROM run_log WHERE kind = 'content' AND started_at = ? AND substr(summary, 1, ?) = ?",
                        (started, len(prefix), prefix)).fetchone() is not None


def _age_days(path: Path, now: int) -> float:
    try:
        return (now - path.stat().st_mtime) / 86400
    except OSError:
        return 0.0


def _remove_report(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not remove report file %s: %s", path.name, exc)


def apply_report_files(conn: sqlite3.Connection, cache: MediaCache) -> int:
    """Apply report JSON files pitv_content dropped in `<cache>/reports` that PiTV has not seen
    (the web service may already have taken the same report over HTTP); each file handled gets a
    `.applied` marker next to it. A file that cannot be applied is left for the next pass.

    The folder is kept short, since it is read on every maintenance pass: an applied report goes
    with its marker after APPLIED_REPORT_DAYS, one that never applied after UNAPPLIED_REPORT_DAYS.
    Only files listed in that folder are ever removed."""
    d = cache.reports_dir
    if d is None or not d.is_dir():
        return 0
    now = now_ts()
    for marker in d.glob("*.json.applied"):
        if not marker.with_suffix("").exists():   # its report is gone
            _remove_report(marker)
    applied = 0
    for f in sorted(d.glob("*.json")):
        done = f.with_suffix(".json.applied")
        if done.exists():
            if _age_days(done, now) > APPLIED_REPORT_DAYS:
                _remove_report(f, done)
            continue
        if _age_days(f, now) > UNAPPLIED_REPORT_DAYS:
            log.warning("report file %s never applied in %d days; removed", f.name, UNAPPLIED_REPORT_DAYS)
            _remove_report(f)
            continue
        try:
            report = json.loads(f.read_text())
            if not _already_applied(conn, report):
                apply_report(conn, report)
                applied += 1
            done.write_text(str(now_ts()))
        except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
            log.warning("report file %s not applied (will retry): %s", f.name, exc)
    return applied
