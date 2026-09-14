"""Development helpers: generate a fake media library so everything runs without the NAS.

Files are real, tiny H.264 videos with realistic durations (a 25-minute file is ~40 KB
because it is 64x36 pixels at 1 frame per second), so ffprobe, the scanner, the scheduler
and even mpv all work on them.
"""

from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path

# (title, year, seasons, episodes per season, minutes, genres, certificate, kids)
FAKE_SHOWS = [
    ("Blake's 7", 1978, 4, 13, 50, ["Science Fiction", "Drama"], "PG", False),
    ("Only Fools and Horses", 1981, 7, 7, 30, ["Comedy"], "PG", False),
    ("Minder", 1979, 5, 11, 52, ["Drama", "Comedy"], "12", False),
    ("The Young Ones", 1982, 2, 6, 35, ["Comedy"], "15", False),
    ("Grange Hill", 1978, 6, 18, 25, ["Children", "Drama"], "U", True),
    ("Danger Mouse", 1981, 5, 12, 10, ["Animation", "Children"], "U", True),
    ("Bergerac", 1981, 5, 10, 50, ["Crime", "Drama"], "PG", False),
    ("Yes Minister", 1980, 3, 7, 30, ["Comedy"], "U", False),
    ("Boys from the Blackstuff", 1982, 1, 5, 65, ["Drama"], "15", False),
    ("Auf Wiedersehen, Pet", 1983, 2, 13, 50, ["Comedy", "Drama"], "12", False),
    ("Knightmare", 1987, 4, 14, 25, ["Children", "Game Show"], "U", True),
    ("Blackadder", 1983, 4, 6, 30, ["Comedy"], "12", False),
    ("Howards' Way", 1985, 4, 13, 50, ["Drama"], "PG", False),
    ("Red Dwarf", 1988, 3, 6, 30, ["Comedy", "Science Fiction"], "12", False),
    ("The Bill", 1984, 3, 12, 25, ["Crime", "Drama"], "12", False),
    ("Countdown", 1982, 2, 20, 30, ["Game Show"], "U", False),
    ("Byker Grove", 1989, 2, 10, 25, ["Children", "Drama"], "U", True),
    ("One Foot in the Grave", 1990, 3, 6, 30, ["Comedy"], "12", False),
    ("Spitting Image", 1984, 3, 8, 25, ["Comedy"], "15", False),
    ("Inspector Morse", 1987, 3, 4, 100, ["Crime", "Drama"], "15", False),
    ("Bread", 1986, 3, 8, 30, ["Comedy"], "PG", False),
    ("Robin of Sherwood", 1984, 3, 7, 50, ["Adventure", "Drama"], "PG", False),
    ("Willo the Wisp", 1981, 1, 26, 5, ["Animation", "Children"], "U", True),
    ("Cracker", 1993, 2, 5, 100, ["Crime", "Drama"], "18", False),
    ("Dad's Army", 1968, 5, 8, 30, ["Comedy"], "U", False),
    ("The Prisoner", 1967, 1, 17, 50, ["Drama", "Science Fiction"], "PG", False),
    ("Fawlty Towers", 1975, 2, 6, 30, ["Comedy"], "PG", False),
    ("The Clangers", 1969, 2, 13, 10, ["Animation", "Children"], "U", True),
    ("The Sweeney", 1975, 4, 13, 50, ["Crime", "Drama"], "15", False),
]

