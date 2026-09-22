"""Channel line-ups: exclusive membership, genre-driven generation, external entries."""

import json
from datetime import timedelta
from itertools import pairwise

import pytest
from conftest import make_library

from pitv import db as dbm
from pitv import lineup
from pitv.genres import programme_type
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


def test_every_programme_belongs_to_exactly_one_channel(tmp_path):
    """Generation gives every series and film exactly one channel and leaves none homeless.

    On a library of its own. This is an invariant over the whole catalogue, and the database the
    rest of this module shares is one a dozen tests mutate: they retire episodes, add titles by
    hand and exclude material, each of which legitimately changes the count. Chasing those with
    ever more careful queries was measuring the neighbours rather than the rule."""
    conn = make_library(tmp_path, max_episodes=4)["conn"]
    shows = conn.execute("SELECT COUNT(*) FROM shows WHERE missing = 0 AND excluded = 0").fetchone()[0]
    movies = conn.execute("SELECT COUNT(*) FROM media WHERE kind = 'movie' AND missing = 0 AND excluded = 0").fetchone()[0]
    entries = conn.execute("SELECT kind, COUNT(*) AS n FROM lineup GROUP BY kind").fetchall()
    counts = {r["kind"]: r["n"] for r in entries}
    assert counts.get("show", 0) == shows and counts.get("movie", 0) == movies
    assert not conn.execute("SELECT show_id FROM lineup WHERE show_id IS NOT NULL GROUP BY show_id HAVING COUNT(*) > 1").fetchall()
    assert not conn.execute("SELECT 1 FROM shows WHERE missing = 0 AND excluded = 0 AND home_channel_id IS NULL").fetchall()
    assert not conn.execute("SELECT 1 FROM media WHERE kind = 'movie' AND missing = 0 AND home_channel_id IS NULL").fetchall()
    conn.close()


def test_generation_respects_channel_genres(conn):
    channels = {r["id"]: dbm.row_to_dict(r) for r in conn.execute("SELECT * FROM channels")}
    # What `generate` placed, which is what this is about: an entry added by hand goes where it
    # was put, and several tests here add one.
    for e in [x for x in lineup.entries(conn) if x.get("source", "library") == "library"]:
        ch = channels[e["channel_id"]]
        assert ch["content"] in ("general", "cartoons")
        genres = {g.lower() for g in (e["genres"] or [])}
        # No sports channel here, so nothing claims sport and it stays general; cartoons are claimed.
        ptype = "sport" if e.get("category") == "sport" else programme_type(e["kind"], e["genres"] or [])
        assert lineup.channel_fit(ch, genres, ptype, claimed=frozenset({"cartoon"})) is not None, (e["title"], ptype, genres)
        assert (ptype == "cartoon") == (ch["content"] == "cartoons"), (e["title"], ptype, ch["name"])
    toons = conn.execute("SELECT id FROM channels WHERE content = 'cartoons'").fetchone()["id"]
    titles = {e["title"] for e in lineup.entries(conn, channel_id=toons)}
    assert {"Danger Mouse", "Thundercats"} <= titles


def test_week_never_shares_a_programme_across_channels(conn):
    """A programme airs on the channel whose line-up holds it. The one exception is a type a
    channel lists under "also carries": by default the general channels borrow cartoons, and
    nothing else crosses."""
    now = local_ts(parse_day("2026-09-14"), "07:00", tz_of(conn))
    _nas_only(conn, True)   # a remote placeholder has no media row and nothing to share
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=7, now=now, seed=5, force=True)
    away = conn.execute(
        "SELECT s.channel_id, c.also_carries, COALESCE(sh.title, m.title) AS title, COALESCE(sh.genres, m.genres) AS genres, m.kind"
        " FROM schedule s JOIN media m ON m.id = s.media_id LEFT JOIN shows sh ON sh.id = m.show_id"
        " JOIN channels c ON c.id = s.channel_id WHERE s.kind = 'programme' AND m.kind IN ('episode','movie')"
        " AND s.channel_id != COALESCE(sh.home_channel_id, m.home_channel_id)").fetchall()
    for r in away:
        ptype = programme_type("show" if r["kind"] == "episode" else "movie", json.loads(r["genres"] or "[]"))
        assert ptype in json.loads(r["also_carries"] or "[]"), (r["title"], ptype, r["channel_id"])
    with dbm.tx(conn):
        conn.execute("UPDATE channels SET also_carries = '[]'")
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=7, now=now, seed=5, force=True)
    homes = conn.execute(
        "SELECT COUNT(*) FROM schedule s JOIN media m ON m.id = s.media_id LEFT JOIN shows sh ON sh.id = m.show_id"
        " WHERE s.kind = 'programme' AND m.kind IN ('episode','movie') AND s.start_ts > ?"
        " AND s.channel_id != COALESCE(sh.home_channel_id, m.home_channel_id)", (now,)).fetchone()[0]
    assert homes == 0, "with nothing borrowed, nothing crosses"
    with dbm.tx(conn):
        conn.execute("UPDATE channels SET also_carries = '[\"cartoon\"]' WHERE content = 'general'")


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


def test_an_unscheduled_request_is_ranked_by_the_gap_it_would_fill(tmp_path):
    """Nothing in the manifest's `wanted` list has an air time of its own, so it is judged by the
    soonest holding card it could fill. Ranking by what raised a request goes stale, because "a
    line-up raised it" becomes true of every channel eventually; a card on screen at eight
    tomorrow does not. A gap in a series that already plays is waited on by nothing and goes
    last, however long it has been queued."""
    from pitv import lineup as lineup_mod, wanted as wanted_mod
    from pitv.content import NOT_WAITED_ON, manifest
    from conftest import make_library

    ctx = make_library(tmp_path / "urgency", max_episodes=2)
    conn = ctx["conn"]
    channel = conn.execute("SELECT id FROM channels WHERE content = 'music'").fetchone()["id"]
    now = dbm.now_ts()
    with dbm.tx(conn):
        conn.execute("DELETE FROM band WHERE channel_id = ?", (channel,))
        for name, start, minutes in (("Soon", "20:00", 60), ("Later", "22:00", 60)):
            conn.execute("INSERT INTO band(channel_id, name, start, minutes, days, fill, enabled, created_at)"
                         " VALUES (?,?,?,?,'[]',?,1,?)",
                         (channel, name, start, minutes,
                          json.dumps({"kinds": ["episode"], "genres": [name], "decades": [], "feature": False}),
                          now))
    for name, at in (("Soon", now + 3 * 3600), ("Later", now + 40 * 3600)):
        entry = lineup_mod.add(conn, channel, title=f"{name} Source", kind="show", genres=[name],
                               source="catalogue", match={"source": "elsewhere", "id": name.lower()})
        with dbm.tx(conn):
            conn.execute("INSERT INTO schedule(channel_id, day, start_ts, end_ts, kind, title, block, replay, offset)"
                         " VALUES (?,?,?,?,'filler',?,?,0,0)",
                         (channel, "2026-09-23", at, at + 3600, name, name))
            conn.execute("INSERT INTO wanted(kind, title, season, episode, lineup_id, auto, created_at, provider)"
                         " VALUES ('episode',?,1,1,?,1,?,'auto')", (f"{name} Source", entry["id"], now))
    with dbm.tx(conn):    # a gap nothing is waiting for, queued long before either
        conn.execute("INSERT INTO wanted(kind, title, season, episode, show_id, auto, created_at, provider)"
                     " SELECT 'episode', title, 1, 99, id, 1, ?, 'auto' FROM shows LIMIT 1", (now - 86400,))

    by_title = {w["show_title"] or w["title"]: w for w in manifest(conn, days=1, now=now)["wanted"]}
    assert by_title["Soon Source"]["priority"] < by_title["Later Source"]["priority"], \
        "the card that is sooner is wanted sooner"
    gap = next(w for k, w in by_title.items() if k not in ("Soon Source", "Later Source"))
    assert gap["priority"] == NOT_WAITED_ON, "nothing is waiting for it, whenever it was asked for"
    conn.close()


