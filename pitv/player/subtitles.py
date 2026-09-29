"""Which of a file's subtitle tracks the player shows.

mpv's own choice (`--sid=auto`) depends on its version and on flags the file's author set: a
track is picked only when it is marked default or matches `--slang`, so two files with the same
subtitles behave differently. The player chooses for itself from mpv's track list, which gives
one answer for every file and lets the published state say whether anything is showing.
"""

from __future__ import annotations

from typing import Any

UNLABELLED = ("", "und", "unk", "mis", "zxx")   # what containers write when the language is not known


# Containers label a track with ISO 639-2, which has two codes for some languages and often
# shares no prefix with the two-letter code the setting holds (de: ger or deu).
THREE_LETTER = {
    "cs": ("cze", "ces"), "cy": ("wel", "cym"), "da": ("dan",), "de": ("ger", "deu"), "el": ("gre", "ell"),
    "en": ("eng",), "es": ("spa",), "fi": ("fin",), "fr": ("fre", "fra"), "ga": ("gle",), "gd": ("gla",),
    "hu": ("hun",), "it": ("ita",), "ja": ("jpn",), "ko": ("kor",), "nl": ("dut", "nld"), "no": ("nor", "nob"),
    "pl": ("pol",), "pt": ("por",), "ru": ("rus",), "sv": ("swe",), "tr": ("tur",), "zh": ("chi", "zho"),
}


def _in_language(track: dict[str, Any], language: str) -> bool:
    """Whether the track's label (en, eng, en-GB) names the two-letter `language`. A language
    missing from the table is matched by its two-letter label only."""
    lang = str(track.get("lang") or "").strip().lower().replace("_", "-").split("-")[0]
    return bool(language) and (lang == language or lang in THREE_LETTER.get(language, ()))


def choose_track(tracks: list[dict[str, Any]], language: str) -> dict[str, Any] | None:
    """The subtitle track to show from mpv's `track-list`, or None when the file has none worth
    showing. A track labelled in another language is not shown: subtitles nobody in the room
    reads are clutter, and the viewer is told there are none. An unlabelled track is given the
    benefit of the doubt, since most sidecar files carry no label at all.

    A forced track holds only the lines for dialogue in a foreign language, so it is the last
    choice: it would leave most of the programme without a caption."""
    language = (language or "").strip().lower()[:2]
    best: tuple[tuple[int, int, int], dict[str, Any]] | None = None
    for track in tracks:
        if not isinstance(track, dict) or track.get("type") != "sub" or track.get("id") is None:
            continue
        lang = str(track.get("lang") or "").strip().lower()
        if _in_language(track, language):
            match = 2
        elif lang in UNLABELLED or not language:
            match = 1
        else:
            continue
        rank = (match, 0 if track.get("forced") else 1, 1 if track.get("default") else 0)
        if best is None or rank > best[0]:   # the first of equals, which is the file's own order
            best = (rank, track)
    return best[1] if best else None


def describe(track: dict[str, Any]) -> dict[str, Any]:
    """What the published state says about the track on screen."""
    return {"id": track.get("id"), "lang": track.get("lang") or None, "codec": track.get("codec") or None,
            "external": bool(track.get("external"))}
