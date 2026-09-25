"""The library index import and where playback finds files."""

import copy
import json
import random
import time

import pytest
from conftest import make_library

from pitv import db as dbm
from pitv.catalogue import IndexFormatError, import_index
from pitv.player.cache import MediaCache


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    return make_library(tmp_path_factory.mktemp("catalogue"), max_episodes=4)


def test_import_creates_catalogue_and_is_idempotent(ctx):
    """Importing the same index twice changes nothing and moves no id.

    Counted over the index's own uids rather than the whole media table: other tests here file
    material that never came from an index, a delivered episode or a fetched film, and those are
    real rows with no index item behind them."""
    conn, doc = ctx["conn"], ctx["lib"]["index"]
    listed = {it["uid"] for it in doc["items"]}
    ids = {r["uid"]: r["id"] for r in conn.execute("SELECT id, uid FROM media") if r["uid"] in listed}
    assert len(ids) == len(listed), "everything the index lists is in the catalogue"
    counts = import_index(conn, doc)
    assert counts["new"] == 0 and counts["missing"] == 0 and counts["rejected"] == 0
    again = {r["uid"]: r["id"] for r in conn.execute("SELECT id, uid FROM media") if r["uid"] in listed}
    assert again == ids, "ids must be stable"
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


def test_a_share_mounted_elsewhere_is_read_where_it_actually_is():
    """pitv_content publishes the path it indexed. PiTV may have the same share somewhere else,
    on a desktop with no /mnt or with the two on different machines, so the source carries its
    own mount point and the items under it are read beneath that. Getting this wrong means every
    file is looked for where it is not, and nothing plays."""
    from pitv.catalogue import local_path

    assert local_path("/mnt/tv/Minder/S01E01.mkv", "/mnt/tv", "/media/nas/tv") == "/media/nas/tv/Minder/S01E01.mkv"
    assert local_path("/mnt/tv/x.mkv", "/mnt/tv/", "/media/nas/tv/") == "/media/nas/tv/x.mkv", "trailing slashes"
    # Nothing to translate, or a path from another share: left exactly as it is.
    assert local_path("/mnt/tv/x.mkv", "/mnt/tv", None) == "/mnt/tv/x.mkv"
    assert local_path("/mnt/tv/x.mkv", "", "/media/nas") == "/mnt/tv/x.mkv"
    assert local_path("/other/x.mkv", "/mnt/tv", "/media/nas/tv") == "/other/x.mkv"


def test_advert_keywords_match_whole_words():
    from pitv.catalogue import family_safe
    from pitv.scheduler.rules import keyword_pattern
    kw = keyword_pattern(["ale", "gin", "lager", "18+"])
    assert family_safe({"title": "MFI furniture sale"}, kw) == 1
    assert family_safe({"title": "Castrol GTX (Liquid engineering)"}, kw) == 1
    assert family_safe({"title": "Hemeling Lager"}, kw) == 0
    assert family_safe({"title": "Real Ale"}, kw) == 0
    # A keyword ending in punctuation still matches before a space (\b needed a word character).
    assert family_safe({"title": "Club 18+ holidays"}, kw) == 0
    assert family_safe({"title": "Tags as text", "tags": "alcohol"}, kw) == 0
    assert family_safe({"title": "Milk Tray"}, None) == 1