def test_external_entry_scheduled_ahead_and_requested(conn):
    ch = conn.execute("SELECT id FROM channels WHERE content = 'general' ORDER BY number LIMIT 1").fetchone()["id"]
    confirmed = {"source": "tvmaze", "id": "2203", "url": "https://www.tvmaze.com/shows/2203/the-tripods", "imdb": "tt0086817"}
    # A title explicitly pinned to this channel remains eligible even when its year lies
    # outside the automatic global era mix (the fixture defaults to the 1980s/1990s).
    entry = lineup.add(conn, ch, title="The Tripods", year=1974, kind="show", genres=["Science Fiction"], episode_minutes=25,
                       match={**confirmed, "poster": "ignored"})
    assert entry["match"] == confirmed
    # Requests this entry raised in an earlier build, whether this test's or a neighbour's: the
    # entry is keyed on its title, so it is the same entry each time and its requests outlive
    # the schedule that raised them. This counts the ones its own build makes.
    with dbm.tx(conn):
        conn.execute("DELETE FROM wanted WHERE lineup_id = ?", (entry["id"],))
    now = local_ts(parse_day("2026-09-14"), "07:00", tz_of(conn))
    # NAS-only: nothing not on disk is scheduled. Set here rather than assumed, because another
    # test needs it off and the database is shared.
    _nas_only(conn, True)
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=4, now=now, seed=9, force=True)
    assert conn.execute("SELECT COUNT(*) FROM schedule s JOIN wanted w ON w.id = s.wanted_id"
                        " WHERE w.lineup_id = ?", (entry["id"],)).fetchone()[0] == 0
    ceiling = dbm.get_setting(conn, "external_new_per_day")
    with dbm.tx(conn):
        dbm.set_setting(conn, "nas_only", False)
        dbm.set_setting(conn, "external_weight", 50.0)  # make the entry win often in the test library
        # Every other test in this module leaves its own remote titles in the shared database, and
        # by the end of a run they far outnumber the few new ones a day the schedule will promise.
        # Which of them wins that handful is not what is under test, so the ceiling is lifted here.
        dbm.set_setting(conn, "external_new_per_day", 40)
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=4, now=now, seed=9, force=True)
    # This entry's own slots. Other tests in this module add remote titles of their own, and
    # every one of them raises requests that look exactly like these.
    slots = conn.execute("SELECT s.*, w.episode, w.transient FROM schedule s JOIN wanted w ON w.id = s.wanted_id"
                         " WHERE s.replay = 0 AND w.lineup_id = ? ORDER BY s.start_ts", (entry["id"],)).fetchall()
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
        dbm.set_setting(conn, "external_new_per_day", ceiling)


REMOTE_TITLES = ("Remote History One", "Remote History Two", "Remote History Three")


def _schedule_remote_titles(conn) -> int:
    """Three remote series on the first general channel and three days built around them. The
    module shares one database, so this adds only what is not there yet, and clears any other
    remote entry on the channel first: how many remote titles a day may carry is capped, so
    entries left by a neighbour would compete for the same few slots and crowd these out."""
    ch = conn.execute("SELECT id FROM channels WHERE content = 'general' ORDER BY number LIMIT 1").fetchone()["id"]
    with dbm.tx(conn):
        conn.execute("DELETE FROM lineup WHERE channel_id = ? AND source != 'library' AND title NOT IN"
                     f" ({','.join('?' * len(REMOTE_TITLES))})", (ch, *REMOTE_TITLES))
    have = {r["title"] for r in conn.execute("SELECT title FROM lineup WHERE channel_id = ?", (ch,))}
    for n, title in enumerate(REMOTE_TITLES, 1):
        if title not in have:
            lineup.add(conn, ch, title=title, year=1985, kind="show", genres=["Documentary"],
                       episode_minutes=25, match={"source": "manual", "id": f"remote-{n}"})
    with dbm.tx(conn):
        dbm.set_setting(conn, "nas_only", False)
        dbm.set_setting(conn, "external_weight", 1.0)
    now = local_ts(parse_day("2026-09-14"), "07:00", tz_of(conn))
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=3, now=now, seed=17, force=True)
    return ch


def _nas_only(conn, on: bool) -> None:
    """Whether anything not on disk may be scheduled. Tests that need it either way set it
    themselves: leaving it changed was how a helper here made its neighbours fail."""
    with dbm.tx(conn):
        dbm.set_setting(conn, "nas_only", on)


def test_every_unseen_remote_title_gets_a_variety_slot(conn):
    """A healthy local library must not starve configured remote catalogue entries."""
    ch = _schedule_remote_titles(conn)
    titles = REMOTE_TITLES
    scheduled = {r["title"] for r in conn.execute(
        "SELECT DISTINCT s.title FROM schedule s JOIN wanted w ON w.id = s.wanted_id"
        " WHERE s.channel_id = ? AND s.replay = 0", (ch,))}
    assert set(titles) <= scheduled


