import os
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from pitv import db as dbm
from pitv.config import Config
from pitv.db import DEFAULT_SETTINGS, now_ts
from pitv.devtools import build_fake_library
from pitv.library.scanner import scan_all
from pitv.scheduler.build import build_horizon
from pitv.scheduler.rules import broadcast_day_for

# The schedule editor refuses slots that have already started on the real clock, so the
# fixture builds today and tomorrow rather than fixed dates.
DAY0 = broadcast_day_for(now_ts(), DEFAULT_SETTINGS, ZoneInfo(DEFAULT_SETTINGS["timezone"]))
DAY1 = DAY0 + timedelta(days=1)


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
    build_horizon(conn, start_day=DAY0, days=2, seed=7)
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
    assert len(r.json()["channels"]) == 6
    d = client.get(f"/api/schedule/day/{DAY0}").json()
    assert d["slots"]
    kinds = {s["kind"] for s in d["slots"]}
    assert kinds <= {"programme", "filler"}
    days = client.get("/api/schedule/days").json()
    assert [x["day"] for x in days["days"]] == [DAY0.isoformat(), DAY1.isoformat()]


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
    assert summary["kinds"]["movie"] == 32


def test_channels_and_settings(client):
    chans = client.get("/api/channels").json()
    assert [c["number"] for c in chans] == [1, 2, 3, 4, 5, 6]
    assert [c["content"] for c in chans][-2:] == ["music", "cartoons"]
    c5 = client.post("/api/channels", json={"name": "PiTV Seven", "short_name": "Seven", "pattern": "show, break"}).json()
    assert c5["number"] == 7 and c5["pattern"] == "show, break"
    assert client.put(f"/api/channels/{c5['id']}", json={"content": "bogus"}).status_code == 400
    off = client.put(f"/api/channels/{c5['id']}", json={"enabled": False}).json()
    assert off["enabled"] == 0
    assert client.delete(f"/api/channels/{c5['id']}").json()["ok"]
    s = client.get("/api/settings").json()
    assert "admin_password_hash" not in s
    s2 = client.put("/api/settings", json={"movie_repeat_days": 10}).json()
    assert s2["movie_repeat_days"] == 10
    assert client.put("/api/settings", json={"nope": 1}).status_code == 400


def test_schedule_edit(client):
    day = client.get(f"/api/schedule/day/{DAY1}").json()
    ch1 = [s for s in day["slots"] if s["channel_id"] == day["channels"][0]["id"] and s["kind"] == "programme"]
    target = ch1[5]
    r = client.post(f"/api/schedule/slots/{target['id']}/lock", json={"locked": True})
    assert r.json()["locked"] == 1
    movies = client.get("/api/library/search", params={"q": "Labyrinth", "kind": "movie"}).json()
    assert movies
    r = client.post(f"/api/schedule/slots/{ch1[6]['id']}/replace", json={"media_id": movies[0]["id"]})
    assert r.status_code == 200, r.text
    day2 = client.get(f"/api/schedule/day/{DAY1}").json()
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


def test_wanted_list(client):
    r = client.post("/api/wanted", json={"kind": "movie", "title": "Some Public Domain Film", "year": 1984})
    assert r.status_code == 200, r.text
    wid = r.json()["id"]
    assert any(w["id"] == wid for w in client.get("/api/wanted").json())
    assert client.post("/api/wanted", json={"kind": "movie", "title": "X", "ref": "ftp://nope"}).status_code == 400
    assert client.post(f"/api/wanted/{wid}/retry").json()["ok"]
    assert client.delete(f"/api/wanted/{wid}").json()["ok"]
    gaps = client.post("/api/wanted/scan-gaps").json()
    assert gaps["added"] == 0  # fake library has no gaps


def test_gap_detection(env):
    from pitv.wanted import queue_gaps
    conn = dbm.connect(env.db_path)
    show = conn.execute("SELECT id FROM shows WHERE title = 'Blackadder'").fetchone()
    with dbm.tx(conn):
        conn.execute("UPDATE media SET missing = 1 WHERE show_id = ? AND season = 1 AND episode = 3", (show["id"],))
    assert queue_gaps(conn) == 1
    row = conn.execute("SELECT * FROM wanted WHERE show_id = ?", (show["id"],)).fetchone()
    assert (row["season"], row["episode"], row["auto"]) == (1, 3, 1)
    assert queue_gaps(conn) == 0
    with dbm.tx(conn):
        conn.execute("UPDATE media SET missing = 0 WHERE show_id = ?", (show["id"],))
        conn.execute("DELETE FROM wanted")
    conn.close()


