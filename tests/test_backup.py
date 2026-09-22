"""Backing the station's configuration up and putting it back.

The library is deliberately outside a backup: it comes back from pitv_content's index. What
these cover is the rest, the part somebody built by hand, which nothing else can regenerate.
"""

import json

import pytest
from conftest import make_library

from pitv import backup
from pitv import db as dbm
from pitv.scheduler import bands as band_rules


@pytest.fixture
def station(tmp_path):
    ctx = make_library(tmp_path, max_episodes=3)
    yield ctx["conn"]
    ctx["conn"].close()


def test_a_backup_carries_what_nobody_can_rebuild(station):
    """The export once held settings, channels, sources and overrides, and stopped there. The
    line-ups and the bands were missing, which is the configuration that takes longest to build
    and the only part pitv_content cannot hand back."""
    doc = backup.export(station)
    assert doc["schema"] == backup.SCHEMA
    assert doc["settings"] and doc["channels"] and doc["sources"]
    assert doc["lineup"]["channels"], "the line-up travels as its own document"
    assert any(c["bands"] for c in doc["channels"]), "a channel's bands travel with it"
    assert "admin_password_hash" not in doc["settings"] and "session_secret" not in doc["settings"]
    assert "shows" in doc and "media" in doc
    # It must survive the trip to a file and back, which is the only way it is ever used.
    assert json.loads(json.dumps(doc)) == doc


def test_a_restore_puts_the_configuration_back(station):
    """A backup is only worth taking if it can be applied, and there was no way to apply one."""
    music = station.execute("SELECT id, number FROM channels WHERE content = 'music'").fetchone()
    first = station.execute("SELECT id, number, name FROM channels ORDER BY number LIMIT 1").fetchone()
    with dbm.tx(station):
        dbm.set_setting(station, "horizon_days", 9)
        band_rules.save(station, music["id"], [band_rules.clean(
            {"name": "Teatime", "start": "17:30", "minutes": 90, "days": [0, 1, 2, 3, 4],
             "fill": {"kinds": ["music"], "genres": ["Pop"]}, "enabled": True})], dbm.now_ts())
        station.execute("UPDATE channels SET name = 'Renamed', colour = '#123456' WHERE id = ?", (first["id"],))
    doc = backup.export(station)

    # Now undo all of it, as a rebuilt database would.
    with dbm.tx(station):
        dbm.set_setting(station, "horizon_days", 2)
        station.execute("DELETE FROM band")
        station.execute("UPDATE channels SET name = 'Something Else', colour = '#000000' WHERE id = ?", (first["id"],))
        station.execute("DELETE FROM lineup")

    result = backup.restore(station, doc)
    assert dbm.get_setting(station, "horizon_days") == 9
    back = station.execute("SELECT name, colour FROM channels WHERE id = ?", (first["id"],)).fetchone()
    assert (back["name"], back["colour"]) == ("Renamed", "#123456")
    bands = band_rules.export(station, music["id"])
    assert [b["name"] for b in bands] == ["Teatime"] and bands[0]["start"] == "17:30" and bands[0]["minutes"] == 90
    assert result["lineup_entries"] > 0
    assert station.execute("SELECT COUNT(*) FROM lineup").fetchone()[0] > 0


def test_a_damaged_backup_leaves_the_station_alone(station):
    """Half a configuration is worse than the one it would replace, so a restore is one
    transaction and a file that is not ours is refused before anything is written."""
    before = dbm.get_setting(station, "horizon_days")
    with pytest.raises(TypeError):
        backup.restore(station, {"nothing": "useful"})
    with pytest.raises(TypeError):
        backup.restore(station, ["not even an object"])
    with pytest.raises(ValueError):
        backup.restore(station, {"settings": {}, "schema": backup.SCHEMA + 1})
    assert dbm.get_setting(station, "horizon_days") == before


def test_a_restore_reports_what_found_no_home(station):
    """Nothing is invented on the way in: a channel this station does not have, a setting this
    build no longer knows and an override for a title the library has not been given are each
    counted, so the person restoring is told rather than left to find out."""
    doc = backup.export(station)
    doc["channels"].append({"number": 99, "name": "A Channel From Somewhere Else"})
    doc["settings"]["a_setting_that_went_away"] = 3
    doc["shows"].append({"uid": "/nowhere/Not In This Library", "overrides": {"year": 1984}})
    result = backup.restore(station, doc)
    assert result["unknown_channels"] == 1
    assert result["unknown_settings"] == 1
    assert result["waiting"] == 1
    assert not station.execute("SELECT 1 FROM channels WHERE number = 99").fetchone()