def test_readiness_substitutes_unfetched_placeholders(conn):
    from pitv.readiness import check
    _schedule_remote_titles(conn)
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
        # A second airing of the same request two days on, marked as a repeat and sized, like the
    # first, from the nominal length.
        conn.execute("INSERT INTO schedule(channel_id, day, start_ts, end_ts, media_id, offset, kind, replay, title, subtitle, wanted_id)"
                     " VALUES (?, '2100-01-02', ?, ?, NULL, 0, 'programme', 1, 'Pending Film', '', ?)",
                     (ch, start + 2 * 86400, start + 2 * 86400 + 90 * 60, wid))
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
    slots = conn.execute("SELECT media_id, end_ts, start_ts FROM schedule WHERE wanted_id = ? ORDER BY start_ts", (wid,)).fetchall()
    assert len(slots) == 2, "the first airing and its repeat"
    for slot in slots:
        # The repeat too: left at its nominal length, a shorter file ends and a card runs on.
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
    assert chosen["content"] == "cartoons", "an animated series is a cartoon and goes to the cartoon channel"
    # Put on the cartoon channel by hand with tags too thin to say so, it is a cartoon all the same.
    by_hand = lineup.add(conn, chosen["id"], title="Mr. Benn", kind="show", year=1971, genres=["Children"])
    assert by_hand["programme_type"] == "cartoon"
    drama = lineup.add(conn, None, title="Edge of Darkness", kind="show", year=1985, genres=["Crime", "Drama"])
    assert next(c for c in lineup.programme_channels(conn) if c["id"] == drama["channel_id"])["content"] == "general"
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
    # "Kids" says who it is for, not what it is; the owner says it is a cartoon when adding it.
    added = lineup.add(conn, None, title="Jamie and the Magic Torch", kind="show", genres=["Kids"], programme_type="cartoon")
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


def test_the_manifest_never_asks_again_for_what_a_band_collection_found(tmp_path):
    """A scheduled row whose only copy has gone is requested again only when a request stands
    behind it. A band collection's row carries pitv_content's own guess at title and year; sent
    back as a request it fetches some other upload under the same guess, every night."""
    from pitv.content import manifest
    c = make_library(tmp_path, max_episodes=4)["conn"]
    now = local_ts(parse_day("2026-09-14"), "07:00", tz_of(c))
    build_horizon(c, start_day=parse_day("2026-09-14"), days=1, now=now, seed=2)
    mid = c.execute("SELECT s.media_id FROM schedule s JOIN media m ON m.id = s.media_id WHERE m.kind = 'music'"
                    " AND s.start_ts > ? ORDER BY s.start_ts LIMIT 1", (now,)).fetchone()[0]
    with dbm.tx(c):
        c.execute("UPDATE media SET origin = 'cache', path = '/nowhere/gone (1960).mp4', cache_path = NULL WHERE id = ?", (mid,))
    assert not [i for i in manifest(c, days=1, now=now)["items"] if i["media_id"] == mid]
    with dbm.tx(c):
        c.execute("INSERT INTO wanted(kind, title, status, dest_path, created_at)"
                  " VALUES ('music', 'Asked for', 'done', '/nowhere/gone (1960).mp4', ?)", (now,))
    again = [i for i in manifest(c, days=1, now=now)["items"] if i["media_id"] == mid]
    assert len(again) == 1 and again[0]["action"] == "fetch"
    c.close()


def test_a_story_about_sport_is_not_scheduled_as_sport():
    """Sport changes the dayparts a series may air in and lets it run back to back at weekends.
    A drama or comedy tagged Sport among its genres is none of that; the sports share is sport
    whatever its files are tagged."""
    from pitv import genres
    assert genres.scheduling_class("general", ["Sport", "Snooker"]) == "sport"
    assert genres.scheduling_class("general", ["Documentary", "Sport"]) == "sport"
    assert genres.scheduling_class("general", ["Drama", "Comedy", "Sport"]) == "general"
    assert genres.scheduling_class("general", ["Action", "Sci-Fi", "Sport", "Thriller"]) == "general"
    assert genres.scheduling_class("sport", ["Drama"]) == "sport"


def test_fetched_material_is_kept_until_the_drive_needs_room_then_oldest_aired_goes_first(tmp_path):
    """Fetched episodes stay after airing so a later airing costs nothing. When the cache folder
    is over its cap the ones that aired longest ago go first, with a warning; nothing scheduled
    ahead, nothing that has not aired and nothing outside the cache is touched."""
    c = make_library(tmp_path, max_episodes=2)["conn"]
    cache = tmp_path / "cache"
    (cache / "acquired").mkdir(parents=True)
    now = dbm.now_ts()
    with dbm.tx(c):
        dbm.set_setting(c, "cache_dir", str(cache))
        dbm.set_setting(c, "cache_max_gb", 0.000002)             # about two kilobytes
        ids = [r[0] for r in c.execute("SELECT id FROM media WHERE kind = 'episode' ORDER BY id LIMIT 4")]
        for n, mid in enumerate(ids):
            f = cache / "acquired" / f"ep{n}.mp4"
            f.write_bytes(b"x" * 1000)
            c.execute("UPDATE media SET origin = 'online', path = ?, cache_path = NULL WHERE id = ?", (str(f), mid))
        # 0 aired long ago, 1 aired yesterday, 2 has not aired, 3 aired but is scheduled again
        for mid, ago in ((ids[0], 30), (ids[1], 1), (ids[3], 20)):
            c.execute("INSERT INTO history(channel_id, media_id, started_at, ended_at, title) VALUES (1, ?, ?, ?, 'x')",
                      (mid, now - ago * 86400 - 1800, now - ago * 86400))
        c.execute("DELETE FROM schedule")
        c.execute("INSERT INTO schedule(channel_id, day, start_ts, end_ts, media_id, offset, kind, title)"
                  " VALUES (1, '2030-01-01', ?, ?, ?, 0, 'programme', 'again')", (now + 3600, now + 5400, ids[3]))
    assert lineup.evict_fetched(c, now=now) == 2, "4 KB held against a 2 KB cap: the two that have aired and are not due again"
    left = {r["id"]: r["missing"] for r in c.execute(f"SELECT id, missing FROM media WHERE id IN ({','.join('?' * 4)})", ids)}
    assert left == {ids[0]: 1, ids[1]: 1, ids[2]: 0, ids[3]: 0}
    assert not (cache / "acquired" / "ep0.mp4").exists() and (cache / "acquired" / "ep2.mp4").exists()
    assert lineup.evict_fetched(c, now=now) == 0, "nothing else has aired that may go"
    c.close()


