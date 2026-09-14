from pathlib import PurePath

from pitv.library.naming import parse_episode, parse_title_year, parse_certificate_tag


def test_title_year_paren():
    r = parse_title_year("Blake's 7 (1978)")
    assert (r.title, r.year) == ("Blake's 7", 1978)


def test_title_year_dotted():
    r = parse_title_year("Top.Gun.1986.1080p.BluRay")
    assert (r.title, r.year) == ("Top Gun", 1986)


def test_title_no_year():
    r = parse_title_year("Mystery Movie")
    assert (r.title, r.year) == ("Mystery Movie", None)


def test_leading_year():
    r = parse_title_year("1984 - Milk Tray")
    assert (r.title, r.year) == ("Milk Tray", 1984)


def test_episode_sxxeyy():
    e = parse_episode(PurePath("Season 02/Minder - S02E05 - The Beach.mkv"), "Minder")
    assert (e.season, e.episode, e.title) == (2, 5, "The Beach")


def test_episode_nxnn():
    e = parse_episode(PurePath("Series 1/minder 1x03 something.avi"), "Minder")
    assert (e.season, e.episode) == (1, 3)


def test_episode_from_folder_and_number():
    e = parse_episode(PurePath("Season 3/07 - Title.mp4"))
    assert (e.season, e.episode, e.title) == (3, 7, "Title")


def test_certificate_tag():
    assert parse_certificate_tag("Film [15].mkv") == "15"
    assert parse_certificate_tag("Film.mkv") is None