def test_content_manifest_and_report(client):
    m = client.get("/api/content/manifest", params={"days": 1}).json()
    assert m["items"] and all(k in m["items"][0] for k in ("media_id", "path", "target", "action", "first_air_ts", "channels"))
    assert m["items"] == sorted(m["items"], key=lambda i: i["first_air_ts"])
    assert all(i["action"] == "copy" for i in m["items"])  # fake library is tiny h264
    w = client.post("/api/wanted", json={"kind": "music", "title": "Queen - Radio Ga Ga", "year": 1984, "genre": "pop"}).json()
    assert w["genre"] == "pop"
    m = client.get("/api/content/manifest").json()
    mine = [x for x in m["wanted"] if x["wanted_id"] == w["id"]]
    assert mine and mine[0]["dest_dir"].endswith("music videos/Pop")
    r = client.post("/api/content/report", json={"items": [{"media_id": m["items"][0]["media_id"], "path": "/nonexistent.mp4", "status": "done"}],
                                                  "wanted": [{"wanted_id": w["id"], "path": "/x/y.mp4", "status": "done"}],
                                                  "run": {"tool": "pitv_content test", "started_ts": 1, "finished_ts": 2}}).json()
    assert r["ok"] and r["wanted_done"] == 1
    assert [x for x in client.get("/api/wanted").json() if x["id"] == w["id"]][0]["status"] == "done"
    client.delete(f"/api/wanted/{w['id']}")


def test_content_manifest_ignores_part_files(client, tmp_path):
    """A half-written `<id>_*.part` from pitv_content must not count as cached (PLAN.md §7)."""
    cache = tmp_path / "cache"
    cache.mkdir()
    client.put("/api/settings", json={"cache_dir": str(cache)})
    try:
        item = client.get("/api/content/manifest").json()["items"][0]
        target = cache / os.path.basename(item["target"])
        assert item["already_cached"] is False and target.parent == cache
        target.with_name(target.name + ".part").write_bytes(b"x")
        item = [i for i in client.get("/api/content/manifest").json()["items"] if i["media_id"] == item["media_id"]][0]
        assert item["already_cached"] is False
        target.write_bytes(b"x")
        item = [i for i in client.get("/api/content/manifest").json()["items"] if i["media_id"] == item["media_id"]][0]
        assert item["already_cached"] is True
        assert client.post("/api/content/make-room", json={"bytes": 1}).json()["ok"] is True
        assert target.exists(), "files in the current manifest are protected from eviction"
    finally:
        client.put("/api/settings", json={"cache_dir": ""})


def test_content_tool_status(client):
    t = client.get("/api/content/tool").json()
    assert "installed" in t and "reports" in t
    r = client.post("/api/content/readiness", json={"days": 1}).json()
    assert r["status"] in ("ok", "warning", "error") and r["checked"] > 0


def test_content_tool_proxy_offline(client):
    client.put("/api/settings", json={"content_tool_url": "http://127.0.0.1:9"})  # nothing listens there
    r = client.get("/api/content/tool/api/settings")
    assert r.status_code == 503 and r.json()["offline"] is True
    assert client.get("/api/content/tool/api/evil").status_code == 404


def test_content_tool_proxy_non_json_and_log_shape(client):
    """An unrelated service on the tool's port (HTML 404) means 'not installed', never a forwarded
    HTML body; a real log reply is passed through in the /api/logs/{name} shape."""
    import http.server
    import json
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/log"):
                body, ctype, code = json.dumps({"name": "pitv-content", "lines": []}).encode(), "application/json", 200
            else:
                body, ctype, code = b"<html><body><h1>Not Found</h1></body></html>", "text/html", 404
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client.put("/api/settings", json={"content_tool_url": f"http://127.0.0.1:{srv.server_port}"})
        r = client.get("/api/content/tool/api/settings")
        assert r.status_code == 503, r.text
        assert r.json()["offline"] is True and "non-JSON" in r.json()["error"] and "HTTP 404" in r.json()["error"]
        r = client.get("/api/content/tool/api/log", params={"lines": 5})
        assert r.status_code == 200 and r.json() == {"name": "pitv-content", "lines": [], "path": None, "exists": True}
    finally:
        srv.shutdown()
        srv.server_close()