def test_a_fetched_file_that_cost_an_encode_outlasts_one_filed_as_found(tmp_path):
    """Both have aired and either would make the room. The one pitv_content re-encoded aired
    longer ago, and still stays: it is the dearer of the two to get back."""
    c = make_library(tmp_path, max_episodes=2)["conn"]
    cache = tmp_path / "cache"
    (cache / "acquired").mkdir(parents=True)
    now = dbm.now_ts()
    with dbm.tx(c):
        dbm.set_setting(c, "cache_dir", str(cache))
        dbm.set_setting(c, "cache_max_gb", 0.0000015)            # room for one of the two
        ids = [r[0] for r in c.execute("SELECT id FROM media WHERE kind = 'episode' ORDER BY id LIMIT 2")]
        for n, (mid, encoded, ago) in enumerate(zip(ids, (1, 0), (30, 1), strict=True)):
            f = cache / "acquired" / f"ep{n}.mp4"
            f.write_bytes(b"x" * 1000)
            c.execute("UPDATE media SET origin = 'cache', path = ?, cache_path = ?, encoded = ? WHERE id = ?",
                      (str(f), str(f), encoded, mid))
            c.execute("INSERT INTO history(channel_id, media_id, started_at, ended_at, title) VALUES (1, ?, ?, ?, 'x')",
                      (mid, now - ago * 86400 - 1800, now - ago * 86400))
        c.execute("DELETE FROM schedule")
    assert lineup.evict_fetched(c, now=now) == 1
    assert (cache / "acquired" / "ep0.mp4").exists() and not (cache / "acquired" / "ep1.mp4").exists()
    c.close()


def test_the_schedule_promises_no_more_new_remote_material_a_day_than_the_ceiling(conn):
    """However many remote titles a channel has, a day commits to at most `external_new_per_day`
    placeholders across all channels; the rest of the day comes from the library."""
    channels = [r["id"] for r in conn.execute("SELECT id FROM channels WHERE content = 'general' ORDER BY number")]
    for n, ch in enumerate(channels * 3):
        lineup.add(conn, ch, title=f"Remote Series {n}", year=1984, kind="show", genres=["Drama"], episode_minutes=25)
    now = local_ts(parse_day("2026-09-14"), "07:00", tz_of(conn))
    with dbm.tx(conn):
        dbm.set_setting(conn, "nas_only", False)
        dbm.set_setting(conn, "external_weight", 50.0)
        dbm.set_setting(conn, "external_new_per_day", 2)
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=4, now=now, seed=5, force=True)
    per_day = conn.execute("SELECT day, COUNT(*) AS n FROM schedule WHERE media_id IS NULL AND wanted_id IS NOT NULL"
                           " AND replay = 0 GROUP BY day").fetchall()
    assert per_day and all(r["n"] <= 2 for r in per_day), [tuple(r) for r in per_day]
    assert not conn.execute("SELECT 1 FROM schedule WHERE kind = 'filler' AND replay = 0 AND block IS NULL"
                            " AND end_ts - start_ts > 300").fetchone(), "the library fills what the ceiling holds back"


def test_a_starved_channel_does_not_book_one_remote_episode_all_day():
    """With nothing on disk and a single remote series, the last step before a holding card may
    bring the series round again, but only up to the daily cap; the rest of the day is the
    holding card. Uncapped, one unfetched episode was booked four times in a day and four more
    in the small hours' replay of it. The replay is left out here: with one title in the day it
    has nothing else to loop."""
    c = dbm.connect(":memory:")
    dbm.init_db(c)
    ch = c.execute("SELECT id FROM channels WHERE content = 'general' ORDER BY number LIMIT 1").fetchone()["id"]
    lineup.add(c, ch, title="Bullseye", year=1981, kind="show", genres=["Game Show"], episode_minutes=30)
    with dbm.tx(c):
        dbm.set_setting(c, "nas_only", False)
    now = local_ts(parse_day("2026-09-14"), "07:00", tz_of(c))
    build_horizon(c, start_day=parse_day("2026-09-14"), days=4, now=now, seed=5, force=True)
    cap = dbm.all_settings(c)["show_daily_limit"]
    placed = c.execute("SELECT day, wanted_id, COUNT(*) AS n FROM schedule WHERE channel_id = ? AND wanted_id IS NOT NULL"
                       " AND CAST(strftime('%H', start_ts, 'unixepoch', 'localtime') AS INTEGER) >= 8"
                       " GROUP BY day, wanted_id", (ch,)).fetchall()
    assert placed, "the remote series was never placed"
    assert max(r["n"] for r in placed) <= cap, [tuple(r) for r in placed]
    c.close()


def test_what_a_programme_is_decides_its_channel_not_its_genres():
    """A documentary channel whose subjects include Crime and History once took Breaking Bad and
    RoboCop, because one matching genre was enough. Membership of a themed channel is by type;
    genres steer only among the general channels; a type no themed channel claims stays general;
    and the owner's word on what a title is outranks its tags."""
    from pitv.lineup import channel_fit, claimed_types
    assert programme_type("show", ["Crime", "Drama", "Thriller"]) == "series"
    assert programme_type("movie", ["Action", "Crime", "Science Fiction"]) == "film"
    assert programme_type("show", ["Documentary", "History", "Music"]) == "documentary"
    assert programme_type("movie", ["Documentary", "Animation"]) == "documentary", "a documentary about cartoons"
    assert programme_type("show", ["Animation", "Comedy"]) == "cartoon"
    assert programme_type("show", ["Darts"], "sport") == "sport"
    assert programme_type("show", ["Documentary", "Sport", "Motorsport"]) == "documentary", "a film about racing"
    assert programme_type("show", ["Documentary"], "sport") == "sport", "the sports share is sport whatever its tags"
    assert programme_type("show", ["Sport", "Comedy", "Drama"]) == "series", "a story about sport is not sport"
    assert programme_type("music", []) == "music"
    assert programme_type("show", ["History"], None, "documentary") == "documentary", "the owner's word wins"
    assert programme_type("show", ["History"], None, "nonsense") == "series"

    docs = {"content": "documentaries", "allowed_genres": ["Crime", "History", "Documentary"]}
    toons = {"content": "cartoons"}
    one = {"content": "general", "allowed_genres": ["Drama", "Crime", "Sport"]}
    two = {"content": "general", "allowed_genres": ["Comedy"]}
    kids = {"content": "kids"}
    claimed = claimed_types([docs, toons, one, two, kids])
    assert claimed == {"documentary", "cartoon"}
    fit = lambda ch, genres, ptype, **kw: channel_fit(ch, {g.lower() for g in genres}, ptype, claimed=claimed, **kw)
    assert fit(docs, ["Crime", "Drama"], "series") is None and fit(one, ["Crime", "Drama"], "series")
    assert fit(docs, ["Documentary", "Music"], "documentary") == 1.0
    assert fit(one, ["Documentary", "Crime"], "documentary") is None, "claimed by the documentary channel"
    assert fit(toons, ["Animation"], "cartoon") == 1.0 and fit(one, ["Animation", "Crime"], "cartoon") is None
    assert fit(one, ["Sport"], "sport") and fit(docs, ["Sport"], "sport") is None, "no sports channel: sport stays general"
    assert fit(two, ["Crime", "Drama"], "series") is None and fit(two, ["Comedy"], "film"), "genres steer among general channels"
    assert fit(kids, ["Comedy"], "series", kids=True) == 1.0 and fit(kids, ["Comedy"], "series") is None
    assert fit({**docs, "excluded_genres": ["Music"]}, ["Documentary", "Music"], "documentary") is None
    # A channel's decades bar what the scheduler would never air there; a series that ran into a
    # listed decade counts, and an unknown year is not held against a title.
    eighties = {**one, "decades": [1970, 1980]}
    assert fit(eighties, ["Drama"], "series", year=1985) and fit(eighties, ["Drama"], "series", year=None)
    assert fit(eighties, ["Drama"], "series", year=2007) is None
    assert fit(eighties, ["Drama"], "series", year=1968, end_year=1972), "it ran into the seventies"