def test_an_advert_that_names_no_product_is_not_put_in_a_break(ctx):
    """A compilation split into chapters arrives as "Unknown Advert <id> 04": a file the guide
    has nothing to call and that nothing tells from the twenty beside it. It stays in the
    library, flagged for the admin, and out of the breaks until somebody names it."""
    from pitv.scheduler.library import Library
    from pitv.scheduler.policy import SchedulerPolicy
    from pitv.scheduler.rules import keyword_pattern, names_a_product

    kw = keyword_pattern(["unknown", "untitled"])
    assert names_a_product("Hula Hoops", kw) and names_a_product("Anything at all", None)
    for nameless in ("Unknown Advert gYyX_P8aAHs 01", "<Untitled Chapter 1>", "   ", None):
        assert not names_a_product(nameless, kw)

    conn = ctx["conn"]
    advert = conn.execute("SELECT id FROM media WHERE kind = 'advert' AND missing = 0 LIMIT 1").fetchone()["id"]
    with dbm.tx(conn):
        conn.execute("UPDATE media SET title = 'Unknown Advert aB3dE5gH7jK 02' WHERE id = ?", (advert,))
        dbm.set_setting(conn, "unnamed_advert_keywords", ["unknown"])
    settings = dbm.all_settings(conn)
    library = Library(conn, SchedulerPolicy(settings, dbm.now_ts()), now=dbm.now_ts())
    assert advert not in {a["id"] for a in library.adverts}, "it is not offered to a break"
    assert conn.execute("SELECT COUNT(*) FROM media WHERE id = ?", (advert,)).fetchone()[0] == 1, "it stays in the library"


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


def test_an_import_that_fails_after_placing_is_logged_as_an_error(tmp_path, monkeypatch):
    """The refill after an import met a locked database and raised, and its run stayed
    "running" for good: a failure recorded as work in progress, which nothing reports."""
    import sqlite3

    from pitv import catalogue

    lib = make_library(tmp_path / "locked", max_episodes=1)

    def locked(conn):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(catalogue, "refill_empty_days", locked)
    with pytest.raises(sqlite3.OperationalError):
        catalogue.import_and_place(lib["conn"], lib["lib"]["index"], "test")
    last = catalogue.last_import(lib["conn"])
    assert last["status"] == "error" and "database is locked" in last["summary"]
    lib["conn"].close()


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


def test_a_series_the_index_knew_nothing_about_is_filled_in_and_given_its_channel(tmp_path, monkeypatch):
    """A cartoon with no year, genres or certificate in the index is not known to be for
    children, so it falls to a general channel and its episodes are flagged for their year. The
    online check fills in what it can, even when it finds no certificate; the series then has a
    year its episodes inherit, and is placed again, this time where cartoons go."""
    from pitv import catalogue, lineup
    ctx = make_library(tmp_path, max_episodes=2)
    conn = ctx["conn"]
    toons = conn.execute("SELECT id FROM channels WHERE content = 'cartoons'").fetchone()["id"]
    show = conn.execute("SELECT id FROM shows WHERE home_channel_id = ? LIMIT 1", (toons,)).fetchone()["id"]
    with dbm.tx(conn):
        conn.execute("UPDATE shows SET year = NULL, genres = '[]', certificate = NULL, kids = 0, enriched = '{}',"
                     " metadata_checked_at = NULL WHERE id = ?", (show,))
        conn.execute("UPDATE media SET year = NULL, attention = 'No year found' WHERE show_id = ?", (show,))
        conn.execute("DELETE FROM lineup WHERE show_id = ?", (show,))
    lineup.generate(conn)
    assert conn.execute("SELECT home_channel_id FROM shows WHERE id = ?", (show,)).fetchone()[0] != toons
    monkeypatch.setattr(catalogue, "_lookup_metadata", lambda *a, **k: (
        {"year": 1999, "genres": ["Animation", "Comedy", "Family"], "match": {"source": "tvmaze", "id": "1"}}, ""))
    assert catalogue.enrich_missing_metadata(conn, limit=500, force=True)["found"] >= 1
    row = dbm.effective(dbm.row_to_dict(conn.execute("SELECT * FROM shows WHERE id = ?", (show,)).fetchone()))
    assert row["year"] == 1999 and row["kids"] == 1 and "Animation" in row["genres"] and not row.get("certificate")
    assert row["home_channel_id"] == toons, "placed again now that its genres are known"
    assert not conn.execute("SELECT 1 FROM media WHERE show_id = ? AND attention LIKE '%No year%'", (show,)).fetchone()
    # The next import recomputes every note from the index, where these episodes still have no
    # year; they inherit the series' year, so the flags stay cleared.
    catalogue.import_index(conn, json.loads(json.dumps(ctx["lib"]["index"]).replace('"year": 19', '"year_was": 19')))
    assert not conn.execute("SELECT 1 FROM media WHERE show_id = ? AND attention LIKE '%No year%'", (show,)).fetchone()