# --- security -------------------------------------------------------------------------------------

def test_session_cookie_flags(client):
    r = client.post("/api/auth/login", json={"password": "secret123"})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" not in cookie
    r = client.post("/api/auth/login", json={"password": "secret123"}, headers={"X-Forwarded-Proto": "https"})
    assert "secure" in r.headers["set-cookie"].lower()


def test_legacy_password_hash_is_upgraded_on_login(client, env):
    from pitv.web import auth
    old = auth.hash_password("secret123", iterations=auth._LEGACY_ITERATIONS)
    legacy = "pbkdf2$" + old.split("$", 2)[2]          # the pre-upgrade 3-part format
    assert auth.verify_password("secret123", legacy) and auth.needs_rehash(legacy)
    assert not auth.verify_password("wrong", legacy) and not auth.verify_password("x", "garbage")
    conn = dbm.connect(env.db_path)
    with dbm.tx(conn):
        dbm.set_setting(conn, "admin_password_hash", legacy)
    try:
        assert client.post("/api/auth/login", json={"password": "secret123"}).status_code == 200
        stored = dbm.get_setting(conn, "admin_password_hash")
        assert stored.startswith(f"pbkdf2_sha256${auth.PBKDF2_ITERATIONS}$") and not auth.needs_rehash(stored)
    finally:
        conn.close()


def test_public_player_state_hides_machine_details(client):
    from pitv.web.api.deps import player_public
    state = {"online": True, "channel": {"number": 1}, "file": "/mnt/tvshows/x.mkv", "error": "file not available: /mnt/x",
             "cache": {"enabled": True, "dir": "/mnt/cache/pitv", "files": 3}, "input_devices": ["OSMC RF Remote"],
             "stream": {"vcodec": "h264"}, "volume": 80}
    assert player_public(state, True) is state
    pub = player_public(state, False)
    assert "file" not in pub and "input_devices" not in pub and "stream" not in pub
    assert "dir" not in pub["cache"] and "/mnt" not in pub["error"] and pub["volume"] == 80
    # Anonymous callers of the public endpoints get the filtered view.
    client.app.state.player_state = state
    try:
        anon = TestClient(client.app)
        assert "file" not in anon.get("/api/now").json()["player"]
        assert "file" in client.get("/api/now").json()["player"]   # logged in
    finally:
        client.app.state.player_state = {"online": False}


def test_remote_endpoint_only_takes_remote_keys(client):
    anon = TestClient(client.app)
    assert anon.post("/api/player/key", json={"key": "quit"}).status_code == 400
    assert anon.post("/api/player/key", json={"key": "guide"}).json()["offline"] is True
    assert anon.post("/api/player/channel", json={"number": 0}).status_code == 400
    assert anon.get("/api/sources").status_code == 401
    assert anon.get("/api/logs").status_code == 401
    assert anon.get("/api/browse", params={"path": "/"}).status_code == 401


def test_control_socket_commands_are_validated():
    from pitv.player.controller import validate_control
    assert validate_control({"cmd": "key", "key": "GUIDE"}) == ("key", "guide")
    assert validate_control({"cmd": "channel", "number": "3"}) == ("channel", 3)
    assert validate_control({"cmd": "volume", "volume": 250}) == ("volume", 100)
    assert validate_control({"cmd": "state"}) == ("state", None)
    for bad in ({"cmd": "key", "key": "rm -rf"}, {"cmd": "channel", "number": "x"}, {"cmd": "channel", "number": 0},
                {"cmd": "exec"}, {}):
        assert isinstance(validate_control(bad), str), bad