def test_a_retyped_title_moves_to_the_channel_that_takes_it(conn):
    """The owner says a series its tags call a drama is a cartoon: it leaves its general channel
    for the cartoon one, the admin's view of it says so, and taking the word back returns it."""
    from pitv.web.api.deps import show_public
    themed = conn.execute("SELECT id FROM channels WHERE content = 'cartoons'").fetchone()
    show = conn.execute("SELECT s.* FROM shows s JOIN lineup l ON l.show_id = s.id JOIN channels c ON c.id = l.channel_id"
                        " WHERE c.content = 'general' AND s.category != 'sport' LIMIT 1").fetchone()
    assert show_public(show)["programme_type"] == "series" and not show_public(show)["programme_type_set"]
    before = json.loads(show["overrides"] or "{}")
    with dbm.tx(conn):
        conn.execute("UPDATE shows SET overrides = ? WHERE id = ?", (json.dumps({**before, "programme_type": "cartoon"}), show["id"]))
    lineup.place_again(conn, show_id=show["id"])
    assert conn.execute("SELECT channel_id FROM lineup WHERE show_id = ?", (show["id"],)).fetchone()[0] == themed["id"]
    after = conn.execute("SELECT * FROM shows WHERE id = ?", (show["id"],)).fetchone()
    assert show_public(after)["programme_type"] == "cartoon" and show_public(after)["programme_type_set"]
    with dbm.tx(conn):
        conn.execute("UPDATE shows SET overrides = ? WHERE id = ?", (json.dumps(before), show["id"]))
    lineup.place_again(conn, show_id=show["id"])
    assert conn.execute("SELECT c.content FROM lineup l JOIN channels c ON c.id = l.channel_id WHERE l.show_id = ?",
                        (show["id"],)).fetchone()[0] == "general"


def test_the_generator_reads_a_titles_type_from_the_index_genres(tmp_path):
    """Genres arrive from the index as a JSON column. Read as text they named no type, and a
    documentary film whose genres nobody had since edited was placed as an ordinary film."""
    ctx = make_library(tmp_path, max_episodes=1)
    conn = ctx["conn"]
    with dbm.tx(conn):
        docs = conn.execute("INSERT INTO channels(number, name, short_name, pattern, content) VALUES (9, 'Docs', 'Docs', 'show', 'documentaries')").lastrowid
        film = conn.execute("SELECT id FROM media WHERE kind = 'movie' AND missing = 0 LIMIT 1").fetchone()["id"]
        conn.execute("UPDATE media SET genres = ?, enriched = '{}', overrides = '{}' WHERE id = ?", (json.dumps(["Documentary", "Biography"]), film))
    lineup.generate(conn, rebalance=True)
    assert conn.execute("SELECT channel_id FROM lineup WHERE media_id = ?", (film,)).fetchone()[0] == docs
    others = conn.execute("SELECT COUNT(*) FROM lineup WHERE channel_id = ? AND media_id != ?", (docs, film)).fetchone()[0]
    assert others == 0, "nothing that is not a documentary joins it"


def test_a_remote_series_is_asked_for_from_its_first_episode_and_not_past_its_last():
    """Requests once continued from the highest number ever raised, so after a week of rebuilds a
    three-part series was asked for episodes 10 and 11 while 1 and 2 went unasked. A new request
    takes the lowest number not yet asked for, and a series whose length is known is never asked
    for an episode past it, however many times the week is rebuilt."""
    c = dbm.connect(":memory:")
    dbm.init_db(c)
    ch = c.execute("SELECT id FROM channels WHERE content = 'general' ORDER BY number LIMIT 1").fetchone()["id"]
    entry = lineup.add(c, ch, title="Science Britannica", year=2013, kind="show", genres=["Documentary"],
                       episode_minutes=60, episode_count=3)
    assert entry["episode_count"] == 3
    with dbm.tx(c):
        dbm.set_setting(c, "nas_only", False)
        c.execute("UPDATE channels SET series_cadence_days = 1 WHERE id = ?", (ch,))   # room for the whole run in a week
        # What the old numbering left behind: a high episode delivered, the first never asked for.
        c.execute("INSERT INTO wanted(kind, title, season, episode, provider, lineup_id, status, created_at)"
                  " VALUES ('episode', 'Episode 2', 1, 2, 'auto', ?, 'done', 0)", (entry["id"],))
    now = local_ts(parse_day("2026-09-14"), "07:00", tz_of(c))
    for seed in (1, 2, 3):
        build_horizon(c, start_day=parse_day("2026-09-14"), days=7, now=now, seed=seed, force=True)
    asked = sorted(r[0] for r in c.execute("SELECT episode FROM wanted WHERE lineup_id = ?", (entry["id"],)))
    assert asked == [1, 2, 3], asked

    # An open request the old numbering left far into a run comes down to the front of it at the
    # next rebuild, and pitv_content saying where a run ends is remembered.
    from pitv.content import apply_report
    other = lineup.add(c, ch, title="Ancient Aliens", year=2009, kind="show", genres=["Documentary"], episode_minutes=45)
    build_horizon(c, start_day=parse_day("2026-09-14"), days=7, now=now, seed=4, force=True)
    with dbm.tx(c):
        c.execute("UPDATE wanted SET episode = episode + 7, title = 'Episode ' || (episode + 7) WHERE lineup_id = ?", (other["id"],))
    build_horizon(c, start_day=parse_day("2026-09-14"), days=7, now=now, seed=4, force=True)
    numbers = sorted(r[0] for r in c.execute("SELECT episode FROM wanted WHERE lineup_id = ?", (other["id"],)))
    assert numbers and numbers == list(range(1, len(numbers) + 1)), numbers
    titles = {r[0] for r in c.execute("SELECT s.subtitle FROM schedule s JOIN wanted w ON w.id = s.wanted_id WHERE w.lineup_id = ?"
                                      " AND s.subtitle != 'Episode ' || w.episode", (other["id"],))}
    assert not titles, f"slots still titled by the old numbers: {titles}"
    last = c.execute("SELECT id, episode FROM wanted WHERE lineup_id = ? ORDER BY episode DESC LIMIT 1", (other["id"],)).fetchone()
    apply_report(c, {"schema": 2, "items": [{"request_id": f"w:{last['id']}", "wanted_id": last["id"], "status": "failed",
                                             "message": f"no such episode: the series has {last['episode'] - 1}", "file": None}]})
    assert lineup.entry(c, other["id"])["episode_count"] == last["episode"] - 1
    apply_report(c, {"schema": 2, "items": [{"request_id": f"w:{last['id']}", "wanted_id": last["id"], "status": "failed",
                                             "message": "no such episode", "meta": {"episodes_total": 5}, "file": None}]})
    assert lineup.entry(c, other["id"])["episode_count"] == 5, "the structured count is read too"
    waiting = c.execute("SELECT id, attempts FROM wanted WHERE lineup_id = ? AND status = 'queued' LIMIT 1", (other["id"],)).fetchone()
    apply_report(c, {"schema": 2, "items": [{"request_id": f"w:{waiting['id']}", "wanted_id": waiting["id"], "status": "failed",
                                             "message": "not found yet: no upload says it is this episode", "file": None}]})
    after = c.execute("SELECT status, attempts FROM wanted WHERE id = ?", (waiting["id"],)).fetchone()
    assert (after["status"], after["attempts"]) == ("queued", waiting["attempts"]), "asked again, no attempt used"
    assert c.execute("SELECT status FROM wanted WHERE id = ?", (last["id"],)).fetchone()[0] == "failed", "not asked again"
    c.close()