def test_the_content_app_is_asked_for_its_own_configuration(station, monkeypatch):
    """Sources, providers and API keys are typed into PiTV's admin and relayed straight through,
    because pitv_content has no interface of its own. So the application that owns the interface
    held none of the data behind it, and a backup taken here carried none of it: losing the card
    lost eleven source definitions and two third-party keys that no amount of re-indexing brings
    back. It is asked for them now, rather than PiTV keeping a copy that could disagree."""
    from pitv import tool_client

    calls = []

    def fake(base, method, path, query="", body=None, timeout=15, token=""):
        calls.append({"method": method, "path": path, "query": query, "body": body, "token": token})
        wants_secrets = method == "POST" and path == backup.CONTENT_EXPORT and (body or {}).get("secrets")
        if path == backup.CONTENT_EXPORT:
            return 200, {"schema": 1, "secrets": bool(wants_secrets),
                         "settings": {"tmdb_api_key": "a-real-key" if wants_secrets else "********"},
                         "sources": [{"id": "tvshows"}], "providers": [{"id": "youtube"}]}
        return 200, {"settings": 1, "sources": 1, "providers": 1}

    monkeypatch.setattr(tool_client, "request", fake)
    doc = backup.export(station, token="shared-token")
    assert doc["content"]["available"] and doc["content"]["holds_secrets"]
    assert doc["content"]["document"]["settings"]["tmdb_api_key"] == "a-real-key", "a mask is useless in a backup"
    # The keys are asked for in a body, not a query, and the request proves who is asking:
    # pitv_content's API has no authentication of its own, so a URL saying secrets=1 would put
    # them within reach of anything on this machine that can open a socket to it, and would be
    # written down by everything that records a request line.
    asked = calls[-1]
    assert asked["method"] == "POST" and asked["query"] == "" and asked["token"] == "shared-token"

    # Asked for without them, the key is left masked and the document says it holds none.
    plain = backup.export(station, content_secrets=False, token="shared-token")
    assert plain["content"]["holds_secrets"] is False
    assert plain["content"]["document"]["settings"]["tmdb_api_key"] == "********"
    assert calls[-1]["method"] == "GET" and not calls[-1]["token"], "the masked form needs nothing"

    calls.clear()
    result = backup.restore(station, doc, token="shared-token")
    posted = [c for c in calls if c["path"] == backup.CONTENT_IMPORT]
    assert posted and posted[0]["body"]["settings"]["tmdb_api_key"] == "a-real-key", "the key goes back as it came"
    # Writing this is at least as sensitive as reading it: an import nobody has to prove
    # themselves for could point every source at another machine's shares.
    assert posted[0]["token"] == "shared-token"
    assert isinstance(result["content"], dict)


def test_the_station_still_comes_back_without_the_content_app(station, monkeypatch):
    """pitv_content's half is a call to another application, so it can fail on its own: it is
    not running, or it is a version from before it could be backed up. PiTV's own restore has
    already happened by then and must stand, with the reason reported rather than raised."""
    from pitv import tool_client

    monkeypatch.setattr(tool_client, "request", lambda *a, **k: (404, {"error": "unknown endpoint"}))
    doc = backup.export(station)
    assert doc["content"]["available"] is False and doc["content"]["reason"]

    monkeypatch.setattr(tool_client, "request", lambda *a, **k: (503, {"offline": True, "error": "not running"}))
    offline = backup.export(station)
    assert offline["content"]["available"] is False and "not running" in offline["content"]["reason"]

    with dbm.tx(station):
        dbm.set_setting(station, "horizon_days", 2)
    result = backup.restore(station, doc)
    assert dbm.get_setting(station, "horizon_days") == doc["settings"]["horizon_days"], "PiTV's own half stands"
    assert isinstance(result["content"], str), "and says why the other half did not happen"


def test_a_restore_matches_rows_by_what_they_are(station):
    """Ids are not carried: a rebuilt database numbers its rows afresh, so a channel is found by
    its number and a source by its name and root. A backup that carried ids would either clash
    with the new numbering or quietly write a channel's settings onto a different channel."""
    doc = backup.export(station)
    assert all("id" not in c for c in doc["channels"]), "a channel's id is not part of the document"
    assert all("id" not in s for s in doc["sources"]), "nor a source's"
    named = {(s["name"], s["path"]) for s in doc["sources"]}
    assert len(named) == len(doc["sources"]), "name and root tell the sources apart"