def test_browse_is_confined_to_allowed_roots(client, tmp_path):
    (tmp_path / "cache" / "sub").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (tmp_path / "cache" / "link").symlink_to(tmp_path / "outside")
    client.put("/api/settings", json={"cache_dir": str(tmp_path / "cache")})
    try:
        root = client.get("/api/browse", params={"path": "/"}).json()
        assert root["parent"] is None and str(tmp_path / "cache")[1:] in root["dirs"]
        r = client.get("/api/browse", params={"path": str(tmp_path / "cache")}).json()
        assert r["dirs"] == ["link", "sub"] and r["parent"] == "/"
        assert client.get("/api/browse", params={"path": "/etc"}).status_code == 403
        assert client.get("/api/browse", params={"path": f"{tmp_path}/cache/../outside"}).status_code == 403
        assert client.get("/api/browse", params={"path": f"{tmp_path}/cache/link"}).status_code == 403
        assert client.get("/api/browse", params={"path": "relative"}).status_code == 400
        # sources use the same rule
        assert client.post("/api/sources", json={"type": "tv", "path": "/etc"}).status_code == 403
        src = client.post("/api/sources", json={"type": "tv", "path": f"{tmp_path}/cache/sub"}).json()
        assert src["path"] == str(tmp_path / "cache" / "sub")
        assert client.delete(f"/api/sources/{src['id']}").json()["ok"]
    finally:
        client.put("/api/settings", json={"cache_dir": ""})


def test_settings_are_validated(client):
    bad = [{"cache_dir": "relative/path"}, {"cache_dir": "/mnt/../etc"}, {"content_tool_url": "http://example.com:8081"},
           {"content_tool_url": "ftp://127.0.0.1"}, {"content_tool_url": "http://127.0.0.1/api"},
           {"audio_device": "alsa/hw:0; rm -rf /"}, {"drm_connector": "HDMI-A-1 --vo=x"}, {"timezone": "Mars/Olympus"},
           {"horizon_days": "7"}, {"horizon_days": 0}, {"keymap": ["KEY_UP"]}, {"keymap": {"exec": ["KEY_UP"]}},
           {"day_start": "8am"}, {"readiness_hours": [25]}, {"channel_switch_static": 1}, {"movie_repeat_days": -1},
           {"browse_roots": ["relative"]}]
    for body in bad:
        assert client.put("/api/settings", json=body).status_code == 400, body
    good = {"content_tool_url": "http://pitv.local:8081", "audio_device": "alsa/hdmi:CARD=vc4hdmi0,DEV=0",
            "drm_connector": "HDMI-A-1", "timezone": "Europe/Dublin", "horizon_days": 7, "keymap": {"guide": ["KEY_G"]},
            "day_start": "08:00", "browse_roots": ["/mnt", "/media"]}
    r = client.put("/api/settings", json=good)
    assert r.status_code == 200, r.text
    client.put("/api/settings", json={"timezone": "Europe/London", "keymap": {}, "browse_roots": ["/mnt", "/media", "/srv"]})


def test_proxy_rejects_path_escapes(client):
    from pitv.web.api.content import _proxy_path
    # httpx normalises dot segments before sending, so the raw forms are checked on the function.
    for path in ("settings/../admin", "settings//x", "settings/./x", "admin", "", "x" * 600):
        assert _proxy_path(path) is None, path
    assert _proxy_path("settings/a b/c?d") == "settings/a%20b/c%3Fd"
    client.put("/api/settings", json={"content_tool_url": "http://127.0.0.1:9"})
    for path in ("settings/%2e%2e/admin", "settings//x", "admin", "..%2fsettings"):
        assert client.get(f"/api/content/tool/api/{path}").status_code == 404, path
    assert client.get("/api/content/tool/api/settings/providers").status_code == 503   # allowed, tool offline


def test_service_actions_match_sudoers(client):
    assert client.post("/api/system/service/pitv-web/stop").status_code == 400
    assert client.post("/api/system/service/sshd/restart").status_code == 400


def test_spa_never_serves_outside_the_bundle(client):
    from pitv.web.app import STATIC_DIR
    if not (STATIC_DIR / "index.html").exists():
        pytest.skip("web bundle not built")
    r = client.get("/%2e%2e/%2e%2e/%2e%2e/etc/passwd")
    assert r.status_code == 200 and b"root:" not in r.content and b"<!doctype html>" in r.content.lower()


def test_unhandled_errors_do_not_leak_details(client):
    import asyncio
    from fastapi import Request
    handler = client.app.exception_handlers[Exception]
    request = Request({"type": "http", "method": "GET", "path": "/api/x", "headers": [], "query_string": b""})
    r = asyncio.run(handler(request, RuntimeError("/mnt/tvshows/secret.mkv")))
    assert r.status_code == 500 and b"secret" not in r.body
