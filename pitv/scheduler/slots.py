"""What a schedule is made of: slots, the series they come from, and how they are titled.

Everything here is plain data with no policy in it, so the modules that decide things (the
library loader, the selector, the bands, the day walk, the horizon) all share one vocabulary
without depending on each other."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .clock import WEEK

FILLER_TITLE = "Programmes will continue shortly"


@dataclass
class Slot:
    channel_id: int
    day: str
    start_ts: int
    end_ts: int
    media_id: int | None
    offset: float
    kind: str
    title: str
    subtitle: str = ""
    replay: int = 0
    locked: int = 0
    block: str | None = None
    wanted_id: int | None = None
    wanted_spec: dict[str, Any] | None = None   # external entry placed; a wanted row is created on save
    # not persisted
    show_id: int | None = None
    genres: list[str] = field(default_factory=list)
    year: int | None = None

    @property
    def duration(self) -> int:
        return self.end_ts - self.start_ts


@dataclass
class Show:
    id: int
    title: str
    year: int | None
    home_channel_id: int | None
    mode: str
    anchor_time: str | None
    anchor_days: list[int]
    rest_weeks: int
    episodes: list[dict[str, Any]]      # sorted by season, episode
    end_year: int | None = None          # premiere year + seasons - 1 (approximation)
    category: str = "general"
    pinned: bool = False                 # explicitly assigned by the admin, not auto-routed
    ptype: str = "series"                # what it is (genres.PROGRAMME_TYPES); decides who may borrow it
    next_index: int = 0
    resting_until: int | None = None
    held: list[int] = field(default_factory=list)   # episodes passed over by a run; offered first next time

    @property
    def anchored(self) -> bool:
        return self.mode in ("strip", "weekly") and bool(self.anchor_time)

    def next_episode(self) -> dict[str, Any] | None:
        if not self.episodes:
            return None
        if self.held:
            return self.episodes[self.held[0]]
        return self.episodes[self.next_index % len(self.episodes)]

    def advance(self, ts: int) -> None:
        """Move past the episode placed at `ts`; after the last one the series rests."""
        if self.held:
            self.held.pop(0)
            return
        self.next_index += 1
        if self.next_index >= len(self.episodes):
            self.next_index = 0
            self.resting_until = ts + self.rest_weeks * WEEK

    def take_short(self, max_seconds: float, room: float, look_ahead: int = 6) -> dict[str, Any] | None:
        """The next episode short enough to join a run, passing over longer ones.

        A series of five minute cartoons often carries the odd full-length special, and stopping
        the run at the first of those leaves a five minute programme on its own. Those are held
        back instead and offered first the next time the series is placed, so nothing drops out
        of the rotation. An episode that is short enough but does not fit the `room` left before
        a fixed boundary is different: it ends the run and stays next in line, so episodes keep
        their order. Works forward from the cursor and never touches what is already held."""
        if not self.episodes:
            return None
        for _ in range(min(look_ahead, len(self.episodes))):
            index = self.next_index % len(self.episodes)
            episode = self.episodes[index]
            if seconds(episode) < max_seconds:
                if seconds(episode) > room:
                    return None
                self.next_index = (index + 1) % len(self.episodes)
                return episode
            if index not in self.held:
                self.held.append(index)
            self.next_index = index + 1
            if self.next_index >= len(self.episodes):
                self.next_index = 0
                break        # a run does not carry the series round into a second pass
        return None


def episode_subtitle(item: dict[str, Any]) -> str:
    """The episode's own title; falls back to 'Episode N' when the file has no title."""
    title = (item.get("title") or "").strip()
    if title:
        return title
    if item.get("episode") is not None:
        return f"Episode {int(item['episode'])}"
    return ""


def slot_titles(item: dict[str, Any], show_title: str | None = None) -> tuple[str, str]:
    """Guide title and subtitle for a programme. Episodes: the series name over the episode
    title (never the SxxEyy code). Films: '(year) certificate'. Music videos: '(year) genres'."""
    year = f"({item['year']})" if item.get("year") else ""
    if item.get("kind") == "episode":
        # A band places an episode with no series beside it, so the series name travels on the
        # item. Without it the guide billed a whole evening as "Episode 2".
        return show_title or item.get("show_title") or item["title"], episode_subtitle(item)
    if item.get("kind") == "music":
        detail = ", ".join(json_field(item.get("genres")) or [])
    else:
        detail = item.get("certificate") or ""
    return item["title"], " ".join(x for x in (year, detail) if x)


def parse_day(value: str) -> date:
    """A broadcast day as written in the schedule and the API (YYYY-MM-DD)."""
    return date.fromisoformat(value)


def json_field(value: Any) -> Any:
    """A column stored as JSON text (or already decoded by row_to_dict); unreadable -> None."""
    if isinstance(value, str):
        if not value:
            return None
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def seconds(item: dict[str, Any]) -> int:
    """Slot length for a file: whole seconds, never zero so a slot always has an end."""
    return max(1, round(float(item["duration"])))


def programme_slot(channel_id: int, day_str: str, start: int, item: dict[str, Any],
                   show: Show | None = None, block: str | None = None) -> Slot:
    title, subtitle = slot_titles(item, show.title if show else None)
    return Slot(channel_id=channel_id, day=day_str, start_ts=start, end_ts=start + seconds(item),
                media_id=item["id"], offset=0, kind="programme", title=title, subtitle=subtitle,
                show_id=show.id if show else None, genres=item.get("genres") or [],
                year=item.get("year"), block=block)


def media_slot(channel_id: int, day_str: str, start: int, item: dict[str, Any], kind: str) -> Slot:
    """An advert or ident slot; the subtitle is just the year."""
    return Slot(channel_id=channel_id, day=day_str, start_ts=start, end_ts=start + seconds(item),
                media_id=item["id"], offset=0, kind=kind, title=item["title"],
                subtitle=str(item.get("year") or ""), year=item.get("year"))


def filler_slot(channel_id: int, day_str: str, start: int, end: int, title: str = FILLER_TITLE,
                **extra: Any) -> Slot:
    """A caption with no file behind it: the player shows the holding card under the title."""
    return Slot(channel_id=channel_id, day=day_str, start_ts=start, end_ts=end, media_id=None,
                offset=0, kind="filler", title=title, **extra)
