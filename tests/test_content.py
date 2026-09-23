"""Delivery reports: what a run says it did, and what it says it never got to."""

import json


def test_a_slice_that_stopped_short_and_one_that_held_work_back_are_different_facts(tmp_path):
    """These were one fact and either was then impossible to judge. A slice that ends before
    looking at a band is starved and nothing about it will change on its own; a request held for
    a later slice is a rule working, since one long re-encode can take an evening. Reported
    together, a threshold could only guess which it was looking at, and would have filled the
    findings with the rule working correctly.

    They are separate at source now and stay separate here: a band is in one or the other, never
    both. An ordinary wait says nothing; a wait that has outlived the bound meant to end it does,
    because a rule that can hold work back for ever is the fault that rule could become."""
    from conftest import make_library

    from pitv import doctor
    from pitv.content import HELD, UNREACHED, apply_report

    ctx = make_library(tmp_path / "starved", max_episodes=1)
    conn = ctx["conn"]

    def run(started: int, unreached=(), held=()):
        apply_report(conn, {"schema": 2, "items": [],
                            "run": {"tool": "pitv-content 9.9", "started_ts": started, "finished_ts": started + 60,
                                    "unreached_bands": list(unreached), "passed_over": list(held)}})

    wanted = [{"band": 4, "name": "wanted", "requests": 1653, "first_at": 793, "of": 2446}]
    ordinary = [{"band": 3, "name": "transcodes", "requests": 5, "most_slices": 2, "limit": 8}]

    run(1_000_000, unreached=wanted, held=ordinary)
    details = json.loads(conn.execute(
        "SELECT details FROM run_log WHERE kind = 'content' ORDER BY id DESC LIMIT 1").fetchone()["details"])
    assert any(m.startswith(UNREACHED) and "1653 request(s) in wanted" in m and "793 of 2446" in m for m in details)
    assert any(m.startswith(HELD) and "transcodes" in m and "2 slice(s) of 8" in m for m in details)

    starved = doctor._starved(conn)
    assert [s["band"] for s in starved] == ["wanted"], "a band held over is not a band never reached"
    assert doctor._held_too_long(conn) == [], "waiting two slices of eight is the rule working"
    finding = next(f for f in doctor._findings({"starved": starved}) if "wanted" in f)
    assert "ended its slice before reaching" in finding and "the last run" in finding

    # It says how long it has been, which is worth knowing even though it decides nothing.
    run(1_000_100, unreached=wanted, held=ordinary)
    assert doctor._starved(conn)[0]["runs"] == 2
    assert "2 runs running" in next(f for f in doctor._findings({"starved": doctor._starved(conn)}) if "wanted" in f)

    # Past the bound pitv_content says it takes the work regardless, the bound has not held.
    run(1_000_200, held=[{"band": 3, "name": "transcodes", "requests": 5, "most_slices": 14, "limit": 8}])
    late = doctor._held_too_long(conn)
    assert [(h["band"], h["slices"], h["limit"]) for h in late] == [("transcodes", 14, 8)]
    assert "may never be done" in next(f for f in doctor._findings({"held_too_long": late}) if "transcodes" in f)
    assert doctor._starved(conn) == [], "the slice reached every band that run"

    # A band that empties stops being reported, and a band with nothing in it never was.
    run(1_000_300, unreached=[{"band": 4, "name": "wanted", "requests": 0}])
    run(1_000_400)
    assert doctor._starved(conn) == [] and doctor._held_too_long(conn) == []
    conn.close()


