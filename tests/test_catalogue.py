"""The library index import and where playback finds files."""

import copy
import random

import pytest
from conftest import make_library

from pitv import db as dbm
from pitv.catalogue import IndexFormatError, import_index
from pitv.player.cache import MediaCache


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    return make_library(tmp_path_factory.mktemp("catalogue"), max_episodes=4)


def test_import_creates_catalogue_and_is_idempotent(ctx):
    conn, doc = ctx["conn"], ctx["lib"]["index"]
    ids = {r["uid"]: r["id"] for r in conn.execute("SELECT id, uid FROM media")}
    assert len(ids) == len(doc["items"])
    counts = import_index(conn, doc)
    assert counts["new"] == 0 and counts["missing"] == 0 and counts["rejected"] == 0
    assert {r["uid"]: r["id"] for r in conn.execute("SELECT id, uid FROM media")} == ids, "ids must be stable"
    sport = conn.execute("SELECT category FROM shows WHERE title = 'Pot Black'").fetchone()
    assert sport["category"] == "sport"
    toons = conn.execute("SELECT category, kids FROM shows WHERE title = 'Danger Mouse'").fetchone()
    assert toons["category"] == "general" and toons["kids"] == 1


def test_complete_index_marks_absent_items_missing(ctx):
    conn, doc = ctx["conn"], ctx["lib"]["index"]
    gone = doc["items"][0]
    partial = copy.deepcopy(doc)
    partial["items"] = doc["items"][1:]
    partial["complete"] = False
    assert import_index(conn, partial)["missing"] == 0, "an incomplete index only adds and updates"
    partial["complete"] = True
    assert import_index(conn, partial)["missing"] == 1
    assert conn.execute("SELECT missing FROM media WHERE uid = ?", (gone["uid"],)).fetchone()[0] == 1
    import_index(conn, doc)
    assert conn.execute("SELECT missing FROM media WHERE uid = ?", (gone["uid"],)).fetchone()[0] == 0


def test_advert_family_safety_precedence(ctx):
    conn, doc = ctx["conn"], ctx["lib"]["index"]
    flagged = {r["title"]: r["family_safe"] for r in conn.execute("SELECT title, family_safe FROM media WHERE kind = 'advert'")}
    assert flagged["Hofmeister"] == 0 and flagged["Milk Tray"] == 1   # PiTV's keyword rule when unspecified
    tweaked = copy.deepcopy(doc)
    for it in tweaked["items"]:
        if it["title"] == "Milk Tray":
            it["tags"] = ["alcohol"]
        if it["title"] == "Hofmeister":
            it["family_safe"] = True                                  # pitv_content's verdict wins
    import_index(conn, tweaked)
    flagged = {r["title"]: r["family_safe"] for r in conn.execute("SELECT title, family_safe FROM media WHERE kind = 'advert'")}
    assert flagged["Hofmeister"] == 1 and flagged["Milk Tray"] == 0
    import_index(conn, doc)


def test_advert_keywords_match_whole_words():
    from pitv.catalogue import family_safe, keyword_pattern
    kw = keyword_pattern(["ale", "gin", "lager", "18+"])
    assert family_safe({"title": "MFI furniture sale"}, kw) == 1
    assert family_safe({"title": "Castrol GTX (Liquid engineering)"}, kw) == 1
    assert family_safe({"title": "Hemeling Lager"}, kw) == 0
    assert family_safe({"title": "Real Ale"}, kw) == 0
    # A keyword ending in punctuation still matches before a space (\b needed a word character).
    assert family_safe({"title": "Club 18+ holidays"}, kw) == 0
    assert family_safe({"title": "Tags as text", "tags": "alcohol"}, kw) == 0
    assert family_safe({"title": "Milk Tray"}, None) == 1


