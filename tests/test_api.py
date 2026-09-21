import os
from datetime import timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from conftest import make_library
from fastapi.testclient import TestClient

from pitv import db as dbm
from pitv.db import DEFAULT_SETTINGS, now_ts
from pitv.scheduler.horizon import build_horizon
from pitv.scheduler.rules import broadcast_day_for

# The schedule editor refuses slots that have already started on the real clock, so the
# fixture builds today and tomorrow rather than fixed dates.
DAY0 = broadcast_day_for(now_ts(), DEFAULT_SETTINGS, ZoneInfo(DEFAULT_SETTINGS["timezone"]))
DAY1 = DAY0 + timedelta(days=1)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    ctx = make_library(tmp_path_factory.mktemp("pitv"), max_episodes=6)
    build_horizon(ctx["conn"], start_day=DAY0, days=2, seed=7)
    ctx["conn"].close()
    return ctx["cfg"]


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


def _wait_for_jobs(client, timeout=60.0):
    """Block until no job is running or queued; background jobs run in a thread."""
    import time as _time
    end = _time.monotonic() + timeout
    while _time.monotonic() < end:
        rows = client.get("/api/jobs").json()
        rows = rows if isinstance(rows, list) else rows.get("jobs", [])
        if not [j for j in rows if j.get("status") in ("running", "queued")]:
            return rows
        _time.sleep(0.2)
    raise AssertionError("jobs did not settle")


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
    assert upd["certificate"] == upd["indexed"]["certificate"]
    att = client.get("/api/library/attention").json()
    assert any("Mystery" in a["title"] for a in att)
    summary = client.get("/api/library/summary").json()
    assert summary["kinds"]["movie"] == 32


def test_channels_and_settings(client):
    chans = client.get("/api/channels").json()
    assert [c["number"] for c in chans] == [1, 2, 3, 4, 5, 6]
    assert [c["content"] for c in chans][-2:] == ["music", "cartoons"]
    assert next(c for c in chans if c["content"] == "music")["has_lineup"] is False
    assert next(c for c in chans if c["content"] == "cartoons")["has_lineup"] is True
    c5 = client.post("/api/channels", json={"name": "PiTV Seven", "short_name": "Seven",
                                            "content": "documentaries", "pattern": "show, break"}).json()
    assert c5["number"] == 7 and c5["pattern"] == "show, break" and c5["has_lineup"] is True
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
    # A replacement longer than the slot it replaces consumes what it overlaps, so pick the
    # slot to delete from the day as it is now.
    now_day = client.get(f"/api/schedule/day/{day['day']}").json()
    later = [s for s in now_day["slots"] if s["channel_id"] == day["channels"][0]["id"] and s["kind"] == "programme"
             and not s["locked"] and not s["replay"]]
    r = client.delete(f"/api/schedule/slots/{later[-2]['id']}")
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


def test_content_manifest_and_report(client, tmp_path):
    """Schema 2: every scheduled file is one request; a delivery report records where it landed."""
    m = client.get("/api/content/manifest", params={"days": 1}).json()
    assert m["schema"] == 2 and m["items"]
    first = m["items"][0]
    for key in ("request_id", "media_id", "kind", "title", "duration", "action", "source", "target",
                "first_air_ts", "deadline_ts", "priority", "channels", "already_cached"):
        assert key in first, key
    assert first["request_id"] == f"m:{first['media_id']}" and first["source"]["path"]
    assert m["items"] == sorted(m["items"], key=lambda i: (i["priority"], i["deadline_ts"]))
    assert all(i["action"] == "copy" for i in m["items"])   # the fake library is H.264 at 240 lines
    w = client.post("/api/wanted", json={"kind": "music", "title": "Queen - Radio Ga Ga", "year": 1984, "genre": "pop"}).json()
    assert w["genre"] == "pop"
    m = client.get("/api/content/manifest").json()
    mine = [x for x in m["wanted"] if x["wanted_id"] == w["id"]]
    assert mine and mine[0]["action"] == "fetch" and mine[0]["dest_dir"].endswith("music videos/Pop")
    assert mine[0]["search"]["phrase"].startswith("Queen - Radio Ga Ga") and mine[0]["search"]["year_tolerance"] == 0
    copy = tmp_path / "copy.mp4"
    copy.write_bytes(b"x" * 10)
    fetched = tmp_path / "Queen - Radio Ga Ga (1984).mp4"
    fetched.write_bytes(b"x" * 10)
    r = client.post("/api/content/report", json={"schema": 2, "items": [
        {"request_id": first["request_id"], "media_id": first["media_id"], "status": "done",
         "file": {"path": str(copy), "duration": first["duration"], "vcodec": "h264", "size": 10}},
        {"request_id": f"w:{w['id']}", "wanted_id": w["id"], "status": "done",
         "file": {"path": str(fetched), "duration": 245.0, "vcodec": "h264", "size": 10},
         "meta": {"kind": "music", "title": "Queen - Radio Ga Ga", "artist": "Queen", "year": 1984, "genres": ["Pop"]}},
        {"request_id": "m:999999", "media_id": 999999, "status": "done", "file": {"path": "/nonexistent.mp4"}},
    ], "run": {"tool": "pitv_content test", "started_ts": 1, "finished_ts": 2}}).json()
    assert r["ok"] and r["items_done"] == 1 and r["wanted_done"] == 1 and r["items_failed"] == 1
    assert next(x for x in client.get("/api/wanted").json() if x["id"] == w["id"])["status"] == "done"
    item = client.get(f"/api/media/{first['media_id']}").json()
    assert item["cache_path"] == str(copy) and item["cached"] is True
    music = client.get("/api/media", params={"kind": "music", "q": "Radio Ga Ga"}).json()["items"]
    assert any(i["origin"] == "online" and i["cache_path"] == str(fetched) for i in music)
    client.delete(f"/api/wanted/{w['id']}")


