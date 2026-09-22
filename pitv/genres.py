"""One spelling per genre, everywhere PiTV stores or compares one.

Genres reach PiTV from several places: pitv_content's index, a title added by hand in the admin,
an override typed into a show editor, a channel's allowed list, a band's fill. The same genre
arrives as "Sci-Fi" and "Science Fiction", "Kids" and "Children's", "cartoon" and "Animation".
A channel that allows Children then misses a series tagged Kids, which is how an added title
quietly never airs.

So every genre is brought to one canonical name as it is read, by `db.genre_list`, which is the
single door genres come through. Comparisons elsewhere are then a plain case-insensitive match
between names that already agree. The table mirrors pitv_content's own (`pitv_content/genres.py`)
so both applications call the same thing by the same name; when one side adds a spelling, the
other follows.

A name this table has never seen keeps its own words in title case, rather than being dropped:
an unusual genre is still a genre, and it stays consistent between files.
"""

from __future__ import annotations

import re

# Spellings that mean a genre already in use. Keys are compared with case, punctuation and
# spacing removed, so "Sci-Fi", "sci fi" and "SCIFI" all match "scifi".
ALIASES: dict[str, str] = {
    "scifi": "Science Fiction", "sciencefiction": "Science Fiction", "sf": "Science Fiction",
    "childrens": "Children", "children": "Children", "kids": "Children", "childrenstv": "Children",
    "childrensprogramme": "Children", "childrensprogrammes": "Children", "child": "Children",
    "family": "Family",
    "cartoon": "Animation", "cartoons": "Animation", "animated": "Animation", "animation": "Animation",
    "docu": "Documentary", "docs": "Documentary", "documentaries": "Documentary", "factual": "Documentary",
    "gameshow": "Game Show", "gameshows": "Game Show", "quiz": "Game Show", "quizshow": "Game Show",
    "sitcom": "Sitcom", "sitcoms": "Sitcom", "situationcomedy": "Sitcom", "comedysitcom": "Sitcom",
    "standup": "Comedy", "standupcomedy": "Comedy",
    "soapopera": "Soap", "soaps": "Soap", "advert": "Advert", "adverts": "Advert",
    "advertisement": "Advert", "advertisements": "Advert", "commercial": "Advert", "commercials": "Advert",
    "musicvideo": "Music", "musicvideos": "Music", "musical": "Musical",
    "rnb": "R&B", "rb": "R&B", "randb": "R&B", "rhythmandblues": "R&B", "rhythmblues": "R&B",
    "hiphop": "Hip Hop", "rap": "Rap", "synth": "Synth Pop", "synthpop": "Synth Pop", "electropop": "Synth Pop",
    "newwave": "New Wave", "postpunk": "Post Punk", "hardrock": "Hard Rock", "heavymetal": "Metal",
    "motown": "Motown", "electronica": "Electronic", "electronic": "Electronic", "dancepop": "Dance",
    "soccer": "Football", "prowrestling": "Wrestling", "professionalwrestling": "Wrestling",
    # "Sports" and "Sport" were two genres, so a channel allowing one missed everything tagged
    # the other, which is the split this table exists to prevent.
    "sports": "Sport", "sporting": "Sport",
    "motorsports": "Motorsport", "motorracing": "Motorsport", "motorsport": "Motorsport",
    "trackandfield": "Athletics", "figureskating": "Ice Skating", "iceskating": "Ice Skating",
    "horseracing": "Horse Racing", "suspense": "Thriller", "warfilm": "War", "warmovie": "War",
    "news": "News", "currentaffairs": "News", "entertainment": "Entertainment", "variety": "Entertainment",
    # "Reality" and "Reality TV" are the same thing and were two genres, so a band asking for one
    # silently missed everything tagged the other. The longer name is the canonical one because
    # the rule that a reality show is not sport is written against it.
    "reality": "Reality TV", "realitytv": "Reality TV", "realityshow": "Reality TV", "docusoap": "Reality TV",
    "education": "Education", "educational": "Education", "science": "Science", "technology": "Technology",
    "scienceandtechnology": "Science", "popular": "Pop", "pop": "Pop",
    # Cookery and food programmes. "Food & Drink" is split on the spaced ampersand before it gets
    # here, so "drink" is listed too or half the tag would become a genre of its own.
    "food": "Food", "foods": "Food", "fooddrink": "Food", "foodanddrink": "Food", "drink": "Food",
    "cooking": "Food", "cookery": "Food", "cookingshow": "Food", "culinary": "Food",
    "baking": "Food", "gastronomy": "Food",
    # Not "came from YouTube", which would be most of the library: this marks material curated
    # from a named channel or playlist somebody chose, so a channel can be built from it and
    # told apart from everything the ordinary search finds. A band collecting such material
    # matches on this, so the spellings have to agree: "YouTube", "You Tube", "youtube" and
    # "yt" otherwise canonicalise three different ways and a band silently matches nothing.
    "youtube": "YouTube", "yt": "YouTube", "ytchannel": "YouTube", "youtubechannel": "YouTube",
}