def test_cache_located_items_count_as_cached(ctx, tmp_path):
    conn, doc = ctx["conn"], ctx["lib"]["index"]
    fetched = tmp_path / "acquired" / "Fetched Film (1984).mp4"
    fetched.parent.mkdir(parents=True)
    fetched.write_bytes(b"x" * 10)
    extra = copy.deepcopy(doc)
    extra["sources"].append({"id": "acquired-movies", "name": "Acquired films", "type": "movie", "category": "general",
                             "root": str(fetched.parent), "location": "cache", "enabled": True})
    extra["items"].append({"uid": "cache:acquired-movies:Fetched Film (1984).mp4", "source": "acquired-movies",
                           "kind": "movie", "title": "Fetched Film", "year": 1984, "duration": 5400.0,
                           "vcodec": "h264", "height": 576, "path": str(fetched)})
    import_index(conn, extra)
    row = conn.execute("SELECT origin, cache_path FROM media WHERE uid = 'cache:acquired-movies:Fetched Film (1984).mp4'").fetchone()
    assert row["origin"] == "cache" and row["cache_path"] == str(fetched)
    import_index(conn, doc)


def test_rejects_other_schemas(ctx):
    with pytest.raises(IndexFormatError):
        import_index(ctx["conn"], {"schema": 1, "items": []})


def test_playback_chain(ctx, tmp_path):
    """Cache copy first; the NAS original only with fallback on; otherwise nothing."""
    conn = ctx["conn"]
    m = dict(conn.execute("SELECT id, path, cache_path, origin FROM media WHERE kind = 'movie' AND missing = 0 LIMIT 1").fetchone())
    cache = MediaCache(tmp_path / "cache", 10 ** 9)
    assert cache.locate(m, nas_fallback=True) == (m["path"], "nas")
    path, why = cache.locate(m, nas_fallback=False)
    assert path is None and "fallback is off" in why
    copy_file = tmp_path / "cache" / f"{m['id']}_film.mp4"
    copy_file.write_bytes(b"x")
    cache.invalidate()
    assert cache.locate(m, nas_fallback=False) == (str(copy_file), "cache")
    copy_file.write_bytes(b"")          # an empty file is a failed write, never playable
    cache.invalidate()
    assert cache.locate(m, nas_fallback=True)[1] == "nas"
    online = {**m, "origin": "online", "path": str(tmp_path / "gone.mp4"), "cache_path": None, "id": 10 ** 7}
    path, why = cache.locate(online, nas_fallback=True)
    assert path is None and "fetched" in why


def test_old_scan_rows_are_adopted(tmp_path):
    """Rows created by the old NAS scanner keep their ids when the first index arrives."""
    ctx = make_library(tmp_path, max_episodes=2)
    conn, doc = ctx["conn"], ctx["lib"]["index"]
    first = doc["items"][0]
    old_id = conn.execute("SELECT id FROM media WHERE uid = ?", (first["uid"],)).fetchone()["id"]
    with dbm.tx(conn):
        conn.execute("UPDATE media SET uid = NULL WHERE id = ?", (old_id,))
    import_index(conn, doc)
    assert conn.execute("SELECT id FROM media WHERE uid = ?", (first["uid"],)).fetchone()["id"] == old_id


def test_cache_copy_is_decoded_by_its_own_properties():
    """An MPEG-2 interlaced original transcoded to progressive H.264 must hardware-decode."""
    from pitv.player.controller import Player
    from pitv.player.hwdec import decode_options
    media = {"vcodec": "mpeg2video", "interlaced": 1, "cache_vcodec": "h264", "cache_interlaced": 0}
    on_cache = decode_options(Player._decode_props(media, "cache"), True, {})
    assert on_cache["deinterlace"] is False and on_cache["hwdec"] != "no"
    on_nas = decode_options(Player._decode_props(media, "nas"), True, {})
    assert on_nas["deinterlace"] is True and on_nas["hwdec"] == "no"


def test_reindex_waits_for_its_job(monkeypatch):
    """The import after a re-index must read the new index, not the one being replaced."""
    from pitv import catalogue, tool_client
    calls, polls = [], iter([[{"job_id": "j1"}], [{"job_id": "j1", "finished_ts": 5, "status": "ok"}]])

    def fake(base, method, path, query="", body=None, timeout=15):
        calls.append((method, path))
        if path == "index":
            return 200, {"ok": True, "job_id": "j1"}
        if path == "jobs":
            return 200, next(polls)
        return 200, {"schema": 2, "items": []}

    monkeypatch.setattr(tool_client, "request", fake)
    monkeypatch.setattr(catalogue, "REINDEX_POLL", 0)
    doc, _ = catalogue.fetch_index({"content_tool_url": "http://x"}, reindex=True)
    assert doc == {"schema": 2, "items": []}
    assert calls == [("POST", "index"), ("GET", "jobs"), ("GET", "jobs"), ("GET", "library")]