def test_a_run_that_died_before_reporting_is_not_a_quiet_night(tmp_path, monkeypatch):
    """PiTV learns what a run did from the report it sends at the end, so one that dies first
    leaves no entry at all and reads exactly like a night with nothing to do. Two happened in a
    single afternoon, both invisible here, and the only trace was a line in pitv_content's own
    job list that nothing on this side read.

    A run whose report did arrive is not listed however it ended, because then PiTV knows what
    it did; what is listed is what PiTV was never told about."""
    from conftest import make_library

    from pitv import db as dbm2
    from pitv import doctor, tool_client
    from pitv.content import apply_report

    ctx = make_library(tmp_path / "abandoned", max_episodes=1)
    conn = ctx["conn"]
    started = dbm2.now_ts() - 600
    jobs = [{"job_id": "a", "mode": "cache", "status": "failed", "started_ts": started,
             "summary": "the admin restarted while it ran; its result was not recorded"},
            {"job_id": "b", "mode": "catalogue", "status": "failed", "started_ts": started - 60, "summary": ""},
            {"job_id": "c", "mode": "cache", "status": "running", "started_ts": started},
            {"job_id": "d", "mode": "cache", "status": "failed", "started_ts": started - 5 * 86400,
             "summary": "last week, long since irrelevant"}]
    monkeypatch.setattr(tool_client, "request", lambda *a, **k: (200, jobs))

    found = doctor._abandoned(conn, dbm2.all_settings(conn))
    assert [j["job_id"] for j in found] == ["a", "b"], "only recent runs that failed without reporting"
    assert found[1]["detail"] == "no reason given", "a job that says nothing still gets a sentence"
    finding = next(f for f in doctor._findings({"abandoned_runs": found}) if "without reporting" in f)
    assert "nothing is lost" in finding, "the remedy is that the next run sees the truth on disk"

    # Once its report arrives, PiTV knows what that run did and stops asking about it.
    apply_report(conn, {"schema": 2, "items": [],
                        "run": {"tool": "pitv-content 9.9", "started_ts": started, "finished_ts": started + 30}})
    assert [j["job_id"] for j in doctor._abandoned(conn, dbm2.all_settings(conn))] == ["b"]
    conn.close()


def test_a_channels_length_is_refreshed_by_pitv_content_never_learned_and_frozen():
    """`episode_count` stops PiTV asking past the end of a run, and there are two ways an entry
    can get one. Only one of them goes stale, and the difference is the whole of the fix.

    A count learned once from an online lookup is never revisited, which is right for a series
    that ran and ended and wrong for a creator's channel: one recorded as 1003 videos became 503
    overnight when shorts stopped being numbered, and PiTV would have spent attempts asking for
    five hundred that do not exist. A channel is no longer asked about that way.

    The count that arrives with a delivery or an over-ask is the opposite and must be kept. It
    comes with every answer pitv_content gives, so it is replaced as often as the two talk and
    follows a channel that is still growing. Removing it as well, which was the first thing I
    tried, would have left a channel with no bound at all: it would ask past the end for ever,
    failing each time until the attempt limit, which is worse than the staleness it cured."""
    from pitv import db as dbm2
    from pitv import lineup as lineup_mod
    from pitv import youtube
    from pitv.content import apply_report

    conn = dbm2.connect(":memory:")
    dbm2.init_db(conn)
    ch = conn.execute("SELECT id FROM channels WHERE content = 'general' ORDER BY number LIMIT 1").fetchone()["id"]
    entry = lineup_mod.add(conn, ch, title="A Creator", kind="show", genres=["Comedy"],
                           match=youtube.parse("https://www.youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa"))

    def over_ask(total: int) -> None:
        with dbm2.tx(conn):
            cur = conn.execute("INSERT INTO wanted(kind, title, episode, lineup_id, created_at)"
                               " VALUES ('episode', 'Episode 1', 1, ?, 1)", (entry["id"],))
        apply_report(conn, {"schema": 2, "items": [
            {"request_id": f"w:{cur.lastrowid}", "wanted_id": cur.lastrowid, "status": "failed",
             "message": f"no such episode: the series has {total}", "meta": {"episodes_total": total}}]})

    def count() -> int | None:
        return conn.execute("SELECT episode_count FROM lineup WHERE id = ?", (entry["id"],)).fetchone()[0]

    assert count() is None, "nothing is assumed about a channel before it has answered"
    over_ask(1003)
    assert count() == 1003, "an answer bounds the asking, so it does not run on for ever"
    over_ask(503)
    assert count() == 503, "and the next answer replaces it, so shorts being dropped is followed"
    over_ask(540)
    assert count() == 540, "as is a channel that has grown since"
    conn.close()
