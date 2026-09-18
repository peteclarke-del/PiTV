"""Channel line-ups: exclusive membership, genre-driven generation, external entries."""

import json
from itertools import pairwise

import pytest
from conftest import make_library

from pitv import db as dbm
from pitv import lineup
from pitv.scheduler.horizon import build_horizon
from pitv.scheduler.rules import local_ts, tz_of
from pitv.scheduler.slots import parse_day


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    ctx = make_library(tmp_path_factory.mktemp("lineup"), max_episodes=12)
    return {"conn": ctx["conn"], "data_dir": ctx["cfg"].data_dir}


@pytest.fixture(scope="module")
def conn(env):
    return env["conn"]


@pytest.fixture(scope="module")
def data_dir(env):
    return env["data_dir"]


def test_every_programme_belongs_to_exactly_one_channel(conn):
    shows = conn.execute("SELECT COUNT(*) FROM shows WHERE missing = 0 AND excluded = 0").fetchone()[0]
    movies = conn.execute("SELECT COUNT(*) FROM media WHERE kind = 'movie' AND missing = 0 AND excluded = 0").fetchone()[0]
    entries = conn.execute("SELECT kind, COUNT(*) AS n FROM lineup GROUP BY kind").fetchall()
    counts = {r["kind"]: r["n"] for r in entries}
    assert counts.get("show", 0) == shows and counts.get("movie", 0) == movies
    assert not conn.execute("SELECT show_id FROM lineup WHERE show_id IS NOT NULL GROUP BY show_id HAVING COUNT(*) > 1").fetchall()
    assert not conn.execute("SELECT 1 FROM shows WHERE missing = 0 AND excluded = 0 AND home_channel_id IS NULL").fetchall()
    assert not conn.execute("SELECT 1 FROM media WHERE kind = 'movie' AND missing = 0 AND home_channel_id IS NULL").fetchall()


def test_generation_respects_channel_genres(conn):
    channels = {r["id"]: dbm.row_to_dict(r) for r in conn.execute("SELECT * FROM channels")}
    for e in lineup.entries(conn):
        ch = channels[e["channel_id"]]
        assert ch["content"] in ("general", "cartoons")
        genres = {g.lower() for g in (e["genres"] or [])}
        assert lineup.channel_fit(ch, genres) is not None, (e["title"], genres, ch["allowed_genres"])
    toons = conn.execute("SELECT id FROM channels WHERE content = 'cartoons'").fetchone()["id"]
    titles = {e["title"] for e in lineup.entries(conn, channel_id=toons)}
    assert {"Danger Mouse", "Thundercats"} <= titles


def test_week_never_shares_a_programme_across_channels(conn):
    now = local_ts(parse_day("2026-09-14"), "07:00", tz_of(conn))
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=7, now=now, seed=5)
    rows = conn.execute(
        "SELECT COALESCE(m.show_id, -m.id) AS prog, COUNT(DISTINCT s.channel_id) AS channels FROM schedule s"
        " JOIN media m ON m.id = s.media_id WHERE s.kind = 'programme' AND m.kind IN ('episode', 'movie')"
        " GROUP BY prog HAVING channels > 1").fetchall()
    assert not rows, [dict(r) for r in rows]
    homes = conn.execute(
        "SELECT COUNT(*) FROM schedule s JOIN media m ON m.id = s.media_id LEFT JOIN shows sh ON sh.id = m.show_id"
        " WHERE s.kind = 'programme' AND m.kind IN ('episode','movie')"
        " AND s.channel_id != COALESCE(sh.home_channel_id, m.home_channel_id)").fetchone()[0]
    assert homes == 0


