"""Everything the owner has set, as one document, and putting it back.

The library is not in here and is not meant to be: shows, episodes, films and adverts come
back from pitv_content's index, and the cache refills itself. What cannot be rebuilt is what
somebody decided, and that is what this covers: the settings, the channels with their bands,
the sources, the line-ups and every show or media override.

The line-up is the part worth being careful about. It is the largest thing built by hand, it
names titles that may not be indexed yet, and it has its own document already (`lineup.export`,
mirrored beside the database), so it is carried here as that document and restored by the same
code. Nothing is invented on the way back in: a channel is matched by its number, a source by
its name and root, a show or a film by the identity the index gives it, and anything that finds
no home is counted and reported rather than guessed at.

pitv_content's configuration travels too, and this is the part worth explaining. Its sources,
providers and keys are typed into PiTV's admin and relayed straight through, because
pitv_content has no interface of its own; the application that owns the interface holds none of
the data behind it, so a PiTV backup used to carry none of it either. Rather than PiTV keeping
a copy that could disagree with the one in use, pitv_content is asked for its configuration at
the moment of backup and handed it back on a restore. Its keys and share passwords are masked
on an ordinary read, which is right for a screen somebody is looking at and useless for a
backup, so they are asked for explicitly and the document says when it holds them.

A restore is one transaction. A file that is damaged half way through leaves the station
exactly as it was, because a half-applied configuration is worse than the one it replaced.
pitv_content's half follows outside that transaction, since it is a call to another application
over HTTP: it can fail on its own, and when it does PiTV's half is already safely in place.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import lineup as lineup_mod
from . import tool_client
from .db import (
    DEFAULT_SETTINGS,
    all_settings,
    columns,
    now_ts,
    rows_to_dicts,
    set_setting,
    tx,
    update_row,
)
from .scheduler import bands as band_rules
from .settings_rules import SECRET_SETTINGS, SettingError, check_setting

SCHEMA = 3
# pitv_content's own backup endpoints. It keeps its configuration and answers for it; PiTV asks
# for it at the moment of backup and gives it back on a restore, so neither application holds a
# second copy of the other's settings to disagree with.
CONTENT_EXPORT = "export"
CONTENT_IMPORT = "import"
# Written from the table and put back into it. `id` is left out everywhere: the rows are matched
# on what they are rather than on the number this database happened to give them, so a restore
# into a rebuilt database lands correctly.
_SKIP = {"id"}


def _clean(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> dict[str, Any]:
    """The fields of `row` that this database's table actually has, JSON columns re-encoded."""
    known = set(columns(conn, table)) - _SKIP
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key not in known:
            continue
        out[key] = json.dumps(value) if isinstance(value, (dict, list)) else value
    return out


def _content_config(settings: dict[str, Any], token: str, *, secrets: bool) -> dict[str, Any]:
    """pitv_content's own configuration, asked for at the moment of backup.

    Its sources, providers and keys are typed into PiTV's admin and relayed straight through,
    because pitv_content has no interface of its own, so the application that owns the interface
    holds none of the data behind it and a PiTV backup carried none of it. They stay
    pitv_content's to own: it is asked for them here and given them back on a restore, rather
    than PiTV keeping a second copy that could disagree with the one in use.

    Its keys and share passwords are masked on an ordinary read, which is right for a screen
    somebody is looking at and useless for a backup: restoring a mask would write asterisks over
    a working key. They are asked for explicitly instead, and a document holding them says so,
    because the file then wants keeping like a password rather than like a settings dump.

    The form carrying secrets is a POST with the shared token, never a GET with a query. A URL
    saying `secrets=1` is written down by everything that records a request line and the answer
    is cacheable; and pitv_content's API has no authentication of its own, so without the token
    the masking is the only thing keeping those keys from any local process that can open a
    socket to it. The plain form stays a GET: it is the same document already masked."""
    base = tool_client.base_url(settings)
    if secrets:
        status, payload = tool_client.request(base, "POST", CONTENT_EXPORT, body={"secrets": True},
                                              timeout=15, token=token)
    else:
        status, payload = tool_client.request(base, "GET", CONTENT_EXPORT, timeout=15)
    if status == 404:
        # A pitv_content from before it could be backed up. Its configuration is still a few
        # screens of typing, so the backup says plainly that it does not hold it.
        return {"available": False, "reason": "this version of pitv_content cannot export its configuration"}
    if status != 200 or not isinstance(payload, dict):
        detail = payload.get("error") if isinstance(payload, dict) else f"HTTP {status}"
        return {"available": False, "reason": str(detail)}
    return {"available": True, "holds_secrets": bool(secrets and payload.get("secrets")), "document": payload}