def test_malformed_records_are_rejected_and_counted(ctx):
    """A bad record from pitv_content costs that record, never the import; a field of the wrong
    type is dropped rather than stored."""
    conn, doc = ctx["conn"], ctx["lib"]["index"]
    first, second = doc["items"][0], doc["items"][1]
    before = conn.execute("SELECT id, path FROM media WHERE uid = ?", (first["uid"],)).fetchone()
    bad = copy.deepcopy(doc)
    bad["complete"] = False
    bad["sources"] += ["junk", {"id": ["x"], "type": "tv"}, {"id": "radio", "type": "radio"}]
    bad["shows"] += [5, {"uid": "show:x", "source": ["tvshows"]}]
    bad["items"] += [
        None, {"uid": {"a": 1}}, {"uid": "nas:movies:nopath", "source": "movies", "kind": "movie", "path": ["/x"]},
        {**first, "path": second["path"]},     # its path already belongs to another uid
        {"uid": "nas:movies:odd.mp4", "source": "movies", "kind": "movie", "path": "/lib/odd.mp4",
         "title": {"x": 1}, "year": "nineteen", "duration": "nan", "height": [720], "size": 10 ** 30,
         "genres": 5, "interlaced": "false", "certificate": 12},
    ]
    counts = import_index(conn, bad)
    assert counts["rejected"] == 3 + 2 + 4 and len(counts["rejects"]) == counts["rejected"]
    assert tuple(conn.execute("SELECT id, path FROM media WHERE uid = ?", (first["uid"],)).fetchone()) == tuple(before)
    odd = conn.execute("SELECT * FROM media WHERE uid = 'nas:movies:odd.mp4'").fetchone()
    assert (odd["title"], odd["year"], odd["duration"], odd["height"], odd["size"], odd["genres"],
            odd["interlaced"], odd["certificate"]) == ("nas:movies:odd.mp4", None, None, None, None, "[]", 0, "12")
    with pytest.raises(IndexFormatError):
        import_index(conn, {"schema": 2, "items": {"not": "a list"}})
    import_index(conn, doc)
    assert conn.execute("SELECT missing FROM media WHERE uid = 'nas:movies:odd.mp4'").fetchone()[0] == 1


def test_refresh_reports_a_malformed_index_instead_of_raising(ctx, monkeypatch):
    from pitv import catalogue, tool_client
    monkeypatch.setattr(tool_client, "request", lambda *a, **k: (200, {"schema": 1}))
    result = catalogue.refresh(ctx["conn"])
    assert result["status"] == "error" and "schema 2" in result["summary"]
    assert catalogue.last_import(ctx["conn"])["status"] == "error"


def test_missing_rating_enrichment_is_separate_and_below_admin_overrides(monkeypatch):
    from pitv import catalogue

    conn = dbm.connect(":memory:")
    dbm.init_db(conn)
    with dbm.tx(conn):
        source = conn.execute("INSERT INTO sources(uid,type,name,path,enabled)"
                              " VALUES ('tv','tv','TV','/tv',1)").lastrowid
        show_id = conn.execute("INSERT INTO shows(source_id,path,title,year,genres,updated_at)"
                               " VALUES (?, 'danger-mouse', 'Danger Mouse', 1981, '[]', ?)",
                               (source, dbm.now_ts())).lastrowid
    monkeypatch.setattr(catalogue, "_lookup_metadata", lambda *a, **k: ({
        "title": "Danger Mouse", "year": 1981, "certificate": "U",
        "genres": ["Animation", "Children's"], "match": {"source": "tmdb"},
    }, ""))

    result = catalogue.enrich_missing_metadata(conn, limit=1)
    row = dbm.row_to_dict(conn.execute("SELECT * FROM shows WHERE id = ?", (show_id,)).fetchone())
    assert result["found"] == 1 and row["certificate"] is None
    assert dbm.effective(row)["certificate"] == "U"
    assert row["metadata_source"] == "tmdb" and dbm.effective(row)["kids"] == 1

    with dbm.tx(conn):
        conn.execute("UPDATE shows SET overrides = ? WHERE id = ?", ('{"certificate":"PG"}', show_id))
    row = dbm.row_to_dict(conn.execute("SELECT * FROM shows WHERE id = ?", (show_id,)).fetchone())
    assert dbm.effective(row)["certificate"] == "PG", "an explicit edit must win over online metadata"
    conn.close()