def test_document_values_are_read_defensively():
    assert [dbm.as_int(v) for v in (3, "4", 5.9, True, "x", None, 2 ** 70, "inf")] == [3, 4, 5, None, None, None, None, None]
    assert [dbm.as_float(v) for v in ("2.5", float("nan"), [1])] == [2.5, None, None]
    assert [dbm.as_text(v) for v in ("a", 12, None, {"a": 1})] == ["a", "12", None, None]
    assert [dbm.as_bool(v) for v in (True, "false", "yes", 0, None)] == [True, False, True, False, False]
    assert dbm.as_bool(None, True) is True
    assert dbm.genre_list('["Drama", "", 5, {"x": 1}]') == ["Drama"]
    assert dbm.genre_list("Comedy") == ["Comedy"]
    assert dbm.genre_list(7) == [] and dbm.genre_list("1984") == []


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


def test_an_ident_named_after_its_channel_survives_renumbering(tmp_path):
    """An ident belongs to the channel its name says, so moving the channels about leaves it
    where it is. One filed only by folder number has nothing to hold it when the numbers move,
    and becomes generic rather than announcing whichever channel took that number. Either way
    no channel shows another's ident."""
    from pitv.scheduler.build import Builder
    ctx = make_library(tmp_path, max_episodes=1)
    conn, doc = ctx["conn"], ctx["lib"]["index"]
    def ident(hint, place):
        return dict(conn.execute("SELECT id, title, channel_hint, home_channel_id FROM media WHERE kind = 'ident'"
                                 " AND channel_hint = ? ORDER BY title LIMIT 1 OFFSET ?", (hint, place)).fetchone())
    # Two idents with different titles, so renaming one channel after the first cannot claim both.
    named, numbered = ident(1, 0), ident(2, 1)
    assert named["title"] != numbered["title"]
    home = conn.execute("SELECT id FROM channels WHERE number = 1").fetchone()["id"]
    assert named["home_channel_id"] == home, "the folder number files it to begin with"
    with dbm.tx(conn):          # the channel takes the name its ident file already carries
        conn.execute("UPDATE channels SET name = ? WHERE id = ?", (named["title"], home))
        conn.execute("UPDATE channels SET number = number + 100")
    import_index(conn, doc)
    homes = {r["id"]: r["home_channel_id"] for r in conn.execute(
        "SELECT id, home_channel_id FROM media WHERE id IN (?, ?)", (named["id"], numbered["id"]))}
    assert homes[named["id"]] == home, "the name still says whose it is"
    assert homes[numbered["id"]] is None, "the number no longer does, so it is generic"
    b = Builder(conn)
    for c in (x for x in b.channels if x["id"] != home):
        picked = b.select.ident(c, random.Random(1), 3600)
        assert picked is None or picked.get("home_channel_id") in (None, c["id"])


def test_what_the_pi_can_play_is_copied_not_transcoded():
    """The copy rule is what the Pi can play, not what suits the screen: a 1080p film is copied
    for a 576 line tube, and only codecs or sizes the Pi cannot manage are re-encoded."""
    from pitv.player.hwdec import pi_can_play
    assert pi_can_play({"vcodec": "h264", "height": 1080})
    assert pi_can_play({"vcodec": "hevc", "height": 2160, "interlaced": 0})
    assert pi_can_play({"vcodec": "h264", "height": 576, "interlaced": 1}), "hardware decode leaves room to deinterlace"
    assert pi_can_play({"vcodec": "mpeg4", "height": 480})
    assert pi_can_play({"vcodec": "h264", "height": None}), "an unknown height is not a reason to re-encode"
    assert not pi_can_play({"vcodec": "h264", "height": 2160})
    assert not pi_can_play({"vcodec": "mpeg2video", "height": 576, "interlaced": 1})
    assert not pi_can_play({"vcodec": "mpeg4", "height": 720})
    assert not pi_can_play({"vcodec": "vp9", "height": 480}) and not pi_can_play({"vcodec": None})