def export(conn: sqlite3.Connection, *, content_secrets: bool = True, token: str = "") -> dict[str, Any]:
    """The whole of the owner's configuration, PiTV's and pitv_content's.

    `token` is the shared credential, needed only to ask pitv_content for its keys; without one
    the backup holds everything else and says which half it is missing."""
    settings = {k: v for k, v in all_settings(conn).items() if k not in SECRET_SETTINGS}
    channels = []
    for row in rows_to_dicts(conn.execute("SELECT * FROM channels ORDER BY number")):
        channels.append({**{k: v for k, v in row.items() if k not in _SKIP},
                         "bands": band_rules.export(conn, row["id"])})
    shows = [{"uid": r["path"], "title": r["title"], "overrides": json.loads(r["overrides"]),
              "home_channel_number": r["home_channel_number"], "mode": r["mode"], "anchor_time": r["anchor_time"],
              "anchor_days": r["anchor_days"], "rest_weeks": r["rest_weeks"], "excluded": r["excluded"]}
             for r in conn.execute(
                 "SELECT sh.*, c.number AS home_channel_number FROM shows sh"
                 " LEFT JOIN channels c ON c.id = sh.home_channel_id"
                 " WHERE sh.overrides != '{}' OR sh.excluded = 1 OR sh.mode != 'auto'")]
    media = [{"uid": r["uid"], "title": r["title"], "overrides": json.loads(r["overrides"]), "excluded": r["excluded"]}
             for r in conn.execute("SELECT * FROM media WHERE overrides != '{}' OR excluded = 1")]
    return {"schema": SCHEMA, "exported_at": now_ts(), "settings": settings, "channels": channels,
            "sources": [{k: v for k, v in r.items() if k not in _SKIP}
                        for r in rows_to_dicts(conn.execute("SELECT * FROM sources ORDER BY name"))],
            "lineup": lineup_mod.export(conn), "shows": shows, "media": media,
            "content": _content_config(all_settings(conn), token, secrets=content_secrets)}


def _restore_settings(conn: sqlite3.Connection, doc: dict[str, Any], count: dict[str, int]) -> None:
    for key, value in (doc.get("settings") or {}).items():
        if key not in DEFAULT_SETTINGS or key in SECRET_SETTINGS:
            count["unknown_settings"] += 1
            continue
        try:
            set_setting(conn, key, check_setting(key, value))
        except SettingError:
            # A value this build no longer accepts, most often a range that has since narrowed.
            count["unknown_settings"] += 1
        else:
            count["settings"] += 1


def _restore_channels(conn: sqlite3.Connection, doc: dict[str, Any], count: dict[str, int]) -> None:
    """Channels are matched by number, which is what the owner tunes to and what a line-up
    document names. A channel in the file that this station does not have is counted, never
    created: the number alone does not say what it is for, and inventing one would put a
    nameless channel in the guide."""
    by_number = {r["number"]: r["id"] for r in conn.execute("SELECT id, number FROM channels")}
    for row in doc.get("channels") or []:
        cid = by_number.get(row.get("number")) if isinstance(row, dict) else None
        if cid is None:
            count["unknown_channels"] += 1
            continue
        update_row(conn, "channels", cid, _clean(conn, "channels", row))
        count["channels"] += 1
        if isinstance(row.get("bands"), list):
            try:
                cleaned = [band_rules.clean(b) for b in row["bands"]]
            except ValueError:
                count["skipped_bands"] += len(row["bands"])
            else:
                count["bands"] += band_rules.save(conn, cid, cleaned, now_ts())