def test_move_and_remove_entries(conn, data_dir):
    show = conn.execute("SELECT id, home_channel_id FROM shows WHERE title = 'Minder'").fetchone()
    other = conn.execute("SELECT id FROM channels WHERE content = 'general' AND id != ? ORDER BY number LIMIT 1", (show["home_channel_id"],)).fetchone()["id"]
    entry = lineup.add(conn, other, show_id=show["id"])
    assert entry["channel_id"] == other and entry["pinned"] == 1
    assert conn.execute("SELECT home_channel_id FROM shows WHERE id = ?", (show["id"],)).fetchone()[0] == other
    assert conn.execute("SELECT COUNT(*) FROM lineup WHERE show_id = ?", (show["id"],)).fetchone()[0] == 1
    lineup.remove(conn, entry["id"])
    assert conn.execute("SELECT home_channel_id FROM shows WHERE id = ?", (show["id"],)).fetchone()[0] is None
    lineup.generate(conn)  # unassigned again: generation puts it back somewhere
    assert conn.execute("SELECT home_channel_id FROM shows WHERE id = ?", (show["id"],)).fetchone()[0] is not None
    doc = lineup.export(conn)
    assert doc["schema"] == 1 and any(c["lineup"] for c in doc["channels"])
    lineup.write_mirror(conn)
    assert dbm.data_path(conn, lineup.MIRROR) == data_dir / "lineups.json" and (data_dir / "lineups.json").exists()