def test_a_remote_title_keeps_its_match_certificate_and_obeys_it():
    """A series nobody holds yet is unrated, and an unrated series is treated as PG: "The Benny
    Hill Show" was scheduled at 08:30. The entry keeps the certificate of its online match, an
    Adult tag stands in for a missing one, and the scheduler holds both to the watershed."""
    c = dbm.connect(":memory:")
    dbm.init_db(c)
    ch = c.execute("SELECT id FROM channels WHERE content = 'general' ORDER BY number LIMIT 1").fetchone()["id"]
    rated = lineup.add(c, ch, title="The Young Ones", year=1982, kind="show", genres=["Comedy"], episode_minutes=35, certificate="UK:15")
    tagged = lineup.add(c, ch, title="The Benny Hill Show", year=1969, kind="show", genres=["Comedy", "Adult"], episode_minutes=60)
    assert rated["certificate"] == "15" and tagged["certificate"] is None
    with dbm.tx(c):
        dbm.set_setting(c, "nas_only", False)
        c.execute("UPDATE channels SET series_cadence_days = 1 WHERE id = ?", (ch,))
    now = local_ts(parse_day("2026-09-14"), "07:00", tz_of(c))
    build_horizon(c, start_day=parse_day("2026-09-14"), days=5, now=now, seed=8, force=True)
    slots = c.execute("SELECT s.title, CAST(strftime('%H', s.start_ts, 'unixepoch', 'localtime') AS INTEGER) AS hour FROM schedule s"
                      " JOIN wanted w ON w.id = s.wanted_id WHERE s.channel_id = ? AND s.replay = 0", (ch,)).fetchall()
    assert {r["title"] for r in slots} == {"The Young Ones", "The Benny Hill Show"}
    assert all(r["hour"] >= 21 or r["hour"] < 6 for r in slots), [(r["title"], r["hour"]) for r in slots]
    c.close()


def test_an_episode_already_on_disk_is_not_asked_for_again(tmp_path):
    """A remote series linked to a show that holds some of its first season asks only for the
    rest: an episode that arrived under another request, or was renamed to its real number, is
    there all the same."""
    ctx = make_library(tmp_path, max_episodes=3)
    conn = ctx["conn"]
    show = conn.execute("SELECT sh.id, sh.title, sh.year, l.channel_id FROM shows sh JOIN lineup l ON l.show_id = sh.id"
                        " JOIN channels c ON c.id = l.channel_id WHERE c.content = 'general' AND sh.category != 'sport' LIMIT 1").fetchone()
    held = {r[0] for r in conn.execute("SELECT episode FROM media WHERE show_id = ? AND COALESCE(season, 1) = 1 AND missing = 0", (show["id"],))}
    assert held
    with dbm.tx(conn):
        dbm.set_setting(conn, "nas_only", False)
        conn.execute("UPDATE channels SET series_cadence_days = 1 WHERE id = ?", (show["channel_id"],))
        conn.execute("UPDATE lineup SET source = 'manual', transient = 0, episode_count = ? WHERE show_id = ?", (max(held) + 2, show["id"]))
    now = local_ts(parse_day("2026-09-14"), "07:00", tz_of(conn))
    build_horizon(conn, start_day=parse_day("2026-09-14"), days=6, now=now, seed=6, force=True)
    asked = {r[0] for r in conn.execute("SELECT w.episode FROM wanted w JOIN lineup l ON l.id = w.lineup_id WHERE l.show_id = ?", (show["id"],))}
    assert asked and not asked & held, (asked, held)
    assert asked <= {max(held) + 1, max(held) + 2}