def test_a_title_shared_by_an_original_and_its_remake_is_read_as_the_original(monkeypatch):
    """With no year in the index, "The Powerpuff Girls" matches the 1998 series and the 2016
    one. Six seasons on disk rule out a three-year run, the original is preferred in any case,
    and only that programme's matches are put together."""
    from pitv import catalogue, tool_client
    answer = {"candidates": [
        {"title": "The Powerpuff Girls", "year": 2016, "end_year": 2019, "genres": ["Crime"], "match": {"source": "tvmaze", "id": 2}},
        {"title": "The Powerpuff Girls", "year": 1998, "end_year": 2005, "genres": ["Animation"], "match": {"source": "tvmaze", "id": 1}},
        {"title": "The Powerpuff Girls", "year": 1998, "end_year": 2005, "certificate": "U", "genres": ["Family"], "match": {"source": "tmdb-tv", "id": 9}},
        {"title": "The Powerpuff Girls", "year": 2016, "end_year": 2019, "certificate": "12", "match": {"source": "tmdb-tv", "id": 8}},
    ]}
    monkeypatch.setattr(tool_client, "request", lambda *a, **k: (200, answer))
    found, _ = catalogue._lookup_metadata({}, "show", "The Powerpuff Girls", None, seasons=6)
    assert found["year"] == 1998 and found["certificate"] == "U" and found["genres"] == ["Animation", "Family"]
    found, _ = catalogue._lookup_metadata({}, "show", "The Powerpuff Girls", 2017)
    assert found["year"] == 2016 and found["certificate"] == "12", "a year in the index settles it"


def test_every_british_certificate_is_recognised():
    """U was missing from the table of certificates, so every U film counted as unrated, was
    treated as a 15 and kept off daytime television, and was looked up again at every import."""
    from pitv.scheduler.rules import normalise_cert
    assert [normalise_cert(c) for c in ("U", "u", "UK:U", "BBFC U", "Uc", "PG", "12", "12A", "15", "18")] == \
        ["U", "U", "U", "U", "U", "PG", "12", "12A", "15", "18"]
    assert normalise_cert("TV-Y7") == "U" and normalise_cert("R") == "15" and normalise_cert("nonsense") is None
    # As media managers write them: a list, each with its country, sometimes spelt out.
    assert normalise_cert("US:R / US:Rated R") == "15" and normalise_cert("Rated PG-13") == "12"
    assert normalise_cert("US:PG-13 / UK:15") == "15", "a British certificate wins wherever it stands"
    assert normalise_cert("NR") is None and normalise_cert("DE:16") is None


def test_a_film_matches_despite_an_edition_note_or_a_subtitle_but_only_in_its_own_year():
    """A library names the edition and a listing often gives a longer title. With the same year
    those are the same film; without it, or with a different year, they are not."""
    from pitv.catalogue import _compatible_candidate
    assert _compatible_candidate("movie", "Alien (Directors Cut)", 1979, {"title": "Alien", "year": 1979})
    assert _compatible_candidate("movie", "Rogue One", 2016, {"title": "Rogue One: A Star Wars Story", "year": 2016})
    assert _compatible_candidate("movie", "Hotel Transylvania 3", 2018, {"title": "Hotel Transylvania 3: Summer Vacation", "year": 2018})
    assert not _compatible_candidate("movie", "Rogue One", 2017, {"title": "Rogue One: A Star Wars Story", "year": 2016})
    assert not _compatible_candidate("movie", "Alien", 1979, {"title": "Aliens", "year": 1986})
    assert not _compatible_candidate("movie", "Seven", 1995, {"title": "Seven Samurai", "year": 1954})
    assert not _compatible_candidate("show", "Bottom", 1991, {"title": "Bottom Live", "year": 1991}), "series keep the exact rule"