# Children's programming, however it is labelled: what marks a series as one for children and
# what a cartoon channel is built from. Canonical names, so the lists are short and exact.
CHILDRENS = ("Animation", "Anime", "Children", "Family")
CARTOONS = ("Animation", "Anime")
SCHEDULING_CLASSES = ("general", "sport")
# A story about sport is not sport, and nor is a quiz played with darts. Metadata sites tag
# "Ted Lasso" and "Twisted Metal" as Sport among their other genres, and "Bullseye" as Sports
# and Game Show; scheduled as sport they would fill a Saturday afternoon back to back. A series
# carrying any of these is something else, whatever it is also tagged.
SCRIPTED = ("Drama", "Comedy", "Action", "Adventure", "Fantasy", "Science Fiction", "Thriller", "Crime",
            "Mystery", "Horror", "Romance", "Soap", "Western", "War", "Supernatural", "Animation", "Anime",
            "Game Show", "Reality TV")

# What a thing of each kind can be. A genre belongs to the kinds it makes sense for: a film is
# never Synth Pop and a music video is never a Sitcom, and offering both to both is how a picker
# becomes a list nobody reads. The three lists are meant to be comprehensive for their kind
# without being exhaustive: enough to describe a library of this period, not every word a
# metadata site has ever used, since anything unusual can still be typed in where the picker
# allows it.
_STORY = ("Action", "Adventure", "Animation", "Anime", "Biography", "Comedy", "Crime", "Documentary",
          "Drama", "Family", "Fantasy", "History", "Horror", "Musical", "Mystery", "Romance",
          "Science Fiction", "Supernatural", "Thriller", "War", "Western")
# A film is a story, plus the two labels that describe the film rather than the story.
# "Sport" describes what a film is about, as in a boxing documentary; what a programme is
# scheduled as is its programme type, not its genre, so a drama about football stays a drama.
# "Adult" is a marker the watershed reads, so it has to be sayable here.
FILM_GENRES = tuple(sorted({*_STORY, "Adult", "Music", "Short", "Sport", "TV Movie"}))
# Television is stories too, plus everything that is not one: the formats, the subjects a
# factual programme is about, and the sports a broadcast covers. A curated channel's videos are
# television as far as the scheduler is concerned, so they draw on this list.
_FORMATS = ("Children", "Game Show", "Magazine", "Mini-series", "News", "Reality TV", "Sitcom", "Soap", "Talk")
_SUBJECTS = ("Adult", "Concert", "DIY", "Education", "Entertainment", "Food", "Informational", "Medical",
             "Music", "Nature", "Science", "Technology", "Travel", "YouTube")
_SPORTS = ("Athletics", "Boxing", "Cricket", "Darts", "Football", "Golf", "Horse Racing", "Ice Skating",
           "Motorsport", "Rugby", "Snooker", "Sport", "Tennis", "Wrestling")
SERIES_GENRES = tuple(sorted({*_STORY, *_FORMATS, *_SUBJECTS, *_SPORTS}))
# Music videos are described by the music, never by a story.
MUSIC_GENRES = ("Blues", "Concert", "Country", "Dance", "Disco", "Electronic", "Folk", "Funk", "Hard Rock",
                "Hip Hop", "Indie", "Jazz", "Metal", "Motown", "New Wave", "Pop", "Post Punk", "Punk", "R&B",
                "Rap", "Reggae", "Rock", "Ska", "Soul", "Synth Pop")
# The kinds the admin counts and offers by (`lineup.FACET_KINDS`).
BY_KIND: dict[str, tuple[str, ...]] = {"episode": SERIES_GENRES, "movie": FILM_GENRES, "music": MUSIC_GENRES}


def satisfied_by(wanted: object, families: object) -> set[str]:
    """Every genre name that satisfies `wanted`, itself included, folded to canonical spellings.

    A family widens and never narrows, so the wanted name is always in the answer however the
    configuration is written. It is read per genre rather than as a rule about which genres are
    alike: a Metal band is happy with Black Sabbath, whom most sources call Hard Rock, while a
    Hard Rock band is not necessarily happy with death metal."""
    name = canonical(wanted)
    if name is None:
        return set()
    out = {name.lower()}
    table = families if isinstance(families, dict) else {}
    for near in table.get(name) or []:
        if (folded := canonical(near)) is not None:
            out.add(folded.lower())
    return out


def for_kinds(kinds: object) -> tuple[str, ...]:
    """Every genre that makes sense for any of these kinds; all of them when none is named."""
    wanted = [k for k in (kinds if isinstance(kinds, (list, tuple, set)) else []) if k in BY_KIND]
    if not wanted:
        return KNOWN
    return tuple(sorted({g for k in wanted for g in BY_KIND[k]}))