def test_a_matched_series_episode_is_filed_under_its_real_number_and_title(conn, tmp_path):
    """PiTV asks for "episode 14" of a matched series; pitv_content reports what that really is
    (S02E11 "Break Step Bridge"). The catalogue takes the real season, number and title, the
    request keeps its place in the run, and an unmatched series is still filed as asked."""
    from pitv.content import apply_report
    ch = conn.execute("SELECT id FROM channels WHERE content = 'general' ORDER BY number LIMIT 1").fetchone()["id"]
    matched = lineup.add(conn, ch, title="MythBusters Test", year=2003, kind="show", genres=["Documentary"], episode_minutes=45,
                         match={"source": "tvmaze", "id": "527", "url": "https://www.tvmaze.com/shows/527/mythbusters"})
    plain = lineup.add(conn, ch, title="Unmatched Test", year=1984, kind="show", genres=["Comedy"], episode_minutes=30)
    out = {}
    for entry, asked in ((matched, 14), (plain, 3)):
        with dbm.tx(conn):
            wid = conn.execute("INSERT INTO wanted(kind, title, year, season, episode, provider, lineup_id, transient, created_at)"
                               " VALUES ('episode', ?, ?, 1, ?, 'auto', ?, 0, 0)", (f"Episode {asked}", entry["year"], asked, entry["id"])).lastrowid
        f = tmp_path / f"{entry['title']}.mp4"
        f.write_bytes(b"x" * 10)
        apply_report(conn, {"schema": 2, "items": [{
            "request_id": f"w:{wid}", "wanted_id": wid, "status": "done",
            "file": {"path": str(f), "duration": 2600.0, "vcodec": "h264", "height": 576, "size": 10},
            "meta": {"kind": "episode", "show_title": entry["title"], "title": "Break Step Bridge", "season": 2, "episode": 11,
                     "year": entry["year"], "genres": [], "uid": f"test:{wid}", "episodes_total": 273}}]})
        out[entry["title"]] = (dict(conn.execute("SELECT season, episode, title FROM media WHERE uid = ?", (f"test:{wid}",)).fetchone()),
                               conn.execute("SELECT episode FROM wanted WHERE id = ?", (wid,)).fetchone()[0])
    assert out["MythBusters Test"] == ({"season": 2, "episode": 11, "title": "Break Step Bridge"}, 14)
    assert out["Unmatched Test"] == ({"season": 1, "episode": 3, "title": "Episode 3"}, 3)
    assert lineup.entry(conn, matched["id"])["episode_count"] == 273
    for entry in (matched, plain):
        with dbm.tx(conn):
            conn.execute("DELETE FROM media WHERE uid LIKE 'test:%'")
        lineup.remove(conn, entry["id"])


def test_a_rebuild_keeps_what_was_promised_for_the_coming_day():
    """A remote title is placed only beyond the lead time, so a forced rebuild, which places
    nothing remote nearer than that, used to drop every remote slot inside it: the request was
    withdrawn while being fetched and today and tomorrow filled from disk alone. A promised slot
    now survives a rebuild; the readiness check, which rebuilds to replace what has not arrived,
    is not bound by it."""
    from pitv.scheduler.horizon import rebuild_from
    c = dbm.connect(":memory:")
    dbm.init_db(c)
    ch = c.execute("SELECT id FROM channels WHERE content = 'general' ORDER BY number LIMIT 1").fetchone()["id"]
    lineup.add(c, ch, title="Remote Series", year=1984, kind="show", genres=["Comedy"], episode_minutes=30)
    with dbm.tx(c):
        dbm.set_setting(c, "nas_only", False)
        c.execute("UPDATE channels SET series_cadence_days = 1 WHERE id = ?", (ch,))
    day = parse_day("2026-09-14")
    build_horizon(c, start_day=day, days=4, now=local_ts(day, "07:00", tz_of(c)), seed=3, force=True)
    later = local_ts(day + timedelta(days=1), "07:30", tz_of(c))      # the next morning, before anything has aired
    promised = c.execute("SELECT start_ts, wanted_id FROM schedule WHERE channel_id = ? AND wanted_id IS NOT NULL AND replay = 0"
                         " AND start_ts > ? AND start_ts < ? ORDER BY start_ts", (ch, later, later + 23 * 3600)).fetchall()
    assert promised, "nothing remote was placed for the following day"
    build_horizon(c, start_day=day + timedelta(days=1), days=4, now=later, seed=99, force=True)
    after = c.execute("SELECT start_ts, wanted_id FROM schedule WHERE channel_id = ? AND wanted_id IS NOT NULL AND replay = 0"
                      " AND start_ts > ? AND start_ts < ? ORDER BY start_ts", (ch, later, later + 23 * 3600)).fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in promised], "a rebuild dropped what was promised"
    assert not c.execute("SELECT 1 FROM schedule a JOIN schedule b ON a.channel_id = b.channel_id AND a.id < b.id AND a.replay = 0"
                         " AND b.replay = 0 AND a.start_ts < b.end_ts AND b.start_ts < a.end_ts WHERE a.channel_id = ?", (ch,)).fetchone(), "slots overlap"
    rebuild_from(c, ch, promised[0]["start_ts"], now=later, allow_external=False)
    assert not c.execute("SELECT 1 FROM schedule WHERE channel_id = ? AND wanted_id = ? AND start_ts = ?",
                         (ch, promised[0]["wanted_id"], promised[0]["start_ts"])).fetchone(), "readiness could not replace it"
    c.close()


def test_a_programme_goes_to_the_channel_it_was_broadcast_on():
    """The channels model real broadcasters, so a series belongs where it actually went out. A
    channel lists what it will take, its own first, which lets a BBC One programme land on the
    channel modelling BBC Two when the first is full without any of this being known to the code."""
    from pitv.lineup import channel_fit, claimed_networks, network_fit

    one = {"content": "general", "networks": ["BBC One", "BBC Two"], "allowed_genres": ["Drama"]}
    two = {"content": "general", "networks": ["BBC Two", "BBC One"], "allowed_genres": ["Drama"]}
    three = {"content": "general", "networks": ["ITV", "Channel 4"], "allowed_genres": ["Drama"]}
    claimed = claimed_networks([one, two, three])

    assert network_fit(one, "BBC One", claimed) == 1.0
    assert network_fit(two, "BBC One", claimed) == 0.5, "its sibling takes it, but second"
    assert network_fit(three, "BBC One", claimed) is None, "ITV never carried it"
    assert network_fit(one, "bbc one", claimed) == 1.0, "the spelling is not case sensitive"

    drama = {"drama"}
    assert (channel_fit(one, drama, "series", network="BBC One", claimed_nets=claimed)
            > channel_fit(two, drama, "series", network="BBC One", claimed_nets=claimed))
    assert channel_fit(three, drama, "series", network="BBC One", claimed_nets=claimed) is None


def test_a_channel_with_no_broadcaster_listed_is_unchanged_and_films_are_not_skewed():
    """Most of the library has never been looked up and a film has no broadcaster at all. Scoring
    the unknown below par would drive every film onto whichever channel listed no networks."""
    from pitv.lineup import channel_fit, network_fit

    open_channel = {"content": "general", "allowed_genres": ["Drama"]}
    bbc = {"content": "general", "networks": ["BBC One"], "allowed_genres": ["Drama"]}
    assert network_fit(open_channel, "ITV") == 1.0, "a channel listing nothing takes anything"
    assert network_fit(bbc, None) == 1.0, "and an unknown broadcaster counts for nothing"
    drama = {"drama"}
    assert channel_fit(bbc, drama, "movie") == channel_fit(open_channel, drama, "movie")


