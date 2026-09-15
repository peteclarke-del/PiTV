"""Development helpers: a fake media library so everything runs without the NAS or pitv_content.

The files are real, tiny H.264 videos (4:3 colour bars with a running timecode, one frame per
second) with realistic durations, so the player can play them; alongside them the builder
writes the schema 2 library index pitv_content would publish for that folder tree, which is
what PiTV imports.
"""

from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path
from typing import Any

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
    ("Bananaman", 1983, 3, 13, 5, ["Animation", "Children"], "U", True),
    ("Dungeons & Dragons", 1983, 3, 9, 22, ["Animation", "Fantasy"], "U", True),
    ("Thundercats", 1985, 4, 20, 22, ["Animation", "Action"], "U", True),
    ("SuperTed", 1983, 3, 12, 10, ["Animation", "Children"], "U", True),
    ("Count Duckula", 1988, 4, 15, 22, ["Animation", "Comedy"], "U", True),
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

# (artist, title, year, genre, minutes) - music videos; concerts live under Concerts/
FAKE_MUSIC = [
    ("Queen", "Radio Ga Ga", 1984, "Pop", 5), ("Duran Duran", "Rio", 1982, "New Wave", 5),
    ("The Human League", "Don't You Want Me", 1981, "Synth", 4), ("Madness", "Our House", 1982, "Ska", 3),
    ("Iron Maiden", "Run to the Hills", 1982, "Metal", 4), ("Def Leppard", "Pour Some Sugar on Me", 1987, "Metal", 5),
    ("Dire Straits", "Money for Nothing", 1985, "Rock", 8), ("The Clash", "London Calling", 1979, "Punk", 3),
    ("Chic", "Le Freak", 1978, "Disco", 5), ("Earth, Wind & Fire", "September", 1978, "Disco", 4),
    ("The Jam", "Going Underground", 1980, "Punk", 3), ("Kate Bush", "Running Up That Hill", 1985, "Pop", 5),
    ("Pet Shop Boys", "West End Girls", 1985, "Synth", 5), ("Whitney Houston", "How Will I Know", 1985, "Pop", 4),
    ("The Smiths", "This Charming Man", 1983, "Indie", 3), ("Bon Jovi", "Livin' on a Prayer", 1986, "Rock", 4),
    ("Marvin Gaye", "Sexual Healing", 1982, "Soul", 4), ("Bob Marley", "Could You Be Loved", 1980, "Reggae", 4),
    ("Blur", "Parklife", 1994, "Indie", 3), ("Oasis", "Wonderwall", 1995, "Rock", 4),
    ("The Beatles", "Hey Jude", 1968, "Pop", 8), ("The Rolling Stones", "Jumpin' Jack Flash", 1968, "Rock", 4),
    ("Pulp", "Common People", 1995, "Indie", 6), ("Prince", "1999", 1982, "Funk", 6),
    ("Donna Summer", "I Feel Love", 1977, "Disco", 6), ("Black Sabbath", "Paranoid", 1970, "Metal", 3),
    ("Soft Cell", "Tainted Love", 1981, "Synth", 3), ("Wham!", "Club Tropicana", 1983, "Pop", 4),
]
FAKE_CONCERTS = [
    ("Queen", "Live at Wembley", 1986, "Rock", 118), ("Dire Straits", "Alchemy Live", 1983, "Rock", 95),
    ("Talking Heads", "Stop Making Sense", 1984, "New Wave", 88), ("Iron Maiden", "Live After Death", 1985, "Metal", 90),
    ("Prince", "Sign o' the Times", 1987, "Funk", 85), ("The Who", "Live at Shea", 1982, "Rock", 100),
]

_DURATION_TEMPLATES: dict[int, Path] = {}
_TEMPLATE_DIR: Path | None = None   # <library root>/.templates, set by build_fake_library