# (title, year, minutes, certificate, genres)
FAKE_MOVIES = [
    ("Time Bandits", 1981, 116, "PG", ["Fantasy", "Adventure"]),
    ("Gregory's Girl", 1981, 91, "PG", ["Comedy", "Romance"]),
    ("Local Hero", 1983, 111, "PG", ["Comedy", "Drama"]),
    ("Educating Rita", 1983, 110, "15", ["Comedy", "Drama"]),
    ("The Long Good Friday", 1980, 114, "18", ["Crime", "Thriller"]),
    ("Withnail and I", 1987, 107, "15", ["Comedy"]),
    ("Brazil", 1985, 132, "15", ["Science Fiction", "Comedy"]),
    ("A Fish Called Wanda", 1988, 108, "15", ["Comedy"]),
    ("Labyrinth", 1986, 101, "U", ["Fantasy", "Family"]),
    ("Chariots of Fire", 1981, 124, "PG", ["Drama", "History"]),
    ("Ghostbusters", 1984, 105, "PG", ["Comedy", "Fantasy"]),
    ("Back to the Future", 1985, 116, "PG", ["Science Fiction", "Adventure"]),
    ("The Goonies", 1985, 114, "PG", ["Adventure", "Family"]),
    ("Blade Runner", 1982, 117, "15", ["Science Fiction"]),
    ("The Terminator", 1984, 107, "18", ["Action", "Science Fiction"]),
    ("Lethal Weapon", 1987, 110, "18", ["Action", "Crime"]),
    ("Beverly Hills Cop", 1984, 105, "15", ["Comedy", "Action"]),
    ("E.T. the Extra-Terrestrial", 1982, 115, "U", ["Family", "Science Fiction"]),
    ("Airplane II", 1982, 85, "PG", ["Comedy"]),
    ("Clockwise", 1986, 96, "PG", ["Comedy"]),
    ("Groundhog Day", 1993, 101, "PG", ["Comedy", "Fantasy"]),
    ("Four Weddings and a Funeral", 1994, 117, "15", ["Comedy", "Romance"]),
    ("The Untouchables", 1987, 119, "15", ["Crime", "Drama"]),
    ("Big", 1988, 104, "PG", ["Comedy", "Fantasy"]),
    ("Casablanca", 1942, 102, "U", ["Drama", "Romance"]),
    ("Some Like It Hot", 1959, 121, "PG", ["Comedy"]),
    ("The Italian Job", 1969, 99, "PG", ["Crime", "Comedy"]),
    ("Jason and the Argonauts", 1963, 104, "U", ["Fantasy", "Adventure"]),
    ("Carry On Camping", 1969, 88, "PG", ["Comedy"]),
    ("Get Carter", 1971, 112, "18", ["Crime", "Thriller"]),
    ("The Wicker Man", 1973, 88, "15", ["Horror", "Mystery"]),
    ("Mystery Movie", None, 95, None, []),  # deliberately incomplete metadata
]

FAKE_ADVERTS = [
    ("Milk Tray", 1984), ("Hovis", 1983), ("Cadbury's Flake", 1985), ("Smash", 1981),
    ("Shake n Vac", 1980), ("Yellow Pages Fly Fishing", 1983), ("Um Bongo", 1985),
    ("Kia-Ora", 1984), ("British Rail", 1987), ("Levi's 501", 1985), ("Tango", 1992),
    ("Milky Bar", 1982), ("R White's Lemonade", 1980), ("Fry's Turkish Delight", 1984),
    ("Texan Bar", 1981), ("Cinzano", 1980), ("Hamlet Cigars", 1983), ("Bisto", 1986),
    ("Guinness Surfer", 1999), ("Crunchie", 1988), ("Bounty", 1984), ("Ready Brek", 1982),
    ("Hofmeister", 1985), ("Access Card", 1987), ("Weetabix", 1983), ("Findus Crispy Pancakes", 1984),
    ("Oxo Family", 1986), ("Persil", 1981), ("Prudential", 1988), ("Nescafe Gold Blend", 1989),
]

# Sport on PiTV is wrestling, snooker, motorcycle racing and strongman competitions.
# (title, year, dated?, episodes, minutes) - dated shows get one file per week of 1985
FAKE_SPORT = [
    ("World of Sport Wrestling", 1965, True, 24, 55),
    ("Pot Black", 1969, False, 16, 30),
    ("World Snooker Championship", 1985, False, 14, 110),
    ("Motorcycle Grand Prix", 1980, True, 16, 60),
    ("British Superbike Championship", 1988, False, 10, 50),
    ("World's Strongest Man", 1977, False, 8, 50),
    ("Britain's Strongest Man", 1979, False, 6, 45),
]

_DURATION_TEMPLATES: dict[int, Path] = {}