def _restore_sources(conn: sqlite3.Connection, doc: dict[str, Any], count: dict[str, int]) -> None:
    """A source is its name and the root pitv_content indexes, so that pair identifies it. One
    that is not here is inserted: a source carries where material comes from, and losing it
    loses the mapping between the index and the disk."""
    existing = {(r["name"], r["path"]): r["id"] for r in conn.execute("SELECT id, name, path FROM sources")}
    for row in doc.get("sources") or []:
        if not (isinstance(row, dict) and row.get("name") and row.get("path")):
            count["skipped_sources"] += 1
            continue
        fields = _clean(conn, "sources", row)
        sid = existing.get((row["name"], row["path"]))
        if sid is None:
            cols = tuple(fields)
            conn.execute(f"INSERT INTO sources({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         tuple(fields[c] for c in cols))
        else:
            update_row(conn, "sources", sid, fields)
        count["sources"] += 1


def _restore_overrides(conn: sqlite3.Connection, doc: dict[str, Any], count: dict[str, int]) -> None:
    """Overrides only reach titles the index has delivered. One whose title is not here yet is
    counted as waiting rather than lost: importing the catalogue again and restoring again
    applies it, and that is a better answer than writing a row the library knows nothing about."""
    channels = {r["number"]: r["id"] for r in conn.execute("SELECT id, number FROM channels")}
    for row in doc.get("shows") or []:
        found = conn.execute("SELECT id FROM shows WHERE path = ?", (row.get("uid"),)).fetchone()
        if not found:
            count["waiting"] += 1
            continue
        fields = {k: row[k] for k in ("mode", "anchor_time", "anchor_days", "rest_weeks", "excluded") if k in row}
        fields["overrides"] = json.dumps(row.get("overrides") or {})
        if "home_channel_number" in row:
            fields["home_channel_id"] = channels.get(row["home_channel_number"])
        update_row(conn, "shows", found["id"], fields)
        count["shows"] += 1
    for row in doc.get("media") or []:
        found = conn.execute("SELECT id FROM media WHERE uid = ?", (row.get("uid"),)).fetchone()
        if not found:
            count["waiting"] += 1
            continue
        update_row(conn, "media", found["id"],
                   {"overrides": json.dumps(row.get("overrides") or {}), "excluded": int(bool(row.get("excluded")))})
        count["media"] += 1


def _restore_content(settings: dict[str, Any], token: str, doc: dict[str, Any], count: dict[str, Any]) -> None:
    """Hand pitv_content its own configuration back, keys included where the backup holds them.

    It carries the shared token because writing this is at least as sensitive as reading it:
    pitv_content's API has no authentication of its own, and an import nobody has to prove
    themselves for could point every source at another machine's shares.

    This runs after PiTV's own restore and outside its transaction, because it is a call to
    another application over HTTP: it can fail on its own, and when it does, PiTV's half is
    already safely in place. What went wrong is reported rather than raised, so a restore does
    not appear to have failed when the only thing missing is a service that is not running."""
    content = doc.get("content")
    if not (isinstance(content, dict) and content.get("available") and isinstance(content.get("document"), dict)):
        count["content"] = "the backup does not hold pitv_content's configuration"
        return
    status, payload = tool_client.request(tool_client.base_url(settings), "POST", CONTENT_IMPORT,
                                          body=content["document"], timeout=30, token=token)
    if status == 404:
        count["content"] = "this version of pitv_content cannot be restored into"
    elif status >= 400 or not isinstance(payload, dict):
        detail = payload.get("error") if isinstance(payload, dict) else f"HTTP {status}"
        count["content"] = f"pitv_content refused its configuration: {detail}"
    else:
        count["content"] = payload


def restore(conn: sqlite3.Connection, doc: Any, token: str = "") -> dict[str, Any]:
    """Put a backup document back, in one transaction. Raises TypeError for a file that is not
    one of ours and ValueError for one this build cannot read; anything readable but unplaceable
    is counted in the result."""
    if not isinstance(doc, dict) or not isinstance(doc.get("settings"), dict):
        raise TypeError("this is not a PiTV backup: expected a document with a settings object")
    if int(doc.get("schema") or 0) > SCHEMA:
        raise ValueError(f"this backup was written by a later version of PiTV (schema {doc['schema']})")
    count: dict[str, Any] = dict.fromkeys(
        ("settings", "unknown_settings", "channels", "unknown_channels", "bands", "skipped_bands",
         "sources", "skipped_sources", "shows", "media", "waiting", "lineup_entries"), 0)
    with tx(conn):
        _restore_settings(conn, doc, count)
        _restore_channels(conn, doc, count)
        _restore_sources(conn, doc, count)
        _restore_overrides(conn, doc, count)
    # The line-up brings its own transaction and rewrites its mirror, so it runs after the rest
    # rather than inside it; by now the channels it names are the restored ones.
    if isinstance(doc.get("lineup"), dict):
        count["lineup_entries"] = lineup_mod.import_doc(conn, doc["lineup"]).get("entries", 0)
    _restore_content(all_settings(conn), token, doc, count)
    return count