def _make_video(dest: Path, seconds: int, text: str) -> None:
    """Create (or copy from a cached template of the same length) a tiny video file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmpl = _DURATION_TEMPLATES.get(seconds)
    if tmpl is None or not tmpl.exists():
        tmpl = (_TEMPLATE_DIR or dest.parent / ".templates") / f"{seconds}.mp4"
        tmpl.parent.mkdir(parents=True, exist_ok=True)
        if not tmpl.exists():
            # 4:3 colour bars with a running timecode and the clip length, so the preview window
            # shows that the live offset is right; 1 fps keeps a 25-minute file around 100 KB.
            mins, secs = divmod(seconds, 60)
            label = f"PiTV test signal  %{{pts\\:gmtime\\:0\\:%H\\:%M\\:%S}} of {mins:02d}\\:{secs:02d}"
            draw = (f"drawtext=text='{label}':fontsize=18:fontcolor=white:box=1:boxcolor=black@0.6:"
                    "x=(w-text_w)/2:y=h-40")
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                 "-i", f"smptebars=s=320x240:r=1:d={seconds}", "-vf", draw,
                 "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage", "-crf", "35",
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


class _Index:
    """Collects a schema 2 library index (docs/CONTENT_CONTRACT.md) while files are written, the
    way pitv_content would publish it after indexing the NAS."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.sources: list[dict] = []
        self.shows: list[dict] = []
        self.items: list[dict] = []

    def source(self, sid: str, name: str, stype: str, folder: Path, category: str = "general") -> None:
        self.sources.append({"id": sid, "name": name, "type": stype, "category": category, "root": str(folder),
                             "remote": f"smb://fakenas/{sid}/", "location": "nas", "enabled": True})

    def show(self, sid: str, folder: Path, title: str, year: int | None, genres: list[str],
             certificate: str | None, category: str = "general") -> str:
        uid = f"show:{sid}:{folder.name}"
        self.shows.append({"uid": uid, "source": sid, "title": title, "year": year, "genres": genres,
                           "certificate": certificate, "plot": f"{title}, first shown in {year}." if year else None,
                           "category": category})
        return uid

    def item(self, sid: str, path: Path, seconds: int, kind: str, title: str, **meta) -> None:
        rel = path.relative_to(Path(next(s["root"] for s in self.sources if s["id"] == sid)))
        self.items.append({"uid": f"nas:{sid}:{rel.as_posix()}", "source": sid, "kind": kind, "title": title,
                           "duration": float(seconds), "vcodec": "h264", "acodec": None, "width": 320, "height": 240,
                           "interlaced": False, "size": path.stat().st_size, "mtime": int(path.stat().st_mtime),
                           "path": str(path), "genres": [], "certificate": None, "plot": None, **meta})

    def document(self) -> dict:
        return {"schema": 2, "generated_ts": int(__import__("time").time()), "complete": True,
                "sources": self.sources, "shows": self.shows, "items": self.items}