def _make_video(dest: Path, seconds: int, text: str) -> None:
    """Create (or copy from a cached template of the same length) a tiny video file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmpl = _DURATION_TEMPLATES.get(seconds)
    if tmpl is None or not tmpl.exists():
        tmpl = dest.parent.parent.parent / ".templates" / f"{seconds}.mp4"
        tmpl.parent.mkdir(parents=True, exist_ok=True)
        if not tmpl.exists():
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                 "-i", f"color=c=0x203040:s=64x36:r=1:d={seconds}",
                 "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage", "-crf", "51",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(tmpl)],
                check=True)
        _DURATION_TEMPLATES[seconds] = tmpl
    shutil.copyfile(tmpl, dest)


def _nfo(path: Path, root: str, fields: dict[str, object], genres: list[str]) -> None:
    lines = [f"<{root}>"]
    for k, v in fields.items():
        if v is not None:
            lines.append(f"  <{k}>{v}</{k}>")
    for g in genres:
        lines.append(f"  <genre>{g}</genre>")
    lines.append(f"</{root}>")
    path.write_text("\n".join(lines) + "\n")


def build_fake_library(root: Path, seed: int = 1, with_nfo: bool = True,
                       max_episodes_per_show: int | None = None) -> dict[str, Path]:
    rnd = random.Random(seed)
    tv = root / "tvshows"
    movies = root / "movies"
    pitv = root / "pitv"
    for d in (tv, movies, pitv / "Adverts", pitv / "Idents"):
        d.mkdir(parents=True, exist_ok=True)

    for title, year, seasons, eps, minutes, genres, cert, _kids in FAKE_SHOWS:
        safe = title.replace(":", "").replace("/", "-")
        show_dir = tv / f"{safe} ({year})"
        show_dir.mkdir(exist_ok=True)
        if with_nfo:
            _nfo(show_dir / "tvshow.nfo", "tvshow",
                 {"title": title, "year": year, "premiered": f"{year}-09-01", "mpaa": f"UK:{cert}",
                  "plot": f"{title}, first shown in {year}."}, genres)
        count = 0
        for s in range(1, seasons + 1):
            for e in range(1, eps + 1):
                if max_episodes_per_show and count >= max_episodes_per_show:
                    break
                f = show_dir / f"Season {s:02d}" / f"{safe} - S{s:02d}E{e:02d} - Episode {e}.mp4"
                jitter = rnd.choice([-2, -1, 0, 0, 0, 1])
                _make_video(f, (minutes + jitter) * 60, f"{title} S{s}E{e}")
                count += 1

    import datetime as _dt
    sport = root / "tvsports"
    sport.mkdir(exist_ok=True)
    for title, year, dated, eps, minutes in FAKE_SPORT:
        show_dir = sport / f"{title} ({year})"
        show_dir.mkdir(exist_ok=True)
        if with_nfo:
            _nfo(show_dir / "tvshow.nfo", "tvshow", {"title": title, "year": year, "mpaa": "UK:U"}, ["Sport"])
        if dated:
            day = _dt.date(1985, 1, 5)
            for _i in range(eps):
                _make_video(show_dir / "Season 1985" / f"{title} - {day.isoformat()}.mp4", minutes * 60, title)
                day += _dt.timedelta(days=7)
        else:
            for e in range(1, eps + 1):
                _make_video(show_dir / "Season 01" / f"{title} - S01E{e:02d} - Episode {e}.mp4", minutes * 60, title)

    for title, year, minutes, cert, genres in FAKE_MOVIES:
        safe = title.replace(":", "").replace("/", "-")
        folder = movies / (f"{safe} ({year})" if year else safe)
        f = folder / (f"{safe} ({year}).mp4" if year else f"{safe}.mp4")
        _make_video(f, minutes * 60, title)
        if with_nfo and year:
            _nfo(f.with_suffix(".nfo"), "movie",
                 {"title": title, "year": year, "mpaa": f"UK:{cert}",
                  "plot": f"{title} ({year})."}, genres)

    for title, year in FAKE_ADVERTS:
        f = pitv / "Adverts" / str(year) / f"{title}.mp4"
        _make_video(f, rnd.choice([20, 30, 30, 40, 60]), title)

    for ch in (1, 2, 3, 4):
        for i in (1, 2):
            _make_video(pitv / "Idents" / f"ch{ch}" / f"Ident {i}.mp4", rnd.choice([8, 10, 15]), f"ch{ch}")
    _make_video(pitv / "Static" / "static.mp4", 2, "static")
    return {"tv": tv, "movies": movies, "pitv": pitv, "sport": sport}