# Every genre this table can name, whether or not the library holds any of it. The admin offers
# these when setting up a channel or a band, because a band asking for something absent is how
# material gets collected in the first place: offering only what is already here makes a channel
# of new material impossible to configure.
KNOWN: tuple[str, ...] = tuple(sorted({*ALIASES.values(), *CHILDRENS, *CARTOONS, *SCRIPTED,
                                       *FILM_GENRES, *SERIES_GENRES, *MUSIC_GENRES}))

# Words that keep their own case inside a title-cased name.
_LOWER = {"and", "of", "the", "in", "on", "de", "la"}
_KEY = re.compile(r"[^a-z0-9]+")
# One tag, several genres: a slash, a comma, a semicolon or a spaced ampersand separates them.
# "R&B" survives, because only a spaced ampersand separates.
_SEPARATORS = re.compile(r"\s*[/,;]\s*|\s+&\s+")


def canonical(name: object) -> str | None:
    """One canonical genre name, or None when there is nothing usable in it."""
    if not isinstance(name, str):
        return None
    text = re.sub(r"\s+", " ", name.replace("_", " ")).strip(" \t.,;:/|-\"'")
    if not text or text.isdigit():
        return None
    known = ALIASES.get(_KEY.sub("", text.casefold()))
    if known:
        return known
    words = [w if w.isupper() and len(w) > 1 else w.capitalize() for w in text.split(" ")]
    return " ".join([words[0], *(w.lower() if w.lower() in _LOWER else w for w in words[1:])])


def canonical_all(names: object) -> list[str]:
    """A list of genres, canonical, in order, without repeats; a tag holding several is split."""
    if isinstance(names, str):
        names = [names]
    out: list[str] = []
    for name in names if isinstance(names, (list, tuple)) else []:
        parts = _SEPARATORS.split(name) if isinstance(name, str) else [name]
        for part in parts or [name]:
            genre = canonical(part)
            if genre and genre not in out:
                out.append(genre)
    return out


def matches(names: object, wanted: object) -> bool:
    """Whether any of `names` is one of `wanted`, both read canonically. The comparison every
    channel, band and rule makes, in one place."""
    have = {g.casefold() for g in canonical_all(names)}
    return bool(have & {g.casefold() for g in canonical_all(wanted)})


def is_childrens(names: object) -> bool:
    """Whether canonical programme genres describe material made for children."""
    return matches(names, CHILDRENS)


def is_cartoon(names: object) -> bool:
    """Cartoon is derived content metadata, never a channel or scheduling category."""
    return matches(names, CARTOONS)


def scheduling_class(value: object, names: object = ()) -> str:
    """The one coarse scheduler classification that is not already represented elsewhere.

    Old databases and indexes used ``kids`` and ``cartoon`` as category values.  They are
    audience/genre facts and therefore collapse to general; sport remains distinct because it
    changes daypart weighting and back-to-back rules.

    A source the owner set up for sport (the sports share) is sport whatever its files are
    tagged. Elsewhere a Sport tag counts only on something that is not scripted.
    """
    if str(value or "").strip().casefold() == "sport":
        return "sport"
    return "sport" if matches(names, ("Sport",)) and not matches(names, SCRIPTED) else "general"



# --- what a programme is ----------------------------------------------------------------------
# A programme's type says what it is; its genres say what it is about. The type, and only the
# type, decides which channel theme it belongs to: a crime drama is a series however many of a
# documentary channel's subjects (Crime, History, Science) its genres happen to name.
PROGRAMME_TYPES = ("film", "series", "documentary", "cartoon", "music", "sport")

# The types a channel theme (`channels.content`) takes as its own. A theme that is not listed
# (children's) is an audience, not a type, and is matched on the kids flag instead.
THEME_TYPES: dict[str, tuple[str, ...]] = {
    "general": ("film", "series"),
    "films": ("film",),
    "documentaries": ("documentary",),
    "cartoons": ("cartoon",),
    "music": ("music",),
    "sport": ("sport",),
}


def programme_type(kind: object, names: object = (), category: object = None, chosen: object = None) -> str:
    """What a programme is. `chosen` is the owner's word (an override, or the type picked when
    the title was added) and wins. Otherwise: a music video is music; what comes from a sport
    source is sport; a Documentary tag makes a documentary, of an animated film or of a
    motorcycle race alike; an unscripted Sport tag makes sport; Animation or Anime makes a
    cartoon; anything else is a film or a series by what kind of file it is. `names` is a list
    of genre names, not a JSON column."""
    if isinstance(chosen, str) and chosen.strip().casefold() in PROGRAMME_TYPES:
        return chosen.strip().casefold()
    kind = str(kind or "").casefold()
    if kind == "music":
        return "music"
    if scheduling_class(category) == "sport":
        return "sport"
    if matches(names, ("Documentary",)):
        return "documentary"
    if scheduling_class(category, names) == "sport":
        return "sport"
    if matches(names, CARTOONS):
        return "cartoon"
    return "film" if kind == "movie" else "series"