def build_fake_library(root: Path, seed: int = 1, with_nfo: bool = True,
                       max_episodes_per_show: int | None = None) -> dict[str, Any]:
    """Write a small library of real (tiny) video files and the library index pitv_content would
    publish for it. Returns the folders plus `index` (the document) and `index_path`."""
    import datetime as _dt
    import json as _json
    global _TEMPLATE_DIR
    rnd = random.Random(seed)
    _TEMPLATE_DIR = root / ".templates"
    _DURATION_TEMPLATES.clear()
    tv, movies, pitv = root / "tvshows", root / "movies", root / "pitv"
    sport, music = root / "tvsports", root / "music videos"
    for folder in (tv, movies, pitv / "Adverts", pitv / "Idents", sport, music):
        folder.mkdir(parents=True, exist_ok=True)
    idx = _Index(root)
    idx.source("tvshows", "TV Shows", "tv", tv)
    idx.source("tvsports", "Sport", "tv", sport, "sport")
    idx.source("movies", "Movies", "movie", movies)
    idx.source("ads", "Adverts", "advert", pitv / "Adverts")
    idx.source("idents", "Idents", "ident", pitv / "Idents")
    idx.source("musicvideos", "Music videos", "music", music)

    for title, year, seasons, eps, minutes, genres, cert, _kids in FAKE_SHOWS:
        safe = title.replace(":", "").replace("/", "-")
        show_dir = tv / f"{safe} ({year})"
        show_dir.mkdir(exist_ok=True)
        if with_nfo:
            _nfo(show_dir / "tvshow.nfo", "tvshow",
                 {"title": title, "year": year, "premiered": f"{year}-09-01", "mpaa": f"UK:{cert}",
                  "plot": f"{title}, first shown in {year}."}, genres)
        show_uid = idx.show("tvshows", show_dir, title, year, list(genres), cert)
        count = 0
        for s in range(1, seasons + 1):
            for e in range(1, eps + 1):
                if max_episodes_per_show and count >= max_episodes_per_show:
                    break
                f = show_dir / f"Season {s:02d}" / f"{safe} - S{s:02d}E{e:02d} - Episode {e}.mp4"
                seconds = (minutes + rnd.choice([-2, -1, 0, 0, 0, 1])) * 60
                _make_video(f, seconds, f"{title} S{s}E{e}")
                idx.item("tvshows", f, seconds, "episode", f"Episode {e}", show_uid=show_uid, season=s, episode=e,
                         year=year + s - 1)
                count += 1

    for title, year, dated, eps, minutes in FAKE_SPORT:
        show_dir = sport / f"{title} ({year})"
        show_dir.mkdir(exist_ok=True)
        show_uid = idx.show("tvsports", show_dir, title, year, ["Sport"], "U", "sport")
        day = _dt.date(1985, 1, 5)
        for e in range(1, eps + 1):
            if dated:
                f = show_dir / "Season 1985" / f"{title} - S1985E{e:02d} - {day.isoformat()}.mp4"
                label, season, ep_year = day.strftime("%d/%m/%Y"), 1985, 1985
                day += _dt.timedelta(days=7)
            else:
                f = show_dir / "Season 01" / f"{title} - S01E{e:02d} - Episode {e}.mp4"
                label, season, ep_year = f"Episode {e}", 1, year
            _make_video(f, minutes * 60, title)
            idx.item("tvsports", f, minutes * 60, "episode", label, show_uid=show_uid, season=season, episode=e,
                     year=ep_year, genres=["Sport"])

    for artist, title, year, genre, minutes in FAKE_MUSIC:
        f = music / genre / f"{artist} - {title} ({year}).mp4"
        _make_video(f, minutes * 60, title)
        idx.item("musicvideos", f, minutes * 60, "music", f"{artist} - {title}", artist=artist, year=year,
                 genres=[genre], concert=False)
    for artist, title, year, genre, minutes in FAKE_CONCERTS:
        f = music / "Concerts" / genre / f"{artist} - {title} ({year}).mp4"
        _make_video(f, minutes * 60, title)
        idx.item("musicvideos", f, minutes * 60, "music", f"{artist} - {title}", artist=artist, year=year,
                 genres=[genre], concert=True)

    for title, year, minutes, cert, genres in FAKE_MOVIES:
        safe = title.replace(":", "").replace("/", "-")
        folder = movies / (f"{safe} ({year})" if year else safe)
        f = folder / (f"{safe} ({year}).mp4" if year else f"{safe}.mp4")
        _make_video(f, minutes * 60, title)
        if with_nfo and year:
            _nfo(f.with_suffix(".nfo"), "movie", {"title": title, "year": year, "mpaa": f"UK:{cert}",
                                                  "plot": f"{title} ({year})."}, genres)
        idx.item("movies", f, minutes * 60, "movie", title, year=year, certificate=cert, genres=list(genres),
                 plot=f"{title} ({year})." if year else None)

    for title, year in FAKE_ADVERTS:
        f = pitv / "Adverts" / str(year) / f"{title}.mp4"
        seconds = rnd.choice([20, 30, 30, 40, 60])
        _make_video(f, seconds, title)
        idx.item("ads", f, seconds, "advert", title, year=year)   # family_safe left to PiTV's keyword rule

    for ch in (1, 2, 3, 4):
        for i in (1, 2):
            f = pitv / "Idents" / f"ch{ch}" / f"Ident {i}.mp4"
            seconds = rnd.choice([8, 10, 15])
            _make_video(f, seconds, f"ch{ch}")
            idx.item("idents", f, seconds, "ident", f"Ident {i}", channel_hint=ch)
    _make_video(pitv / "Static" / "static.mp4", 2, "static")

    doc = idx.document()
    index_path = root / "library.json"
    index_path.write_text(_json.dumps(doc, indent=1))
    return {"tv": tv, "movies": movies, "pitv": pitv, "sport": sport, "music": music, "index": doc,
            "index_path": index_path}
