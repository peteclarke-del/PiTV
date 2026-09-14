import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pitv import db as dbm
from pitv.config import Config
from pitv.devtools import build_fake_library
from pitv.library.scanner import scan_all
from pitv.scheduler.build import build_horizon, parse_day
from pitv.scheduler.rules import local_ts, tz_of


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("pitv")
    lib = build_fake_library(root / "lib", max_episodes_per_show=6)
    cfg = Config(data_dir=root / "data", run_dir=root / "run")
    os.environ.pop("PITV_DB", None)
    cfg.ensure_dirs()
    conn = dbm.connect(cfg.db_path)
    dbm.init_db(conn)
    with dbm.tx(conn):
        for stype, name, p in (("tv", "TV", lib["tv"]), ("movie", "Movies", lib["movies"]),
                               ("advert", "Ads", lib["pitv"] / "Adverts"), ("ident", "Idents", lib["pitv"] / "Idents")):
            conn.execute("INSERT INTO sources(type, name, path) VALUES (?,?,?)", (stype, name, str(p)))
    scan_all(conn)
    now = local_ts(parse_day("2026-09-14"), "12:00", tz_of(conn))
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=2, now=now, seed=7)
    conn.close()
    return cfg


@pytest.fixture(scope="module")
def client(env):
    from pitv.web.app import create_app
    with TestClient(create_app(env)) as c:
        yield c


def test_now_and_schedule(client):
    r = client.get("/api/now")
    assert r.status_code == 200
    assert len(r.json()["channels"]) == 4
    d = client.get("/api/schedule/day/2026-09-14").json()
    assert d["slots"]
    kinds = {s["kind"] for s in d["slots"]}
    assert kinds <= {"programme", "filler"}
    days = client.get("/api/schedule/days").json()
    assert [x["day"] for x in days["days"]] == ["2026-09-14", "2026-09-15"]


def test_admin_open_until_password(client):
    assert client.get("/api/auth").json() == {"password_set": False, "admin": True}
    assert client.get("/api/sources").status_code == 200


def test_library_and_shows(client):
    shows = client.get("/api/shows").json()
    assert len(shows) >= 20
    sid = shows[0]["id"]
    detail = client.get(f"/api/shows/{sid}").json()
    assert detail["episodes"]
    upd = client.put(f"/api/shows/{sid}", json={"certificate": "18", "mode": "strip", "anchor_time": "17:30"}).json()
    assert upd["certificate"] == "18" and upd["mode"] == "strip"
    upd = client.put(f"/api/shows/{sid}", json={"certificate": None}).json()
    assert upd["certificate"] == upd["scanned"]["certificate"]
    att = client.get("/api/library/attention").json()
    assert any("Mystery" in a["title"] for a in att)
    summary = client.get("/api/library/summary").json()
    assert summary["kinds"]["movie"] == 25


def test_channels_and_settings(client):
    chans = client.get("/api/channels").json()
    assert [c["number"] for c in chans] == [1, 2, 3, 4]
    c5 = client.post("/api/channels", json={"name": "PiTV Five", "short_name": "Five", "pattern": "show, break"}).json()
    assert c5["number"] == 5 and c5["pattern"] == "show, break"
    assert client.delete(f"/api/channels/{c5['id']}").json()["ok"]
    s = client.get("/api/settings").json()
    assert "admin_password_hash" not in s
    s2 = client.put("/api/settings", json={"movie_repeat_days": 10}).json()
    assert s2["movie_repeat_days"] == 10
    assert client.put("/api/settings", json={"nope": 1}).status_code == 400


def test_schedule_edit(client):
    day = client.get("/api/schedule/day/2026-09-15").json()
    ch1 = [s for s in day["slots"] if s["channel_id"] == day["channels"][0]["id"] and s["kind"] == "programme"]
    target = ch1[5]
    r = client.post(f"/api/schedule/slots/{target['id']}/lock", json={"locked": True})
    assert r.json()["locked"] == 1
    movies = client.get("/api/library/search", params={"q": "Labyrinth", "kind": "movie"}).json()
    assert movies
    r = client.post(f"/api/schedule/slots/{ch1[6]['id']}/replace", json={"media_id": movies[0]["id"]})
    assert r.status_code == 200, r.text
    day2 = client.get("/api/schedule/day/2026-09-15").json()
    titles = [s["title"] for s in day2["slots"] if s["channel_id"] == day["channels"][0]["id"]]
    assert "Labyrinth" in titles
    r = client.delete(f"/api/schedule/slots/{ch1[7]['id']}")
    assert r.status_code == 200, r.text


def test_password_flow(client):
    assert client.post("/api/auth/setup", json={"password": "secret123"}).status_code == 200
    assert client.get("/api/auth").json()["admin"] is True
    client.post("/api/auth/logout")
    client.cookies.clear()
    assert client.get("/api/sources").status_code == 401
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "secret123"}).status_code == 200
    assert client.get("/api/sources").status_code == 200


def test_player_offline(client):
    r = client.get("/api/player").json()
    assert r["offline"] is True