def test_content_manifest_ignores_part_files(client, tmp_path):
    """A half-written `<id>_*.part` from pitv_content must not count as cached (PLAN.md §7)."""
    cache = tmp_path / "cache"
    cache.mkdir()
    client.put("/api/settings", json={"cache_dir": str(cache)})
    try:
        # An item nothing has delivered yet (the report test records a copy for the first one).
        item = next(i for i in client.get("/api/content/manifest").json()["items"] if not i["already_cached"])
        target = cache / os.path.basename(item["target"])
        assert item["already_cached"] is False and target.parent == cache
        target.with_name(target.name + ".part").write_bytes(b"x")
        item = next(i for i in client.get("/api/content/manifest").json()["items"] if i["media_id"] == item["media_id"])
        assert item["already_cached"] is False
        target.write_bytes(b"x")
        item = next(i for i in client.get("/api/content/manifest").json()["items"] if i["media_id"] == item["media_id"])
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
    # Anonymous callers of the public endpoints get the filtered view. The state carries the
    # same detail but does not claim to be online, because the web service's player subscriber
    # resets any state that does, every three seconds and whenever the socket drops: setting one
    # immediately before each request only narrows the window, and this test failed on a machine
    # busy encoding video. What is under test here is that /api/now applies the filter above by
    # whether the caller is logged in, which has nothing to do with the player answering.
    # Before any password exists every caller is an admin, so there is nothing to filter and
    # this proves nothing. The test set one earlier in the file, which made it pass here and
    # fail on its own; it now sees to that itself.
    if not client.get("/api/auth").json()["password_set"]:
        assert client.post("/api/auth/setup", json={"password": "secret123"}).status_code == 200
    anon = TestClient(client.app)
    try:
        client.app.state.player_state = {**state, "online": False}
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


