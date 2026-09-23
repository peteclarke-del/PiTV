"""Delivery reports: what a run says it did, and what it says it never got to."""

import json


def test_work_a_run_never_reached_is_recorded_and_only_called_out_when_it_persists(tmp_path):
    """pitv_content delivers in bands, scheduled before unscheduled, and a slice that runs out
    of time never reaches the last of them. Nothing fails, nothing is logged, and the only
    symptom is a channel that stays empty: 148 requests sat untouched for a day while every
    report read as an ordinary busy night.

    One run that ran out of time is ordinary, because the next starts from the top and gets
    further. The same band unreached twice running is not, since nothing about it will change on
    its own, so only what two runs agree on becomes a finding."""
    from conftest import make_library

    from pitv import doctor
    from pitv.content import UNREACHED, apply_report

    ctx = make_library(tmp_path / "starved", max_episodes=1)
    conn = ctx["conn"]

    def run(started: int, unreached):
        apply_report(conn, {"schema": 2, "items": [],
                            "run": {"tool": "pitv-content 9.9", "started_ts": started, "finished_ts": started + 60,
                                    "unreached_bands": unreached}})

    band = [{"band": 4, "name": "wanted", "requests": 1653, "first_at": 793, "of": 2446}]
    run(1_000_000, band)
    details = json.loads(conn.execute(
        "SELECT details FROM run_log WHERE kind = 'content' ORDER BY id DESC LIMIT 1").fetchone()["details"])
    assert any(m.startswith(UNREACHED) and "1653 request(s) in wanted" in m and "793 of 2446" in m for m in details)
    assert doctor._starved(conn) == [], "one run is a busy night, not a starved band"

    run(1_000_100, band)
    starved = doctor._starved(conn)
    assert len(starved) == 1 and "wanted" in starved[0], "twice running is the state worth saying"
    finding = next(f for f in doctor._findings({"starved": starved}) if "not reached" in f)
    assert "will change on its own" in finding and "1653 request(s) in wanted" in finding

    # A band that empties stops being reported, and a band with nothing in it never was.
    run(1_000_200, [{"band": 4, "name": "wanted", "requests": 0, "first_at": 0, "of": 0}])
    run(1_000_300, [])
    assert doctor._starved(conn) == []
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
