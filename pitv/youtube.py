"""Naming a YouTube channel or playlist so pitv_content can fetch from it.

A channel added to the catalogue is a series whose episodes are its videos: an ordinary line-up
entry whose confirmed identity names the channel instead of a television database. Everything
after that is the machinery a series already has, and the episode number PiTV keeps is the
position in the listing.

Two things decide what is sent. pitv_content fetches the `url`, so that is always sent and is
what must be right. The `id` is for its own state, and a creator can rename a handle at will:
a handle stops resolving the day it is renamed, and a series keyed on it would quietly stop
with no error anyone reads. So YouTube's own channel id (`UC...`) is sent where the
address carries one, and the handle only where it does not.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

SOURCE = "youtube_channel"   # pitv_content's provider type; a playlist is the same source

_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
_CHANNEL = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_PLAYLIST = re.compile(r"^(?:PL|UU|FL|OL|RD)[A-Za-z0-9_-]{10,}$")
_HANDLE = re.compile(r"^@[A-Za-z0-9._-]{3,30}$")
# A vanity address from before handles: youtube.com/<name>, with no /c/ or /user/ in front of
# it. These are still the address a creator prints, so they are still what somebody copies.
_VANITY = re.compile(r"^[A-Za-z0-9_-]{3,50}$")
# First segments of youtube.com that are the site rather than somebody's channel. Without this
# a link to a video would be read as a channel called "watch" and quietly fetch nothing.
_NOT_A_CHANNEL = frozenset({
    "watch", "playlist", "shorts", "live", "embed", "feed", "results", "hashtag", "about",
    "account", "premium", "gaming", "music", "movies", "sports", "news", "upload", "post",
    "clip", "oembed", "redirect", "t", "source", "howyoutubeworks", "creators", "ads",
})
# YouTube's own browse id for a playlist: the playlist id with VL in front of it, which is what
# a "show" address carries. What can actually be listed is the playlist inside it.
_BROWSE_LIST = re.compile(r"^VL(?P<list>[A-Za-z0-9_-]{10,})$")


def _playlist(listed: str) -> dict[str, str]:
    return {"source": SOURCE, "id": listed, "url": f"https://www.youtube.com/playlist?list={listed}"}


def parse(url: str) -> dict[str, str] | None:
    """The confirmed identity for a channel or playlist address, or None when it is neither.

    A playlist is the same source: pitv_content lists both the same way and reads the order from
    what it was given, a channel reversed to oldest-first and a playlist in the order its maker
    chose. A playlist is the better thing to point at where one exists, because it survives the
    channel being reorganised and it is the creator saying what order the thing goes in."""
    # Every space is dropped rather than the ends trimmed: an address copied out of a message
    # that wrapped it arrives with the break inside the id, and a channel id with a space in it
    # matches nothing and reads as "not a YouTube address" when it plainly is one.
    text = re.sub(r"\s+", "", url or "")
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if parts.hostname not in _HOSTS:
        return None
    listed = parse_qs(parts.query).get("list", [None])[0]
    if listed and _PLAYLIST.match(listed):
        return _playlist(listed)
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) >= 2 and segments[0] in ("show", "playlist"):
        # A series presented as a show. The address carries a browse id rather than a plain
        # playlist id, and the query string is whatever YouTube was tracking at the time.
        browse = _BROWSE_LIST.match(segments[1])
        inner = browse.group("list") if browse else segments[1]
        if _PLAYLIST.match(inner):
            return _playlist(inner)
    if segments and _HANDLE.match(segments[0]):
        return {"source": SOURCE, "id": segments[0], "url": f"https://www.youtube.com/{segments[0]}"}
    if len(segments) >= 2 and segments[0] == "channel" and _CHANNEL.match(segments[1]):
        return {"source": SOURCE, "id": segments[1], "url": f"https://www.youtube.com/channel/{segments[1]}"}
    if len(segments) >= 2 and segments[0] in ("c", "user") and segments[1]:
        # A legacy vanity address. It has no stable id in it, so the address is all there is.
        return {"source": SOURCE, "id": segments[1], "url": f"https://www.youtube.com/{segments[0]}/{segments[1]}"}
    if len(segments) == 1 and segments[0].lower() not in _NOT_A_CHANNEL and _VANITY.match(segments[0]):
        return {"source": SOURCE, "id": segments[0], "url": f"https://www.youtube.com/{segments[0]}"}
    return None


def survives_a_rename(match: object) -> bool:
    """Whether a confirmed identity is keyed on something its creator cannot change.

    A channel id (`UC...`) and a playlist id are permanent. A handle or a vanity name is the
    creator's to change at will, and the day they do, the address stops resolving: the fetches
    fail one by one with nothing to say why, and a series that has run for months simply stops.
    Both forms are valid to fetch from, so this is not an error; it is the difference between an
    entry that will still work in a year and one that depends on nobody renaming anything."""
    if not is_channel(match):
        return False
    ident = str((match or {}).get("id") or "")
    return bool(_CHANNEL.match(ident) or _PLAYLIST.match(ident))


def same_channel(stored: object, candidate: object) -> bool:
    """Whether a lookup candidate is certainly the channel an entry already names.

    The comparison is on the handle, never on the title. Two creators can share a name and a
    search asked for one will happily return the other, which is the fault this exists to
    prevent rather than to introduce: an entry silently repointed at somebody else would fetch
    their videos under the right name and nothing would say so. A handle is unique while it
    lasts, so a candidate carrying the handle we already hold is that channel and its permanent
    id can be taken. Anything less certain is left alone for a person to look at."""
    if not (is_channel(stored) and isinstance(candidate, dict)):
        return False
    ours = str((stored or {}).get("id") or "").lstrip("@").lower()
    theirs = str(candidate.get("uploader") or "").lstrip("@").lower()
    return bool(ours) and ours == theirs


def is_channel(match: object) -> bool:
    """Whether a line-up entry's confirmed identity names a channel or playlist."""
    return isinstance(match, dict) and match.get("source") == SOURCE
