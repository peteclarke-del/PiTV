"""The library index import and where playback finds files."""

import copy

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
    assert toons["category"] == "cartoon" and toons["kids"] == 1


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
    from pitv.catalogue import _family_safe
    kw = ["ale", "gin", "lager"]
    assert _family_safe({"title": "MFI furniture sale"}, kw) == 1
    assert _family_safe({"title": "Castrol GTX (Liquid engineering)"}, kw) == 1
    assert _family_safe({"title": "Hemeling Lager"}, kw) == 0
    assert _family_safe({"title": "Real Ale"}, kw) == 0


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