def test_a_film_is_matched_despite_its_edition_note_or_a_shortened_title():
    """A library names the edition and shortens titles; a listing does neither. With the exact
    year, "Alien (Directors Cut)" is Alien and "Rogue One" is "Rogue One: A Star Wars Story";
    without it, or for a different film, nothing is assumed."""
    from pitv.catalogue import _compatible_candidate as same
    assert same("movie", "Alien (Directors Cut)", 1979, {"title": "Alien", "year": 1979})
    assert same("movie", "Star Wars: Episode IV - A New Hope (Original Theatrical Version)", 1977,
                {"title": "Star Wars: Episode IV - A New Hope", "year": 1977})
    assert same("movie", "Rogue One", 2016, {"title": "Rogue One: A Star Wars Story", "year": 2016})
    assert same("movie", "Hotel Transylvania 3", 2018, {"title": "Hotel Transylvania 3: Summer Vacation", "year": 2018})
    assert not same("movie", "Rogue One", 2015, {"title": "Rogue One: A Star Wars Story", "year": 2016}), "only with the exact year"
    assert not same("movie", "Alien", 1979, {"title": "Aliens", "year": 1979}), "a short title does not open a longer one"
    assert not same("movie", "Seven", 1995, {"title": "Seven Samurai", "year": 1995}), "too short a title to open another"
    assert not same("show", "Minder", 1979, {"title": "Minder on the Orient Express", "year": 1979}), "films only"


def test_attention_is_for_what_the_owner_can_act_on():
    """A file the Pi cannot decode in hardware is transcoded by pitv_content when it is scheduled,
    without anyone doing anything, so it is not something that needs a look."""
    from pitv.catalogue import _attention
    base = {"duration": 5400, "year": 1999, "kind": "movie", "certificate": "PG", "hwdec": 0, "height": 1080, "vcodec": "vp9"}
    assert _attention(base) is None
    assert "No certificate" in _attention({**base, "certificate": None})


def test_the_nfo_identifier_is_asked_before_the_title(ctx, monkeypatch):
    """An index item's `ids` are kept only in their documented shapes, and the online check asks
    by identifier first: the library's "Leon" is "Léon: The Professional" to every source, which
    no title search settles. An identifier whose programme is from another decade is not
    believed, and the title is asked instead."""
    from urllib.parse import parse_qs

    from pitv import catalogue, tool_client
    conn = ctx["conn"]
    doc = copy.deepcopy(ctx["lib"]["index"])
    film = next(i for i in doc["items"] if i["kind"] == "movie")
    film.update(title="Leon", year=1994, certificate=None,
                ids={"imdb": "tt0110413", "tmdb": 101, "tvdb": "x; DROP", "other": "1"})
    import_index(conn, doc)
    row = dbm.row_to_dict(conn.execute("SELECT * FROM media WHERE uid = ?", (film["uid"],)).fetchone())
    assert row["ids"] == {"imdb": "tt0110413", "tmdb": "101"}

    asked = []

    def answer(base, method, path, query="", **kw):
        q = {k: v[0] for k, v in parse_qs(query).items()}
        asked.append(q)
        if "imdb" in q:
            return 200, {"candidates": [{"title": "Léon: The Professional", "year": 1994, "certificate": "18",
                                         "genres": ["Crime"], "match": {"source": "tmdb", "id": "101"}}]}
        return 200, {"candidates": []}
    monkeypatch.setattr(tool_client, "request", answer)
    found, _ = catalogue._lookup_metadata({}, "movie", "Leon", 1994, ids=row["ids"])
    assert found["certificate"] == "18" and "title" not in asked[0] and asked[0]["imdb"] == "tt0110413"

    asked.clear()
    found, _ = catalogue._lookup_metadata({}, "movie", "Leon", 1962, ids=row["ids"])
    assert found is None and [("title" in q) for q in asked] == [False, True], "the wrong decade falls back to the title"


