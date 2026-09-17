from pitv import db as dbm
from pitv.web.api.public import api_schedule


def test_adverts_and_band_items_are_independent_schedule_filters():
    conn = dbm.connect(":memory:")
    dbm.init_db(conn)
    music = conn.execute("SELECT id FROM channels WHERE number = 5").fetchone()["id"]
    rows = [
        (music, "2026-09-18", 100, 200, "programme", "Rock Hour", "First video"),
        (music, "2026-09-18", 200, 300, "programme", "Rock Hour", "Second video"),
        (music, "2026-09-18", 300, 330, "advert", None, "Advert"),
        (music, "2026-09-18", 330, 500, "programme", None, "Next programme"),
    ]
    conn.executemany(
        "INSERT INTO schedule(channel_id,day,start_ts,end_ts,kind,block,title) VALUES (?,?,?,?,?,?,?)",
        rows,
    )

    plain = api_schedule(conn, start=0, end=1000, ads=0, bands=0)["slots"]
    continuity = api_schedule(conn, start=0, end=1000, ads=1, bands=0)["slots"]
    band_items = api_schedule(conn, start=0, end=1000, ads=0, bands=1)["slots"]
    everything = api_schedule(conn, start=0, end=1000, ads=1, bands=1)["slots"]

    assert len(plain) == 2 and plain[0]["block"] == "Rock Hour" and plain[0]["items"] == 2
    assert plain[1]["title"] == "Next programme"
    assert len(continuity) == 3 and continuity[0]["block"] == "Rock Hour" and continuity[0]["items"] == 2
    assert [(s["title"], s["kind"]) for s in continuity[1:]] == [
        ("Advert", "advert"), ("Next programme", "programme")]
    assert [s["title"] for s in band_items] == ["First video", "Second video", "Next programme"]
    assert [s["title"] for s in everything] == [
        "First video", "Second video", "Advert", "Next programme"]
