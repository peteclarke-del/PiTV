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


def test_a_restore_matches_rows_by_what_they_are(station):
    """Ids are not carried: a rebuilt database numbers its rows afresh, so a channel is found by
    its number and a source by its name and root. A backup that carried ids would either clash
    with the new numbering or quietly write a channel's settings onto a different channel."""
    doc = backup.export(station)
    assert all("id" not in c for c in doc["channels"]), "a channel's id is not part of the document"
    assert all("id" not in s for s in doc["sources"]), "nor a source's"
    named = {(s["name"], s["path"]) for s in doc["sources"]}
    assert len(named) == len(doc["sources"]), "name and root tell the sources apart"