def test_document_values_are_read_defensively():
    assert [dbm.as_int(v) for v in (3, "4", 5.9, True, "x", None, 2 ** 70, "inf")] == [3, 4, 5, None, None, None, None, None]
    assert [dbm.as_float(v) for v in ("2.5", float("nan"), [1])] == [2.5, None, None]
    assert [dbm.as_text(v) for v in ("a", 12, None, {"a": 1})] == ["a", "12", None, None]
    assert [dbm.as_bool(v) for v in (True, "false", "yes", 0, None)] == [True, False, True, False, False]
    assert dbm.as_bool(None, True) is True
    assert dbm.genre_list('["Drama", "", 5, {"x": 1}]') == ["Drama", "5"]
    assert dbm.genre_list("Comedy") == ["Comedy"] and dbm.genre_list(7) == [] and dbm.genre_list("1984") == ["1984"]


def test_sql_identifiers_are_checked():
    conn = dbm.connect(":memory:")
    dbm.init_db(conn)
    with pytest.raises(ValueError):
        dbm.update_row(conn, "channels", 1, {"name = 'x' --": "y"})
    with pytest.raises(ValueError):
        dbm.find_id(conn, "channels; DROP TABLE media", "id", 1)


def test_tool_client_refuses_other_schemes_redirects_and_oversized_answers(monkeypatch):
    import http.server
    import threading

    from pitv import tool_client
    status, payload = tool_client.request("file:///etc", "GET", "library")
    assert status == 503 and payload["offline"] is True

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.endswith("/moved"):
                self.send_response(302)
                self.send_header("Location", "http://192.0.2.1/api/library")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b'{"x": "' + b"y" * 200 + b'"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        assert tool_client.request(base, "GET", "library")[0] == 200
        status, payload = tool_client.request(base, "GET", "moved")   # never followed to 192.0.2.1
        assert status == 502 and "redirect" in payload["error"]
        monkeypatch.setattr(tool_client, "MAX_RESPONSE_BYTES", 100)
        status, payload = tool_client.request(base, "GET", "library")
        assert status == 502 and "more than 100 bytes" in payload["error"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_fetched_titles_cannot_leave_the_acquire_folder():
    from pitv.content import _wanted_dest
    assert _wanted_dest({"kind": "movie", "title": "../../etc"}, "/acq") == "/acq/movies/..-..-etc"
    assert _wanted_dest({"kind": "movie", "title": ".."}, "/acq") == "/acq/movies/Unknown"
    assert _wanted_dest({"kind": "music", "title": "x", "genre": "rock/../../x"}, "/acq") == "/acq/music videos/Rock-..-..-X"


def test_idents_follow_their_channel_not_its_number(tmp_path):
    """An ident is tied to a channel by id once, from the number its file was made for, so
    renumbering channels does not move it; the builder never borrows another channel's."""
    from pitv.scheduler.build import Builder
    ctx = make_library(tmp_path, max_episodes=1)
    conn, doc = ctx["conn"], ctx["lib"]["index"]
    ident = dict(conn.execute("SELECT id, channel_hint, home_channel_id FROM media WHERE kind = 'ident'"
                              " AND channel_hint IS NOT NULL LIMIT 1").fetchone())
    home = conn.execute("SELECT id FROM channels WHERE number = ?", (ident["channel_hint"],)).fetchone()["id"]
    assert ident["home_channel_id"] == home
    with dbm.tx(conn):
        conn.execute("UPDATE channels SET number = number + 100")
    import_index(conn, doc)
    assert conn.execute("SELECT home_channel_id FROM media WHERE id = ?", (ident["id"],)).fetchone()[0] == home
    b = Builder(conn)
    others = [c for c in b.channels if c["id"] != home]
    for c in others:
        picked = b._choose_ident(c, random.Random(1), 3600)
        assert picked is None or picked.get("home_channel_id") in (None, c["id"])
