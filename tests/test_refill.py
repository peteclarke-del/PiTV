from pitv import db as dbm
from pitv.scheduler import build


def test_every_future_gap_rebuilds_from_the_first_gap_only(monkeypatch):
    conn = dbm.connect(":memory:")
    dbm.init_db(conn)
    one = conn.execute("SELECT id FROM channels WHERE number = 1").fetchone()["id"]
    two = conn.execute("SELECT id FROM channels WHERE number = 2").fetchone()["id"]
    rows = [
        (one, "2026-09-18", 1200, 1500, "filler", 0),       # even a five-minute gap is retried
        (one, "2026-09-18", 3000, 4000, "filler", 0),       # same day rebuilds from its first gap
        (two, "2026-09-18", 500, 2000, "filler", 0),        # a gap already under way starts now
        (two, "2026-09-18", 2200, 4000, "filler", 1),       # overnight replay is not rebuilt
    ]
    conn.executemany(
        "INSERT INTO schedule(channel_id,day,start_ts,end_ts,kind,replay) VALUES (?,?,?,?,?,?)",
        rows,
    )

    calls = []

    def fake_rebuild(_conn, channel_id, from_ts, *, now):
        calls.append((channel_id, from_ts, now))
        return {"status": "ok", "summary": "2 programmes"}

    monkeypatch.setattr(build, "rebuild_from", fake_rebuild)
    result = build.refill_empty_days(conn, now=1000)

    assert calls == [(two, 1000, 1000), (one, 1200, 1000)]
    assert result == {
        "status": "ok",
        "days": 2,
        "programmes": 4,
        "summary": "2 gap-bearing channel-days rebuilt, 4 programmes",
    }