def test_make_room_measures_the_whole_cache_folder(tmp_path):
    """The cap covers the folder, fetched material included, which is how pitv_content measures it.
    Counting only its own copies, PiTV judged that a delivery still fitted and evicted nothing,
    while pitv_content, at the cap by its own count, refused every delivery."""
    import os
    import time
    cache_dir = tmp_path / "cache"
    (cache_dir / "acquired" / "tvshows").mkdir(parents=True)
    (cache_dir / "acquired" / "tvshows" / "fetched.mp4").write_bytes(b"x" * 600)
    old = time.time() - 3 * 3600
    for n in (1, 2, 3):
        copy = cache_dir / f"{n}_Film_{n}.mkv"
        copy.write_bytes(b"x" * 400)
        os.utime(copy, (old + n, old + n))
    cache = MediaCache(cache_dir, max_bytes=2000)
    # Copies are 1200 and a delivery of 300 would fit under 2000 by that count; the folder holds
    # 1800, so it does not, and the least recently used copy goes.
    cache.make_room(300)
    assert sorted(p.name for p in cache_dir.glob("*.mkv")) == ["2_Film_2.mkv", "3_Film_3.mkv"]
    assert (cache_dir / "acquired" / "tvshows" / "fetched.mp4").exists(), "fetched material is not a copy to evict"


def test_an_ident_in_a_flat_folder_finds_its_channel_by_name(tmp_path):
    """Idents kept in one folder carry no channel in their path. One whose title begins with a
    channel's name is that channel's, the longest name winning, and one that matches nothing is
    generic. The file is the only say: a channel set on an ident by hand is put back."""
    ctx = make_library(tmp_path, 1)
    conn = ctx["conn"]
    one, two = (conn.execute("SELECT id FROM channels WHERE number = ?", (n,)).fetchone()["id"] for n in (1, 2))
    with dbm.tx(conn):
        conn.execute("UPDATE channels SET name = 'PiTV' WHERE id = ?", (two,))          # a shorter name that also matches
        conn.execute("UPDATE media SET home_channel_id = NULL, channel_hint = NULL WHERE kind = 'ident'")
        ids = [r["id"] for r in conn.execute("SELECT id FROM media WHERE kind = 'ident' AND missing = 0 ORDER BY id LIMIT 3")]
        assert len(ids) == 3, "the fixture library carries idents"
        for media_id, title in zip(ids, ("PiTV One ident", "PiTV late night", "Station clock"), strict=True):
            conn.execute("UPDATE media SET title = ? WHERE id = ?", (title, media_id))
        dbm.assign_ident_channels(conn)
    homes = [conn.execute("SELECT home_channel_id FROM media WHERE id = ?", (i,)).fetchone()[0] for i in ids]
    assert homes == [one, two, None]
    with dbm.tx(conn):
        conn.execute("UPDATE media SET home_channel_id = ? WHERE id = ?", (two, ids[0]))   # pointed elsewhere by hand
        dbm.assign_ident_channels(conn)
    assert conn.execute("SELECT home_channel_id FROM media WHERE id = ?", (ids[0],)).fetchone()[0] == one, \
        "the name is the answer, so the next import puts it back"


def test_a_reindex_job_nobody_can_see_is_not_waited_on(monkeypatch):
    """pitv_content may answer with the id of a request already queued. One queued yesterday that
    has never had its turn has fallen off the end of the job list, and looks exactly like one
    queued a moment ago that has not started. A fresh rebuild sat for half an hour on such an id;
    the index in hand is imported instead."""
    import time as _time

    from pitv import catalogue, tool_client

    monkeypatch.setattr(catalogue, "REINDEX_UNSEEN", 0.05)
    monkeypatch.setattr(catalogue, "REINDEX_POLL", 0.01)
    polls = 0

    def fake_request(base, method, path, query="", body=None, timeout=15):
        nonlocal polls
        if path == "index":
            return 200, {"ok": True, "job_id": "20260920-080355-10e5", "deduplicated": True}
        polls += 1
        return 200, [{"job_id": "something-else", "finished_ts": 1}]

    monkeypatch.setattr(tool_client, "request", fake_request)
    started = _time.monotonic()
    catalogue._reindex("http://127.0.0.1:8091", timeout=30)
    assert _time.monotonic() - started < 5, "it must not wait out the full timeout"
    assert polls >= 1


