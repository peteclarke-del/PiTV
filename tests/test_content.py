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