def test_pitv_content_uses_its_token_not_a_session(client, env):
    """With a password set pitv_content has no session; its token opens only its own endpoints."""
    import os
    import stat
    assert client.get("/api/auth").json()["password_set"]
    anon = TestClient(client.app)
    token = client.get("/api/content/token").json()["token"]
    path = env.data_dir / "content-token"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and path.read_text().strip() == token
    bearer = {"Authorization": f"Bearer {token}"}
    assert anon.get("/api/content/manifest").status_code == 401
    assert anon.get("/api/content/manifest", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert anon.get("/api/content/manifest", headers=bearer).status_code == 200
    assert anon.post("/api/content/make-room", json={"bytes": 0}, headers=bearer).status_code == 200
    assert anon.get("/api/content/tool", headers=bearer).status_code == 401     # the admin's, not pitv_content's
    assert anon.get("/api/settings", headers=bearer).status_code == 401
    rotated = client.post("/api/content/token").json()["token"]
    assert rotated != token and anon.get("/api/content/manifest", headers=bearer).status_code == 401
    assert anon.get("/api/content/manifest", headers={"Authorization": f"Bearer {rotated}"}).status_code == 200


def test_setting_ranges():
    from pitv.web.api.settings_rules import SettingError, check_setting
    assert check_setting("osd_scale", 1.25) == 1.25
    for key, value in (("osd_scale", 9), ("osd_safe_margin", 0.5), ("memory_limit_mb", 10), ("horizon_days", 0)):
        with pytest.raises(SettingError):
            check_setting(key, value)


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
        # pitv_content source roots are held to the same rule before they are forwarded
        assert client.put("/api/sources", json={"id": "x", "type": "tv", "root": "/etc"}).status_code == 403
    finally:
        client.put("/api/settings", json={"cache_dir": ""})


def test_sources_are_pitv_contents(client):
    """With pitv_content down the Sources view falls back to the last index; edits are refused."""
    client.put("/api/settings", json={"content_tool_url": "http://127.0.0.1:9"})
    try:
        r = client.get("/api/sources").json()
        assert r["owner"] == "pitv_content" and r["offline"] is True
        ids = {s["id"] for s in r["sources"]}
        assert {"tvshows", "movies", "ads", "tvsports", "musicvideos"} <= ids
        assert all(s["health"]["items"] > 0 for s in r["sources"] if s["id"] in ("tvshows", "movies"))
        assert client.put("/api/sources", json={"id": "tvshows", "enabled": False}).status_code == 503
        assert client.put("/api/sources", json={"type": "tv"}).status_code == 400
    finally:
        client.put("/api/settings", json={"content_tool_url": "http://127.0.0.1:8081"})


def test_catalogue_import_endpoint(client, env):
    status = client.get("/api/catalogue").json()
    assert status["last_import"]["kind"] == "catalogue"
    assert client.post("/api/catalogue/import", json={"schema": 1}).status_code == 400
    assert "shows" in client.get("/api/catalogue/export").json()


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


def test_service_actions_match_sudoers(monkeypatch):
    """The admin offers exactly the systemctl commands the installer lets the pitv user run."""
    from pathlib import Path

    from fastapi import HTTPException

    from pitv.web.api.admin import service_action
    from pitv.web.api.services import SERVICE_ACTIONS
    install = (Path(__file__).parents[1] / "setup" / "install.sh").read_text()
    rule = next(line for line in install.splitlines() if line.startswith("pitv ALL=(root) NOPASSWD:"))
    granted = {tuple(cmd.split()[1:]) for cmd in rule.split("NOPASSWD:", 1)[1].split(",")}
    assert granted == {(action, unit) for unit, actions in SERVICE_ACTIONS.items() for action in actions}
    monkeypatch.setattr("pitv.web.api.admin.systemd_state", lambda *args, **kwargs: {})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(cfg=None)))
    with pytest.raises(HTTPException) as missing:
        service_action("pitv-web.service", "stop", request)
    assert missing.value.status_code == 409
    with pytest.raises(HTTPException) as unsupported:
        service_action("sshd.service", "restart", request)
    assert unsupported.value.status_code == 400


def test_services_cover_both_apps():
    from pitv.web.api.services import UNITS, Unit, assess
    assert {u.app for u in UNITS} == {"pitv", "content"}
    daemon, run = Unit("a.service", "pitv", "", "daemon"), Unit("b.service", "content", "", "run")
    loaded = {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "Result": "success"}
    assert assess(run, loaded, None) == ("idle", "idle, last run ok")         # a oneshot at rest is not a fault
    assert assess(daemon, loaded, None)[0] == "down"
    assert assess(daemon, {**loaded, "ActiveState": "active", "SubState": "running"}, False)[0] == "warn"
    assert assess(daemon, {"LoadState": "not-found"}, True) == ("ok", "running outside systemd")
    assert assess(daemon, {"LoadState": "not-found"}, None) == ("absent", "not installed")
    # On a desktop the run goes through the API and there is no boot splash: neither is a fault.
    assert assess(run, {"LoadState": "not-found"}, False)[0] == "idle"
    assert assess(Unit("d.timer", "content", "", "timer"), {"LoadState": "not-found"}, True)[0] == "idle"
    assert assess(Unit("c.service", "pitv", "", "boot"), {"LoadState": "not-found"}, None, on_pi=False) == ("idle", "Pi only")


def test_system_reports_host_in_the_contract_shape(client):
    host = client.get("/api/system").json()["host"]
    assert {"version", "python", "tools", "hostname", "model", "pi", "uptime_s", "load", "temperature_c",
            "memory"} <= set(host)
    assert "mpv" in host["tools"]
    from pitv.web.api.content import _proxy_path
    assert _proxy_path("system") == "system"      # pitv_content's host document is reachable through the proxy
    assert _proxy_path("lookup") == "lookup"      # and so is its online lookup for the add dialog


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


