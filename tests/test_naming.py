from pathlib import PurePath

from pitv.library.naming import parse_certificate_tag, parse_decade_dir, parse_episode, parse_music, parse_title_year


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


def test_dated_episode():
    e = parse_episode(PurePath("Season 1985/Grandstand - 1985-03-16.mp4"), "Grandstand")
    assert (e.season, e.episode) == (1985, 316)
    assert e.title == "16/03/1985"


def test_music_names():
    m = parse_music("Queen - Radio Ga Ga (1984)")
    assert (m.artist, m.title, m.year) == ("Queen", "Radio Ga Ga", 1984)
    m = parse_music("Some Video")
    assert (m.artist, m.title, m.year) == (None, "Some Video", None)
    assert parse_decade_dir("1980s") == 1980 and parse_decade_dir("80s") == 1980 and parse_decade_dir("Rock") is None


def test_four_digit_seasons():
    e = parse_episode(PurePath("Season 1985/World of Sport Wrestling - S1985E01 - Big Daddy v Giant Haystacks.mp4"), "World of Sport Wrestling")
    assert (e.season, e.episode, e.title) == (1985, 1, "Big Daddy v Giant Haystacks")
    e = parse_episode(PurePath("Season 85/Show - S85E03 - Title.mp4"), "Show")
    assert (e.season, e.episode) == (85, 3)