def test_an_import_from_a_broadcaster_no_channel_claims_is_placed_freely():
    """The 1980s schedules were full of American series. A rule that sent anything from ABC or
    NBC to no channel at all would have thrown out Dallas and The A-Team with the streaming
    material, so only broadcasters some channel actually claims constrain anything."""
    from pitv.lineup import channel_fit, claimed_networks, network_fit

    channels = [{"content": "general", "networks": ["BBC One", "BBC Two"], "allowed_genres": ["Drama"]},
                {"content": "general", "networks": ["ITV", "Channel 4"], "allowed_genres": ["Drama"]}]
    claimed = claimed_networks(channels)
    assert claimed == {"bbc one", "bbc two", "itv", "channel 4"}

    assert network_fit(channels[0], "ABC", claimed) == 1.0, "nobody claims ABC, so it goes anywhere"
    assert network_fit(channels[1], "ABC", claimed) == 1.0
    assert network_fit(channels[1], "BBC One", claimed) is None, "but the BBC is claimed elsewhere"

    drama = {"drama"}
    assert channel_fit(channels[1], drama, "series", network="ABC", claimed_nets=claimed) is not None
    assert channel_fit(channels[1], drama, "series", network="BBC Two", claimed_nets=claimed) is None


def test_a_broadcaster_is_recognised_by_its_current_name():
    """Providers name a channel as it is called now, not as it was called then: TVmaze returns
    ITV1 for everything ITV broadcast, including programmes from before that name existed, and
    19 of the catalogue's series came back that way."""
    from pitv.lineup import claimed_networks, network_fit, network_key

    assert network_key("ITV1") == network_key("ITV") == network_key("CITV") == "itv"
    assert network_key("BBC1") == network_key("BBC One") == "bbc one"
    assert network_key("Channel4") == network_key("channel 4") == "channel 4"

    itv = {"networks": ["ITV", "Channel 4"]}
    bbc = {"networks": ["BBC One", "BBC Two"]}
    claimed = claimed_networks([bbc, itv])
    assert network_fit(itv, "ITV1", claimed) == 1.0, "however the provider spells it"
    assert network_fit(bbc, "ITV1", claimed) is None


def test_a_year_taken_from_the_request_is_noted_rather_than_trusted(conn, tmp_path):
    """A delivered file takes the upload's own year, or the year PiTV asked with when none could
    be read. That fallback is right for a series, whose episodes really are of its year, and a
    guess for a strand that ran for decades: a 2019 round fetched against an entry dated 1988
    would be filed as 1988 and pass an era check it should fail. It is noted, not refused,
    because the guess is usually right and nothing here can tell which case it is."""
    from pitv.content import apply_report

    ch = conn.execute("SELECT id FROM channels WHERE content = 'general' ORDER BY number LIMIT 1").fetchone()["id"]
    entry = lineup.add(conn, ch, title="Superbikes", year=1988, kind="show", genres=["Sport"], episode_minutes=45)
    for n, (read, expect) in enumerate(((2019, None), (None, "Year taken from the request, not the file"))):
        with dbm.tx(conn):
            wid = conn.execute("INSERT INTO wanted(kind, title, year, episode, provider, lineup_id, created_at)"
                               " VALUES ('episode', 'Superbikes', 1988, ?, 'auto', ?, 0)",
                               (n + 1, entry["id"])).lastrowid
        f = tmp_path / f"round{n}.mp4"
        f.write_bytes(b"x" * 10)
        meta = {"kind": "episode", "title": f"Round {n}", "uid": f"yt:round{n}"}
        if read:
            meta["year"] = read
        apply_report(conn, {"schema": 2, "items": [{
            "request_id": f"w:{wid}", "wanted_id": wid, "status": "done",
            "file": {"path": str(f), "duration": 2700.0, "vcodec": "h264", "height": 576, "size": 10},
            "meta": meta}]})
        row = conn.execute("SELECT year, attention FROM media WHERE uid = ?", (f"yt:round{n}",)).fetchone()
        assert row["year"] == (read or 1988)
        assert row["attention"] == expect


def test_a_title_added_by_hand_is_kept_after_it_airs(conn):
    """An episode found once is expensive to find again, and the point of fetching it is that a
    later airing costs nothing. The default was to discard it, so ninety-two titles added from
    the catalogue were all marked remove-after-airing."""
    ch = conn.execute("SELECT id FROM channels WHERE content = 'general' ORDER BY number LIMIT 1").fetchone()["id"]
    kept = lineup.add(conn, ch, title="Kept By Default", year=1985, kind="show", episode_minutes=30)
    assert not kept["transient"], "what is fetched is kept unless the caller says otherwise"
    once = lineup.add(conn, ch, title="Asked To Go", year=1985, kind="show", episode_minutes=30, transient=True)
    assert once["transient"], "and a caller that wants it gone can still say so"


def test_a_genre_nothing_carries_yet_is_still_offered(conn):
    """A band asking for material the library does not hold is not a mistake: a band with nothing
    at all is what tells pitv_content to collect it, which is how the music bands were filled.
    Offering only what is already here made a channel of new material impossible to set up, so
    YouTube could not be chosen for a channel meant to be filled from YouTube."""
    from pitv.genres import KNOWN

    offered = lineup.facets(conn)["genres"]
    assert "YouTube" in offered, "a genre nothing carries yet can still be asked for"
    assert offered["YouTube"] == {"episode": 0, "movie": 0, "music": 0}, "and is honest about holding none"
    assert set(KNOWN) <= set(offered), "every genre the vocabulary knows is choosable"
    carried = {g for g, counts in offered.items() if any(counts.values())}
    assert carried, "what the library does hold still counts"


def test_the_broadcasters_a_channel_stands_for_are_read_as_a_list(tmp_path):
    """Stored as JSON and read back through the same decoding as every other list column. Left
    out of it, placement received the raw text and iterated its characters: every channel then
    scored neutral for every broadcaster and the rule had no effect at all, silently."""
    from pitv.db import connect, init_db, rows_to_dicts
    from pitv.lineup import claimed_networks, network_fit

    conn = connect(tmp_path / "fresh.db")
    init_db(conn)
    channels = rows_to_dicts(conn.execute("SELECT * FROM channels ORDER BY number"))
    one = channels[0]
    assert one["networks"] == ["BBC One", "BBC Two"], "a list, not the text it was stored as"

    claimed = claimed_networks(channels)
    assert claimed >= {"bbc one", "bbc two", "itv", "channel 4"}
    assert network_fit(one, "BBC One", claimed) == 1.0
    assert network_fit(one, "ITV1", claimed) is None, "claimed by another channel"
    assert network_fit(one, "ABC", claimed) == 1.0, "claimed by nobody, so unconstrained"
    conn.close()