def test_lineup_api(client):
    facets = client.get("/api/library/facets").json()
    assert facets["genres"]["Comedy"]["episode"] > 0
    assert all(int(d) % 10 == 0 for d in facets["decades"])
    chans = client.get("/api/channels").json()
    general = [c for c in chans if c["content"] == "general"]
    assert general[0]["allowed_genres"]
    entries = client.get("/api/lineup", params={"channel_id": general[0]["id"]}).json()
    assert entries and all(e["channel_id"] == general[0]["id"] for e in entries)
    opts = client.get("/api/lineup/options", params={"q": "Minder"}).json()
    assert opts and opts[0]["type"] == "show" and opts[0]["channel_id"]
    target = general[1]["id"]
    moved = client.post("/api/lineup", json={"channel_id": target, "show_id": opts[0]["show_id"]}).json()
    assert moved["channel_id"] == target and moved["pinned"] == 1
    ext = client.post("/api/lineup", json={"channel_id": target, "title": "The Tripods", "year": 1984, "kind": "show",
                                           "episode_minutes": 25}).json()
    assert ext["external"] is True and ext["transient"] == 1
    upd = client.put(f"/api/lineup/{ext['id']}", json={"remove_after_airing": True, "enabled": False}).json()
    assert upd["remove_after_airing"] == 1 and upd["enabled"] == 0
    doc = client.get("/api/lineup/export").json()
    assert any(any(e["title"] == "The Tripods" for e in c["lineup"]) for c in doc["channels"])
    assert client.post("/api/lineup/import", json=doc).json()["entries"] > 0
    assert client.delete(f"/api/lineup/{ext['id']}").json()["ok"]
    r = client.put(f"/api/channels/{target}", json={"nas_only": "no", "allowed_genres": ["Comedy", "Drama"]}).json()
    assert r["nas_only"] == "no" and r["allowed_genres"] == ["Comedy", "Drama"]
    assert client.put(f"/api/channels/{target}", json={"nas_only": "maybe"}).status_code == 400
    client.put(f"/api/channels/{target}", json={"nas_only": "inherit"})
    gen = client.post("/api/lineup/generate", json={"rebalance": True}).json()
    assert gen["assigned"] > 0


# --- request hardening ----------------------------------------------------------------------------

JSON = {"Content-Type": "application/json"}