def test_external_entry_scheduled_ahead_and_requested(conn):
    ch = conn.execute("SELECT id FROM channels WHERE content = 'general' ORDER BY number LIMIT 1").fetchone()["id"]
    confirmed = {"source": "tvmaze", "id": "2203", "url": "https://www.tvmaze.com/shows/2203/the-tripods", "imdb": "tt0086817"}
    # A title explicitly pinned to this channel remains eligible even when its year lies
    # outside the automatic global era mix (the fixture defaults to the 1980s/1990s).
    entry = lineup.add(conn, ch, title="The Tripods", year=1974, kind="show", genres=["Science Fiction"], episode_minutes=25,
                       match={**confirmed, "poster": "ignored"})
    assert entry["match"] == confirmed
    now = local_ts(parse_day("2026-09-14"), "07:00", tz_of(conn))
    # NAS-only (default): nothing not on disk is scheduled.
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=4, now=now, seed=9, force=True)
    assert conn.execute("SELECT COUNT(*) FROM schedule WHERE wanted_id IS NOT NULL").fetchone()[0] == 0
    with dbm.tx(conn):
        dbm.set_setting(conn, "nas_only", False)
        dbm.set_setting(conn, "external_weight", 50.0)  # make the entry win often in the test library
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=4, now=now, seed=9, force=True)
    slots = conn.execute("SELECT s.*, w.episode, w.transient FROM schedule s JOIN wanted w ON w.id = s.wanted_id"
                         " WHERE s.replay = 0 ORDER BY s.start_ts").fetchall()
    assert slots, "external entry never placed"
    lead = now + 23 * 3600
    assert all(sl["start_ts"] > lead for sl in slots), "placed inside the 23-hour local-first window"
    assert all(sl["media_id"] is None and sl["title"] == "The Tripods" and sl["transient"] == 1 for sl in slots)
    episodes = [sl["episode"] for sl in slots]
    assert episodes == sorted(episodes) and len(set(episodes)) == len(episodes)
    wanted = conn.execute("SELECT * FROM wanted WHERE lineup_id = ?", (entry["id"],)).fetchall()
    assert len(wanted) == len(set(episodes))
    # Rebuilding reuses the wanted rows instead of raising duplicates.
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=4, now=now, seed=9, force=True)
    assert conn.execute("SELECT COUNT(*) FROM wanted WHERE lineup_id = ?", (entry["id"],)).fetchone()[0] >= len(wanted)
    assert not conn.execute("SELECT episode FROM wanted WHERE lineup_id = ? GROUP BY episode HAVING COUNT(*) > 1", (entry["id"],)).fetchall()
    # The manifest carries them for pitv_content with the series title and a search phrase.
    from pitv.content import REMOTE_PRIORITY_HOURS, manifest
    m = manifest(conn, days=3, now=now)
    ids = {w["id"] for w in wanted}
    mine = [i for i in m["items"] if i["wanted_id"] in ids]
    assert mine and all(i["action"] == "fetch" and i["request_id"] == f"w:{i['wanted_id']}" for i in mine)
    assert all(i["match"] == confirmed for i in mine), "the confirmed identity goes to pitv_content with every request"
    assert mine[0]["show_title"] == "The Tripods" and mine[0]["transient"] is True
    assert mine[0]["remote_required"] is True
    local_priorities = [int(max(0.0, (i["first_air_ts"] - now) / 3600) // 4) for i in mine]
    adjustment = REMOTE_PRIORITY_HOURS // 4
    assert all(i["priority"] == local - adjustment
               for i, local in zip(mine, local_priorities, strict=True))
    assert all(i["priority"] < local for i, local in zip(mine, local_priorities, strict=True)), \
        "remote-only material must outrank an equally timed playable NAS fallback"
    assert "The Tripods" in mine[0]["search"]["phrase"] and mine[0]["dest_dir"]
    with dbm.tx(conn):
        dbm.set_setting(conn, "nas_only", True)
        dbm.set_setting(conn, "external_weight", 1.0)


def test_every_unseen_remote_title_gets_a_variety_slot(conn):
    """A healthy local library must not starve configured remote catalogue entries."""
    ch = conn.execute("SELECT id FROM channels WHERE content = 'general' ORDER BY number LIMIT 1").fetchone()["id"]
    titles = ("Remote History One", "Remote History Two", "Remote History Three")
    for n, title in enumerate(titles, 1):
        lineup.add(conn, ch, title=title, year=1985, kind="show", genres=["Documentary"],
                   episode_minutes=25, match={"source": "manual", "id": f"remote-{n}"})
    with dbm.tx(conn):
        dbm.set_setting(conn, "nas_only", False)
        dbm.set_setting(conn, "external_weight", 1.0)

    now = local_ts(parse_day("2026-09-14"), "07:00", tz_of(conn))
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=3, now=now, seed=17, force=True)
    scheduled = {r["title"] for r in conn.execute(
        "SELECT DISTINCT s.title FROM schedule s JOIN wanted w ON w.id = s.wanted_id"
        " WHERE s.channel_id = ? AND s.replay = 0", (ch,))}
    assert set(titles) <= scheduled


def test_readiness_substitutes_unfetched_placeholders(conn):
    from pitv.readiness import check
    placeholder = conn.execute("SELECT * FROM schedule WHERE wanted_id IS NOT NULL AND media_id IS NULL AND replay = 0 ORDER BY start_ts LIMIT 1").fetchone()
    assert placeholder is not None
    now = placeholder["start_ts"] - 6 * 3600
    r = check(conn, now=now, days=1)
    assert r["status"] == "error" and r["substituted"] >= 1
    assert conn.execute("SELECT COUNT(*) FROM schedule WHERE channel_id = ? AND wanted_id IS NOT NULL AND media_id IS NULL"
                        " AND start_ts >= ? AND start_ts < ?", (placeholder["channel_id"], now, now + 86400)).fetchone()[0] == 0
    rows = conn.execute("SELECT start_ts, end_ts FROM schedule WHERE channel_id = ? AND day = ? ORDER BY start_ts",
                        (placeholder["channel_id"], placeholder["day"])).fetchall()
    for a, b in pairwise(rows):
        assert a["end_ts"] == b["start_ts"]


def test_delivery_report_fills_placeholder(conn, tmp_path):
    """A film fetched online arrives in a report: it becomes a catalogue entry, takes its
    placeholder slot at its real length and turns the external entry into a library one."""
    from pitv.content import apply_report
    ch = conn.execute("SELECT id FROM channels WHERE content = 'general' ORDER BY number DESC LIMIT 1").fetchone()["id"]
    entry = lineup.add(conn, ch, title="Pending Film", year=1983, kind="movie", episode_minutes=90)
    start = 4102444800  # far future so the slot is untouched by other tests
    with dbm.tx(conn):
        wid = conn.execute("INSERT INTO wanted(kind, title, year, provider, lineup_id, transient, created_at)"
                           " VALUES ('movie', 'Pending Film', 1983, 'auto', ?, 1, 0)", (entry["id"],)).lastrowid
        conn.execute("INSERT INTO schedule(channel_id, day, start_ts, end_ts, media_id, offset, kind, title, subtitle, wanted_id)"
                     " VALUES (?, '2099-12-31', ?, ?, NULL, 0, 'programme', 'Pending Film', '', ?)",
                     (ch, start, start + 90 * 60, wid))
    film = tmp_path / "Pending Film (1983).mp4"
    film.write_bytes(b"x" * 10)
    counts = apply_report(conn, {"schema": 2, "items": [{
        "request_id": f"w:{wid}", "wanted_id": wid, "status": "done",
        "file": {"path": str(film), "duration": 6420.0, "vcodec": "h264", "height": 576, "size": 10},
        "meta": {"kind": "movie", "title": "Pending Film", "year": 1983, "genres": ["Drama"], "certificate": "PG",
                 "uid": "yt:pending"}}]})
    assert counts["wanted_done"] == 1 and counts["created"] == 1
    media = conn.execute("SELECT * FROM media WHERE uid = 'yt:pending'").fetchone()
    assert media["origin"] == "online" and media["cache_path"] == str(film) and media["transient"] == 1
    slot = conn.execute("SELECT media_id, end_ts, start_ts FROM schedule WHERE wanted_id = ?", (wid,)).fetchone()
    assert slot["media_id"] == media["id"] and slot["end_ts"] - slot["start_ts"] == 6420
    e = lineup.entry(conn, entry["id"])
    assert e["media_id"] == media["id"] and e["external"] is False
    with dbm.tx(conn):
        conn.execute("DELETE FROM schedule WHERE day >= '2099-12-31'")
    lineup.remove(conn, entry["id"])


def test_skip_in_progress_is_neither_done_nor_failed(conn):
    """"skipped, being written by another process" arrives without a file block; it must not use
    up an attempt or count as a failed delivery."""
    from pitv.content import apply_report
    with dbm.tx(conn):
        wid = conn.execute("INSERT INTO wanted(kind, title, provider, created_at) VALUES ('music', 'In Progress', 'auto', 0)").lastrowid
    counts = apply_report(conn, {"schema": 2, "items": [
        {"request_id": f"w:{wid}", "wanted_id": wid, "status": "skipped", "message": "being written by another process", "file": None},
        {"request_id": "m:1", "media_id": 1, "status": "skipped", "message": "being written by another process", "file": None}]})
    assert counts == {"items_done": 0, "items_failed": 0, "wanted_done": 0, "wanted_failed": 0, "created": 0}
    row = conn.execute("SELECT status, attempts FROM wanted WHERE id = ?", (wid,)).fetchone()
    assert (row["status"], row["attempts"]) == ("queued", 0)
    with dbm.tx(conn):
        conn.execute("DELETE FROM wanted WHERE id = ?", (wid,))


def test_report_file_already_posted_is_not_applied_twice(conn, tmp_path):
    from pitv.content import apply_report, apply_report_files
    from pitv.player.cache import MediaCache
    cache = MediaCache(tmp_path / "cache", 10 ** 9)
    cache.reports_dir.mkdir(parents=True)
    report = {"schema": 2, "items": [], "run": {"tool": "pitv-content test", "started_ts": 1234, "finished_ts": 1240}}
    apply_report(conn, report)                                   # posted over HTTP
    (cache.reports_dir / "r.json").write_text(json.dumps(report))
    assert apply_report_files(conn, cache) == 0                  # the dropped copy is recognised
    assert (cache.reports_dir / "r.json.applied").exists()
    assert conn.execute("SELECT COUNT(*) FROM run_log WHERE kind = 'content' AND started_at = 1234").fetchone()[0] == 1


def test_gap_episode_is_filed_under_its_series(conn, tmp_path):
    """A wanted row raised for a gap in a library series already names the series; the delivered
    episode belongs to it rather than to a new series created for online material."""
    from pitv.content import apply_report
    show = conn.execute("SELECT id, title, year FROM shows WHERE title = 'Minder'").fetchone()
    with dbm.tx(conn):
        wid = conn.execute("INSERT INTO wanted(kind, title, year, season, episode, show_id, provider, auto, created_at)"
                           " VALUES ('episode', ?, ?, 9, 3, ?, 'auto', 1, 0)", (show["title"], show["year"], show["id"])).lastrowid
    f = tmp_path / "Minder - S09E03.mp4"
    f.write_bytes(b"x" * 10)
    counts = apply_report(conn, {"schema": 2, "items": [{
        "wanted_id": wid, "status": "done", "file": {"path": str(f), "duration": 3000.0, "vcodec": "h264"},
        "meta": {"kind": "episode", "show_title": "Minder (fetched)", "season": 9, "episode": 3, "uid": "yt:gap"}}]})
    assert counts["wanted_done"] == 1
    media = conn.execute("SELECT show_id, season, episode FROM media WHERE uid = 'yt:gap'").fetchone()
    assert tuple(media) == (show["id"], 9, 3)
    assert not conn.execute("SELECT 1 FROM shows WHERE path LIKE 'fetched:show:minder%'").fetchone()
    with dbm.tx(conn):
        conn.execute("DELETE FROM media WHERE uid = 'yt:gap'")
        conn.execute("DELETE FROM wanted WHERE id = ?", (wid,))


def test_malformed_report_entries_are_not_fatal(conn, tmp_path):
    from pitv.content import apply_report, apply_report_files
    from pitv.player.cache import MediaCache
    with dbm.tx(conn):
        wid = conn.execute("INSERT INTO wanted(kind, title, provider, created_at) VALUES ('music', 'Odd', 'auto', 0)").lastrowid
    counts = apply_report(conn, {"items": [
        {"wanted_id": "inf", "media_id": "nan", "status": "done", "file": {"path": 5}},
        {"wanted_id": wid, "status": "done", "file": {"path": str(tmp_path / "absent.mp4")}},
        "junk"], "wanted": {"not": "a list"}, "run": {"started_ts": "soon", "tool": ["x"]}})
    assert counts["wanted_failed"] == 1 and counts["items_failed"] == 1
    row = conn.execute("SELECT status, attempts, message FROM wanted WHERE id = ?", (wid,)).fetchone()
    assert (row["status"], row["attempts"], row["message"]) == ("queued", 1, "reported file does not exist")
    with pytest.raises(TypeError):
        apply_report(conn, [])
    cache = MediaCache(tmp_path / "cache", 10 ** 9)
    cache.reports_dir.mkdir(parents=True)
    (cache.reports_dir / "list.json").write_text("[]")
    assert apply_report_files(conn, cache) == 0 and not (cache.reports_dir / "list.json.applied").exists()
    with dbm.tx(conn):
        conn.execute("DELETE FROM wanted WHERE id = ?", (wid,))


def test_transient_removal_never_leaves_the_cache(tmp_path):
    """Only files inside the cache are deleted; a symlink in the cache goes as a link and its
    target (standing in for the read-only NAS) is untouched."""
    cache, nas = tmp_path / "cache", tmp_path / "nas"
    cache.mkdir()
    nas.mkdir()
    inside, outside, target = cache / "fetched.mp4", nas / "original.mkv", nas / "linked.mkv"
    for f in (inside, outside, target):
        f.write_bytes(b"x")
    link = cache / "link.mkv"
    link.symlink_to(target)
    sneaky = cache / ".." / "nas" / "original.mkv"
    conn = dbm.connect(":memory:")
    dbm.init_db(conn)
    now = 10 ** 9
    with dbm.tx(conn):
        dbm.set_setting(conn, "cache_dir", str(cache))
        for path in (inside, outside, link, sneaky):
            mid = conn.execute("INSERT INTO media(kind, title, path, origin, transient) VALUES ('movie', 'x', ?, 'online', 1)",
                               (str(path),)).lastrowid
            conn.execute("INSERT INTO history(channel_id, media_id, started_at, ended_at) VALUES (1, ?, ?, ?)",
                         (mid, now - 30 * 86400, now - 30 * 86400))
    assert lineup.remove_aired_transients(conn, now=now) == 4
    assert not inside.exists() and not link.is_symlink()
    assert outside.exists() and target.exists()
    assert conn.execute("SELECT COUNT(*) FROM media WHERE missing = 0").fetchone()[0] == 0


def test_fetched_advert_without_a_verdict_follows_the_keyword_rule(conn, tmp_path):
    """"family_safe": null is no verdict: tags and PiTV's keyword list decide, as on import."""
    from pitv.content import apply_report
    rows = []
    for title, meta in (("Hofmeister", {"family_safe": None}), ("Milk Tray", {"family_safe": None}),
                        ("Cadbury's Flake", {"tags": ["alcohol"]}), ("Carling", {"family_safe": True})):
        with dbm.tx(conn):
            wid = conn.execute("INSERT INTO wanted(kind, title, year, provider, created_at) VALUES ('advert', ?, 1985, 'auto', 0)",
                               (title,)).lastrowid
        f = tmp_path / f"{title}.mp4"
        f.write_bytes(b"x" * 10)
        apply_report(conn, {"schema": 2, "items": [{"wanted_id": wid, "status": "done", "file": {"path": str(f)},
                                                    "meta": {"kind": "advert", "title": title, "uid": f"yt:{title}", **meta}}]})
        rows.append(wid)
    safe = {r["title"]: r["family_safe"] for r in conn.execute("SELECT title, family_safe FROM media WHERE uid LIKE 'yt:%'"
                                                                 " AND kind = 'advert'")}
    assert safe == {"Hofmeister": 0, "Milk Tray": 1, "Cadbury's Flake": 0, "Carling": 1}
    with dbm.tx(conn):
        conn.execute("DELETE FROM media WHERE uid LIKE 'yt:%' AND kind = 'advert'")
        conn.executemany("DELETE FROM wanted WHERE id = ?", [(w,) for w in rows])


def test_request_already_in_the_library_is_met_by_that_file(conn):
    """pitv_content reports a fetch as a duplicate of a NAS file with `existing_uid`: the request
    is bound to that file and done, with no attempt used; an unknown uid is a plain failure."""
    from pitv.content import apply_report
    film = conn.execute("SELECT id, uid, title FROM media WHERE kind = 'movie' AND missing = 0 AND origin = 'nas'"
                        " ORDER BY id LIMIT 1").fetchone()
    ch = conn.execute("SELECT id FROM channels ORDER BY number LIMIT 1").fetchone()["id"]
    start = 4102444800
    with dbm.tx(conn):
        known = conn.execute("INSERT INTO wanted(kind, title, provider, created_at) VALUES ('movie', ?, 'auto', 0)",
                             (film["title"],)).lastrowid
        unknown = conn.execute("INSERT INTO wanted(kind, title, provider, created_at) VALUES ('movie', 'Nowhere', 'auto', 0)").lastrowid
        conn.execute("INSERT INTO schedule(channel_id, day, start_ts, end_ts, media_id, kind, title, wanted_id)"
                     " VALUES (?, '2099-12-31', ?, ?, NULL, 'programme', ?, ?)", (ch, start, start + 5400, film["title"], known))
    counts = apply_report(conn, {"schema": 2, "items": [
        {"wanted_id": known, "status": "failed", "message": f"already in the library as {film['uid']}",
         "existing_uid": film["uid"], "file": None},
        {"wanted_id": unknown, "status": "failed", "message": "already in the library as nas:x:y",
         "existing_uid": "nas:x:y", "file": None}]})
    assert (counts["wanted_done"], counts["created"], counts["wanted_failed"]) == (1, 0, 1)
    row = conn.execute("SELECT status, attempts FROM wanted WHERE id = ?", (known,)).fetchone()
    assert (row["status"], row["attempts"]) == ("done", 0)
    assert conn.execute("SELECT attempts FROM wanted WHERE id = ?", (unknown,)).fetchone()[0] == 1
    assert conn.execute("SELECT media_id FROM schedule WHERE wanted_id = ?", (known,)).fetchone()[0] == film["id"]
    with dbm.tx(conn):
        conn.execute("DELETE FROM schedule WHERE day >= '2099-12-31'")
        conn.executemany("DELETE FROM wanted WHERE id = ?", [(known,), (unknown,)])


def test_old_report_files_are_pruned(conn, tmp_path):
    import os

    from pitv.content import apply_report_files
    from pitv.player.cache import MediaCache
    cache = MediaCache(tmp_path / "cache", 10 ** 9)
    d = cache.reports_dir
    d.mkdir(parents=True)
    old = dbm.now_ts() - 40 * 86400
    applied, marker, broken, orphan, recent = (d / "applied.json", d / "applied.json.applied", d / "broken.json",
                                               d / "gone.json.applied", d / "recent.json")
    for f, text in ((applied, "{}"), (marker, "1"), (broken, "not json"), (orphan, "1"), (recent, "not json")):
        f.write_text(text)
    for f in (applied, marker, broken):
        os.utime(f, (old, old))
    assert apply_report_files(conn, cache) == 0
    assert sorted(p.name for p in d.iterdir()) == ["recent.json"]   # unapplied and recent: retried next pass


def test_added_title_without_a_channel_goes_where_its_genres_fit(tmp_path):
    """The admin's Add to the catalogue may leave the channel to PiTV: a title lands on a channel
    that accepts its genres, as the generator would place it, and one no channel takes is refused."""
    ctx = make_library(tmp_path, max_episodes=1)
    conn = ctx["conn"]
    e = lineup.add(conn, None, title="Count Duckula", kind="show", year=1988, genres=["Animation"])
    chosen = next(c for c in lineup.programme_channels(conn) if c["id"] == e["channel_id"])
    assert lineup.channel_fit(chosen, {"animation"}) is not None
    with dbm.tx(conn):
        conn.execute("UPDATE channels SET excluded_genres = '[\"animation\"]'")
    with pytest.raises(ValueError):
        lineup.add(conn, None, title="Jamie and the Magic Torch", kind="show", genres=["Animation"])
    with pytest.raises(ValueError):
        lineup.add(conn, None, show_id=1)       # moving a library title needs a destination
    toons = conn.execute("SELECT id FROM channels WHERE content = 'cartoons'").fetchone()[0]
    child = lineup.add(conn, toons, title="Bod", kind="show", year=1975, genres=["Children"])
    assert lineup.facets(conn)["genres"]["Children"]["episode"] >= 1
    changed = lineup.update(conn, child["id"], {"genres": ["Children", "Animation"], "episode_minutes": 5})
    assert changed["genres"] == ["Children", "Animation"] and changed["episode_minutes"] == 5


def test_genres_are_one_spelling_everywhere(tmp_path):
    """Kids, Children's and cartoons are not three genres. Whatever a title is tagged with, it
    reaches the channel that allows that genre; a channel's own list is stored the same way, and
    rows written before the vocabulary existed are brought into line on the next start."""
    from pitv import genres
    from pitv.db import genre_list
    assert genre_list(["Sci-Fi", "kids", "cartoons", "Action/Adventure"]) == [
        "Science Fiction", "Children", "Animation", "Action", "Adventure"]
    assert genres.matches(["Kids"], genres.CHILDRENS) and not genres.matches(["Crime"], genres.CHILDRENS)

    ctx = make_library(tmp_path, max_episodes=2)
    conn = ctx["conn"]
    toons = conn.execute("SELECT id FROM channels WHERE content = 'cartoons'").fetchone()["id"]
    with dbm.tx(conn):     # a channel list as an older PiTV stored it
        conn.execute("UPDATE channels SET allowed_genres = ? WHERE id = ?",
                     (json.dumps(["animation", "cartoon", "kids"]), toons))
    dbm.init_db(conn)
    assert dbm.row_to_dict(conn.execute("SELECT allowed_genres FROM channels WHERE id = ?",
                                        (toons,)).fetchone())["allowed_genres"] == ["Animation", "Children"]
    added = lineup.add(conn, None, title="Jamie and the Magic Torch", kind="show", genres=["Kids"])
    assert added["channel_id"] == toons and added["genres"] == ["Children"]


def test_confirmed_identity_is_cleaned_and_survives_the_mirror(tmp_path):
    """Only a well-formed identity is kept, never a script URL, and it travels with the line-up
    document so a rebuilt database still fetches the confirmed title."""
    assert lineup.clean_match({"source": "tvmaze", "id": 7, "url": "javascript:alert(1)", "imdb": "nope"}) == \
        {"source": "tvmaze", "id": "7"}
    assert lineup.clean_match({"source": "Bad Source!", "id": "1"}) is None
    assert lineup.clean_match("tvmaze:1") is None
    ctx = make_library(tmp_path, max_episodes=1)
    conn = ctx["conn"]
    match = {"source": "tmdb", "id": "11", "url": "https://www.themoviedb.org/movie/11"}
    lineup.add(conn, None, title="A Film Not On The NAS", year=1983, kind="movie", genres=["Comedy"], match=match)
    doc = lineup.export(conn)
    with dbm.tx(conn):
        conn.execute("DELETE FROM lineup")
    lineup.import_doc(conn, doc)
    restored = next(e for e in lineup.entries(conn) if e["title"] == "A Film Not On The NAS")
    assert restored["match"] == match