def test_a_reindex_job_that_is_running_is_waited_for(monkeypatch):
    """A job that does appear is waited on properly, however long it takes to start."""
    from pitv import catalogue, tool_client

    monkeypatch.setattr(catalogue, "REINDEX_UNSEEN", 0.05)
    monkeypatch.setattr(catalogue, "REINDEX_POLL", 0.01)
    answers = [
        [],                                                     # not listed yet
        [{"job_id": "j1", "status": "queued"}],                 # queued, past the unseen grace
        [{"job_id": "j1", "status": "running"}],
        [{"job_id": "j1", "status": "ok", "finished_ts": 2}],
    ]

    def fake_request(base, method, path, query="", body=None, timeout=15):
        if path == "index":
            return 200, {"ok": True, "job_id": "j1"}
        return 200, answers.pop(0) if answers else [{"job_id": "j1", "status": "ok", "finished_ts": 2}]

    monkeypatch.setattr(tool_client, "request", fake_request)
    catalogue._reindex("http://127.0.0.1:8091", timeout=30)
    assert not answers, "it followed the job to the end"


def test_a_reindex_queued_behind_other_work_is_not_waited_on(monkeypatch):
    """A queued index has not started and may be behind hours of delivery. One evening a rebuild
    waited forty minutes for an index that never ran, when the index it would have replaced was
    minutes old: maintenance imports every rewrite, so what is in hand is nearly current."""
    from pitv import catalogue, tool_client

    monkeypatch.setattr(catalogue, "REINDEX_QUEUED", 0.05)
    monkeypatch.setattr(catalogue, "REINDEX_POLL", 0.01)
    polls = 0

    def fake_request(base, method, path, query="", body=None, timeout=15):
        nonlocal polls
        if path == "index":
            return 200, {"ok": True, "job_id": "j1"}
        polls += 1
        return 200, [{"job_id": "j1", "status": "queued", "started_ts": None}]

    monkeypatch.setattr(tool_client, "request", fake_request)
    started = time.monotonic()
    catalogue._reindex("http://127.0.0.1:8091", timeout=30)
    assert time.monotonic() - started < 5, "it must not wait out the running-index timeout"
    assert polls >= 1


def test_a_reindex_that_is_running_is_given_its_time(monkeypatch):
    """One that has started is nearly done, so it is waited for; the point of asking was a fresh
    index and this is the only case that delivers one."""
    from pitv import catalogue, tool_client

    monkeypatch.setattr(catalogue, "REINDEX_QUEUED", 0.05)
    monkeypatch.setattr(catalogue, "REINDEX_POLL", 0.01)
    answers = [
        [{"job_id": "j1", "status": "queued", "started_ts": None}],
        [{"job_id": "j1", "status": "running", "started_ts": 100}],
        [{"job_id": "j1", "status": "running", "started_ts": 100}],
        [{"job_id": "j1", "status": "ok", "started_ts": 100, "finished_ts": 200}],
    ]

    def fake_request(base, method, path, query="", body=None, timeout=15):
        if path == "index":
            return 200, {"ok": True, "job_id": "j1"}
        return 200, answers.pop(0) if answers else [{"job_id": "j1", "finished_ts": 200, "status": "ok"}]

    monkeypatch.setattr(tool_client, "request", fake_request)
    catalogue._reindex("http://127.0.0.1:8091", timeout=30)
    assert not answers, "it followed the job from queued through running to done"