def test_cross_site_writes_are_refused(client):
    """A page on another site (or another port of this host) cannot drive the API through the
    admin's browser; the UI itself, scripts and pitv_content send no such headers or match."""
    body = {"key": "guide"}
    for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                    {"Origin": "http://evil.example"}, {"Origin": "null"}):
        assert client.post("/api/player/key", json=body, headers=headers).status_code == 403, headers
    for headers in ({"Sec-Fetch-Site": "same-origin"}, {"Origin": "http://testserver"}, {}):
        assert client.post("/api/player/key", json=body, headers=headers).status_code == 200, headers
    r = client.get("/api/now", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"


def test_request_bodies_are_capped(client):
    from pitv.web.app import MAX_BODY
    r = client.post("/api/player/key", content=b" " * (MAX_BODY + 1), headers=JSON)
    assert r.status_code == 413

    def chunked():   # no Content-Length, so the cap has to count what arrives
        for _ in range(MAX_BODY // 65536 + 2):
            yield b" " * 65536
    assert client.post("/api/player/key", content=chunked(), headers=JSON).status_code == 413


def test_document_uploads_are_read_only_after_the_admin_check(client):
    anon = TestClient(client.app)
    for path in ("/api/catalogue/import", "/api/lineup/import", "/api/content/report"):
        assert anon.post(path, content=b"{not json", headers=JSON).status_code == 401, path
        assert client.post(path, content=b"{not json", headers=JSON).status_code == 400, path
    assert client.post("/api/lineup/import", content=b"{}", headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.post("/api/lineup/import", json=[1]).status_code == 400


def test_proxy_requires_json_bodies(client):
    client.put("/api/settings", json={"content_tool_url": "http://127.0.0.1:9"})
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    assert client.post("/api/content/tool/api/settings", content=b"a=1", headers=form).status_code == 415
    assert client.post("/api/content/tool/api/settings", content=b"{bad", headers=JSON).status_code == 400
    assert client.post("/api/content/tool/api/run", json={}).status_code == 503   # well formed; tool offline


def test_password_change_revokes_other_sessions(client):
    other = TestClient(client.app)
    assert other.post("/api/auth/login", json={"password": "secret123"}).status_code == 200
    assert other.get("/api/jobs").status_code == 200
    r = client.post("/api/auth/password", json={"current": "secret123", "password": "secret456"})
    assert r.status_code == 200 and "pitv_session=" in r.headers["set-cookie"]
    assert other.get("/api/jobs").status_code == 401
    assert client.get("/api/jobs").status_code == 200   # the caller carries on with its new cookie
    assert client.post("/api/auth/password", json={"current": "secret456", "password": "secret123"}).status_code == 200


def test_login_attempts_are_counted_before_verification():
    from pitv.web import auth
    ip = "203.0.113.7"
    try:
        assert all(auth.login_allowed(ip) for _ in range(auth.LOGIN_ATTEMPTS))
        assert not auth.login_allowed(ip)
        auth.forget_attempts(ip)
        assert auth.login_allowed(ip)
    finally:
        auth.forget_attempts(ip)


def test_settings_refuse_secrets_and_malformed_urls(client):
    from pitv.web.api.settings_rules import SettingError, check_setting
    for key in ("admin_password_hash", "session_secret"):
        with pytest.raises(SettingError):
            check_setting(key, "x")
        assert client.put("/api/settings", json={key: "x"}).status_code == 400
    for url in ("http://127.0.0.1:99999", "http://127.0.0.1:abc", "http://0.0.0.0:8081", "http://[::ffff:8.8.8.8]:8081"):
        assert client.put("/api/settings", json={"content_tool_url": url}).status_code == 400, url
    assert check_setting("content_tool_url", "http://100.101.102.103:8081")   # a Tailscale peer
    assert client.post("/api/settings/reset", json={"keys": "timezone"}).status_code == 400


def test_bad_references_are_client_errors(client):
    assert client.post("/api/wanted", json={"kind": "episode", "title": "X", "show_id": 999999}).status_code == 409
    assert client.post("/api/wanted", json={"kind": "movie", "title": "X", "year": "soon"}).status_code == 400
    assert client.post("/api/wanted", json={"kind": "movie", "title": ["X"]}).status_code == 400
    r = client.post("/api/schedule/insert", json={"channel_id": 999999, "start_ts": now_ts() + 3600, "media_id": 1})
    assert r.status_code == 404
    sid = client.get("/api/shows").json()[0]["id"]
    for bad in ({"year": "soon"}, {"genres": "Comedy"}, {"home_channel_id": "one"}, {"anchor_days": 3}):
        assert client.put(f"/api/shows/{sid}", json=bad).status_code == 400, bad


def test_event_streams_are_capped():
    from pitv.web.events import MAX_SUBSCRIBERS, EventBus
    bus = EventBus()
    queues = [bus.subscribe() for _ in range(MAX_SUBSCRIBERS)]
    assert all(q is not None for q in queues) and bus.is_full() and bus.subscribe() is None
    bus.unsubscribe(queues[0])
    assert not bus.is_full() and bus.subscribe() is not None


def test_job_runner_bounds_history_and_notes():
    import time

    from pitv.web.events import EventBus
    from pitv.web.tasks import MAX_JOBS_KEPT, MAX_NOTES, JobRunner
    runner = JobRunner(EventBus())

    def noisy(job):
        job.notes.extend(str(i) for i in range(MAX_NOTES * 3))

    def wait():
        deadline = time.monotonic() + 10
        while any(j["status"] in ("queued", "running") for j in runner.recent(10 ** 6)):
            assert time.monotonic() < deadline
            time.sleep(0.01)

    for _ in range(MAX_JOBS_KEPT + 10):
        runner.submit("test", "noisy", noisy)
    wait()
    runner.submit("test", "noisy", noisy)   # pruning happens on submit
    wait()
    jobs = runner.recent(10 ** 6)
    assert len(jobs) <= MAX_JOBS_KEPT + 1
    assert all(len(runner._jobs[j["id"]].notes) <= MAX_NOTES for j in jobs)


def test_settings_schema_matches_the_defaults(client):
    """Every setting a person edits has exactly one field, each field names a real setting, and
    every default sits inside its field's range and choices."""
    from pitv import settings_schema
    from pitv.db import DEFAULT_SETTINGS
    keys = [f["key"] for f in settings_schema.FIELDS]
    assert len(keys) == len(set(keys))
    assert set(keys) - settings_schema.COMPUTED | settings_schema.UNLISTED == set(DEFAULT_SETTINGS)
    panes = {pid for pid, _, _ in settings_schema.PANES}
    for f in settings_schema.FIELDS:
        assert f["pane"] in panes and f["level"] in settings_schema.LEVELS, f["key"]
        if f["key"] in settings_schema.COMPUTED:
            continue
        default = DEFAULT_SETTINGS[f["key"]]
        if "min" in f:
            assert f["min"] <= default <= f["max"], f["key"]
        if "choices" in f:
            assert default in settings_schema.choice_values(f["key"]), f["key"]
    doc = client.get("/api/settings/schema").json()
    assert [p["id"] for p in doc["panes"]] == [pid for pid, _, _ in settings_schema.PANES]
    by_key = {f["key"]: f for f in doc["fields"]}
    assert by_key["day_start"]["value"] == "08:00" and by_key["osd_scale"]["max"] == 2.5
    assert "admin_password_hash" not in by_key


def _content_stub(refuse_values: bool):
    """A stand-in for pitv_content's PUT /api/settings that records each body it is sent. An
    older one refuses the screen's values as an unknown setting."""
    import http.server
    import json
    import threading
    seen: list[dict] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_PUT(self):
            sent = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(sent)
            refused = refuse_values and "video_profile_values" in sent
            body = json.dumps({"errors": {"video_profile_values": "unknown setting"}} if refused else {"ok": True}).encode()
            self.send_response(400 if refused else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_the_screens_values_go_to_pitv_content_with_its_name(client):
    """pitv_content keeps no table of screens: what a catalogue run encodes to and the best source
    it starts from are the values in `pitv/display.py`, the same object the manifest carries."""
    srv, seen = _content_stub(refuse_values=False)
    try:
        client.put("/api/settings", json={"content_tool_url": f"http://127.0.0.1:{srv.server_port}"})
        client.put("/api/settings", json={"display_profile": "lcd_1080p"})
    finally:
        srv.shutdown()
        srv.server_close()
    assert len(seen) == 1 and seen[0]["profile"] == "lcd_1080p"
    assert seen[0]["video_profile_values"] == client.get("/api/content/manifest").json()["profile"]
    assert seen[0]["video_profile_values"]["max_source_height"] == 2160


def test_a_pitv_content_from_before_the_values_still_gets_the_name(client):
    srv, seen = _content_stub(refuse_values=True)
    try:
        client.put("/api/settings", json={"content_tool_url": f"http://127.0.0.1:{srv.server_port}"})
        client.put("/api/settings", json={"display_profile": "lcd_1080p"})
    finally:
        srv.shutdown()
        srv.server_close()
    assert [sorted(s) for s in seen] == [["profile", "video_profile_values"], ["profile"]]
    assert seen[1] == {"profile": "lcd_1080p"}


def test_screen_profile_sets_quality_and_screen(client):
    """Choosing the screen sets what pitv_content encodes to and the best source it fetches (720p
    for a standard definition screen, two rungs higher for HD, equal at 4K), and brings the
    screen's shape and margins with it."""
    from pitv import display
    ceilings = {p.height: p.max_source_height for p in display.PROFILES}
    assert ceilings == {576: 720, 480: 720, 720: 1440, 1080: 2160, 2160: 2160}
    assert display.BY_ID["lcd_2160p"].vcodec == "hevc" and display.BY_ID["lcd_1080p"].vcodec == "h264"
    r = client.put("/api/settings", json={"display_profile": "lcd_1080p"}).json()
    assert r["display_aspect"] == "16:9" and r["osd_safe_margin"] == 0.03
    assert r["content_profile"]["height"] == 1080 and r["content_profile"]["max_source_height"] == 2160
    assert client.get("/api/content/manifest").json()["profile"]["name"] == "lcd_1080p"
    r = client.put("/api/settings", json={"display_profile": "crt_pal", "osd_scale": 1.5}).json()
    assert r["display_aspect"] == "4:3" and r["osd_safe_margin"] == 0.07 and r["osd_scale"] == 1.5
    assert client.put("/api/settings", json={"display_profile": "plasma"}).status_code == 400


def test_source_login_is_relayed_not_kept(client, env):
    """PiTV relays a share's login to pitv_content and keeps none of it: with pitv_content
    unreachable both the save and the connection test answer 503, and the password appears in
    neither PiTV's database nor its settings."""
    client.put("/api/settings", json={"content_tool_url": "http://127.0.0.1:9"})
    body = {"id": "tvshows", "root": "/mnt/tvshows", "remote": "smb://nas/tvshows", "username": "pete", "password": "hunter2-secret"}
    assert client.put("/api/sources", json=body).status_code == 503
    assert client.post("/api/sources/test", json=body).status_code == 503
    import sqlite3
    dump = "\n".join(sqlite3.connect(env.db_path).iterdump())
    assert "hunter2-secret" not in dump


def test_doctor_reports_the_whole_television_and_survives_a_broken_section(client, monkeypatch):
    """One read-only report: findings first, then a section each for services, player, schedule,
    cache, bands, library, pitv_content, logs and disks. A section that cannot be gathered says so
    and the rest still are, since the report matters most when something is broken."""
    from pitv import doctor
    doc = client.get("/api/doctor").json()
    assert next(iter(doc)) == "findings" and isinstance(doc["findings"], list)
    for section in ("host", "services", "player", "schedule", "cache", "bands", "library", "wanted", "runs",
                    "content", "logs", "disks"):
        assert section in doc, section
    assert doc["schedule"]["channel_days"] and doc["cache"]["next_day_files"] > 0
    assert doc["content"]["reachable"] is False and any("pitv_content is not reachable" in f for f in doc["findings"])
    assert "Findings" in doctor.render(doc)

    def broken(*_args, **_kwargs):
        raise RuntimeError("no such table")
    monkeypatch.setattr(doctor, "_library", broken)
    doc = client.get("/api/doctor").json()
    assert doc["library"] == {"error": "RuntimeError: no such table"} and doc["schedule"]["channel_days"]
    assert any("library section could not be gathered" in f for f in doc["findings"])


def test_the_add_dialog_is_told_what_the_catalogue_already_holds(client):
    """Search results for a title already in the catalogue are greyed out, so the same programme
    is not added twice: line-up entries carry the identity they were confirmed against, and the
    wanted list and library answer for adverts and music videos."""
    shows = client.get("/api/lineup/known", params={"kind": "show"}).json()
    assert shows and all({"title", "year", "source", "match"} <= set(r) for r in shows)
    channel = client.get("/api/channels").json()[0]["id"]
    added = client.post("/api/lineup", json={"kind": "show", "title": "The Tripods", "year": 1984, "channel_id": channel,
                                            "match": {"source": "tvmaze", "id": 2203}}).json()
    mine = [r for r in client.get("/api/lineup/known", params={"kind": "show"}).json() if r["title"] == "The Tripods"]
    assert mine and mine[0]["match"] == {"source": "tvmaze", "id": "2203"} and mine[0]["channel_number"]
    client.delete(f"/api/lineup/{added['id']}")
    assert client.get("/api/lineup/known", params={"kind": "music"}).json()
    assert client.get("/api/lineup/known", params={"kind": "series"}).status_code == 400


def test_a_channel_is_pointed_at_its_idents(client):
    """The channel editor lists every ident with whose it is, and saving `ident_ids` is the whole
    answer: those listed become the channel's own, any it had that are not listed go generic."""
    channels = client.get("/api/channels").json()
    first, second = channels[0], channels[1]
    idents = first["idents"]
    assert len(idents) >= 2 and {"id", "title", "seconds", "channel_id", "channel_name"} <= set(idents[0])
    a, b = idents[0]["id"], idents[1]["id"]
    got = client.put(f"/api/channels/{first['id']}", json={"ident_ids": [a, b]}).json()
    assert {i["id"] for i in got["idents"] if i["channel_id"] == first["id"]} == {a, b}
    got = client.put(f"/api/channels/{first['id']}", json={"ident_ids": [a]}).json()
    mine = {i["id"]: i for i in got["idents"]}
    assert mine[a]["channel_id"] == first["id"] and mine[b]["channel_id"] is None, "the one left out went generic"
    client.put(f"/api/channels/{second['id']}", json={"ident_ids": [a]})           # pointing another channel at it moves it
    assert {i["id"]: i for i in client.get("/api/channels").json()[0]["idents"]}[a]["channel_name"] == second["name"]
    assert client.put(f"/api/channels/{first['id']}", json={"ident_ids": ["x"]}).status_code == 400
    assert client.put(f"/api/channels/{first['id']}", json={"name": first["name"]}).status_code == 200, "a save that says nothing about idents leaves them"
    assert {i["id"]: i for i in client.get("/api/channels").json()[1]["idents"]}[a]["channel_id"] == second["id"]


def test_doctor_says_when_the_cache_cannot_hold_the_schedule():
    """A cap smaller than the schedule does not fail, it just copies the same files again every
    day and plays those slots from the NAS, and nothing in a delivery report shows it: a run only
    ever sees what is missing now. It took an evening by hand to find, so the doctor says it."""
    from pitv import doctor
    gb = 1024 ** 3
    doc = {"cache": {"next_day_files": 520, "cached_percent": 100, "cap_holds_days": 1.2,
                     "schedule_day_bytes": 222 * gb, "usage": {"max": 270 * gb, "free": 13 * gb}}}
    finding = next(f for f in doctor._findings(doc) if "holds only" in f)
    assert "1.2 days" in finding and "270 GiB against 222 GiB a day" in finding
    assert "play from the NAS" in finding
    assert "wants about 333 GiB" in finding and "13 GiB spare" in finding

    roomy = {"cache": {"next_day_files": 520, "cached_percent": 100, "cap_holds_days": 3.0,
                       "schedule_day_bytes": 100 * gb, "room_for_kept_bytes": 200 * gb, "kept_bytes": 25 * gb,
                       "usage": {"max": 300 * gb, "free": 500 * gb}}}
    assert not [f for f in doctor._findings(roomy) if "holds only" in f]


def test_doctor_says_when_what_is_kept_is_about_to_be_evicted():
    """Fetched episodes are kept so a later airing costs nothing, so they are the part of the
    cache meant to grow. A cap that holds the schedule but leaves less room than they already
    occupy is about to start evicting them, which no count of days would show."""
    from pitv import doctor
    gb = 1024 ** 3
    doc = {"cache": {"next_day_files": 520, "cached_percent": 100, "cap_holds_days": 1.2,
                     "schedule_day_bytes": 222 * gb, "room_for_kept_bytes": 48 * gb, "kept_bytes": 23 * gb,
                     "usage": {"max": 270 * gb, "free": 13 * gb}}}
    assert not [f for f in doctor._findings(doc) if "start being evicted" in f], "48 GiB spare holds 23 GiB kept"
    doc["cache"]["room_for_kept_bytes"] = 18 * gb
    finding = next(f for f in doctor._findings(doc) if "start being evicted" in f)
    assert "18 GiB left" in finding and "already hold 23 GiB" in finding


def test_a_fresh_schedule_keeps_what_was_fetched_unless_asked_otherwise(client, monkeypatch):
    """The button said every file was kept while the call threw away everything pitv_content had
    fetched. A fresh schedule is usually wanted so the material already gathered can be arranged
    again, and a night's fetching is expensive to replace, so clearing it is now asked for."""
    from pitv import tool_client
    called: list[str] = []

    def fake_request(base, method, path, query="", body=None, timeout=15):
        called.append(path)
        if path == "reset":
            return 200, {"ok": True}
        return 503, {"error": "offline"}

    monkeypatch.setattr(tool_client, "request", fake_request)
    assert client.post("/api/schedule/fresh-rebuild").status_code == 200
    _wait_for_jobs(client)
    assert "reset" not in called, "nothing fetched is thrown away unless it was asked for"

    called.clear()
    assert client.post("/api/schedule/fresh-rebuild", json={"clear_material": True}).status_code == 200
    _wait_for_jobs(client)
    assert "reset" in called, "and it is still available for starting the library over"


def test_doctor_reports_what_pitv_content_healed_and_what_is_still_waiting():
    """A queue that puts itself right silently is only half of what was asked for: the rule that
    held the job is still there to hold the next one, so the event belongs where a person reads
    it rather than only in the tool's own log."""
    from pitv import doctor
    doc = {"content": {"reachable": True, "errors": [],
                       "queue_warning": "index job 20260921-021947-08be has been queued 9 hours without a turn",
                       "healed": [("ran index job 20260921-021947-08be ahead of its turn after 9 hours"
                                   " queued, because it was waiting for a delivery run that never ends")]}}
    findings = doctor._findings(doc)
    assert any("healed its queue" in f and "9 hours queued" in f for f in findings)
    assert any("queue: index job" in f for f in findings)
    assert not [f for f in doctor._findings({"content": {"reachable": True, "errors": []}}) if "healed" in f]
