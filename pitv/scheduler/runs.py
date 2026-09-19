"""Runs of short episodes, and the placeholder slot for material not on disk yet.

A five minute cartoon on its own leaves the day in scraps and the guide unreadable, so an
episode under `short_episode_minutes` is followed straight away by the next ones of its series,
in order, until the run reaches `short_episode_run_minutes`. The run shares the series title as
its block, so the guide shows one entry, as it does for a band. A series with nothing on disk
(a line-up entry pitv_content has yet to fetch) is run the same way, each component with its
own request so the whole programme can be prepared.

A run is remembered for the build: a cadence repeat replays the same bundle rather than
collapsing back to one five-minute episode followed by an advert break."""

from __future__ import annotations

from typing import Any

from .library import Library
from .policy import SchedulerPolicy
from .slots import Show, Slot, programme_slot, seconds


def next_episode_number(entry: dict[str, Any]) -> int | None:
    """The next episode of a remote series to ask for: the lowest number from the entry's starting
    point that no request has taken, or None when the run is known to end before it."""
    number = int(entry.get("next_episode") or 1)
    while number in entry["taken"]:
        number += 1
    count = entry.get("episode_count")
    return None if count and number > int(count) else number


class Runs:
    def __init__(self, policy: SchedulerPolicy, library: Library) -> None:
        self.policy = policy
        self.library = library

    def external_slot(self, channel_id: int, day_str: str, start: int, e: dict[str, Any]) -> Slot:
        """A programme that is not on disk yet. Episodes number on from the entry's counter; a
        wanted row raised by an earlier build for a slot since replaced is reused first."""
        duration = int(e["duration"])
        if e["kind"] == "episode":
            if e.get("_repeat_spec"):
                spec = dict(e["_repeat_spec"])
                number = int(spec["episode"] or 1)
            elif e.get("_external_repeat"):
                spec = dict(e["last_spec"])
                number = int(spec["episode"] or 1)
            elif e["spare_wanted"]:
                spare = e["spare_wanted"].pop(0)
                number, reuse = int(spare["episode"] or 1), int(spare["id"])
                spec = {"kind": "episode", "lineup_id": e["lineup_id"], "title": f"Episode {number}", "season": 1,
                        "episode": number, "year": e.get("year"), "reuse": reuse}
            else:
                number, reuse = next_episode_number(e), None
                if number is None:
                    raise ValueError(f"{e['title']}: nothing left to ask for")    # the selector does not offer it
                e["taken"].add(number)
                spec = {"kind": "episode", "lineup_id": e["lineup_id"], "title": f"Episode {number}", "season": 1,
                        "episode": number, "year": e.get("year"), "reuse": reuse}
            subtitle = f"Episode {number}"
            if not e.get("_external_repeat"):
                e["last_spec"] = dict(spec)
        else:
            # One request serves every airing of a film, so a spare is shared rather than used up.
            spare = e["spare_wanted"][0] if e["spare_wanted"] else None
            subtitle = f"({e['year']})" if e.get("year") else ""
            spec = {"kind": "movie", "lineup_id": e["lineup_id"], "title": e["title"], "season": None,
                    "episode": None, "year": e.get("year"), "reuse": int(spare["id"]) if spare else None}
        return Slot(channel_id=channel_id, day=day_str, start_ts=start, end_ts=start + duration, media_id=None,
                    offset=0, kind="programme", title=e["title"], subtitle=subtitle, show_id=None,
                    genres=e.get("genres") or [], year=e.get("year"), wanted_spec=spec,
                    replay=1 if e.get("_external_repeat") else 0)

    def external_run(self, channel: dict[str, Any], day_str: str, entry: dict[str, Any], first: Slot,
                     room: int, *, repeating: bool) -> tuple[list[Slot], int]:
        """The rest of a run of short remote episodes after `first`, and where the run ends.
        Each component gets its own request; a later cadence repeat reuses them in order."""
        threshold, target = self.policy.short_episode_seconds(channel)
        if not threshold or not target or first.duration >= threshold:
            return [], first.end_ts
        first.block = first.block or entry["title"]
        t = first.end_ts
        out: list[Slot] = []
        cached = self.library.external_short_runs.get(entry["lineup_id"]) if repeating else None
        specs = list(cached[1:]) if cached else []
        run = list(cached) if cached else [dict(first.wanted_spec or {})]
        fresh = dict(entry)
        fresh.pop("_external_repeat", None)
        fresh.pop("_repeat_spec", None)
        while t - first.start_ts < target and room >= int(entry["duration"]):
            if specs:
                component = {**entry, "_external_repeat": True, "_repeat_spec": specs.pop(0)}
            elif cached or (not fresh["spare_wanted"] and next_episode_number(fresh) is None):
                break
            else:
                component = fresh
            slot = self.external_slot(channel["id"], day_str, t, component)
            slot.block = first.block
            out.append(slot)
            if not cached:
                run.append(dict(slot.wanted_spec or {}))
                self.library.external_last_placed[entry["lineup_id"]] = t
                # each episode of a run is one more thing to fetch, and counts against the day
                self.library.external_per_day[day_str] = self.library.external_per_day.get(day_str, 0) + 1
            room -= slot.duration
            t = slot.end_ts
        if not cached:
            self.library.external_short_runs[entry["lineup_id"]] = run
        return out, t

    def series_run(self, channel: dict[str, Any], day_str: str, show: Show, first: Slot, room: int
                   ) -> tuple[list[Slot], int]:
        """The rest of a run of short episodes of one series after `first`, and where it ends.
        Component episodes are one programme for variety and daily-limit purposes."""
        threshold, target = self.policy.short_episode_seconds(channel)
        if not threshold or not target or first.duration >= threshold:
            return [], first.end_ts
        first.block = first.block or show.title
        t = first.end_ts
        out: list[Slot] = []
        placed = {first.media_id}
        while t - first.start_ts < target:
            episode = show.take_short(threshold, room)
            # A series with one short file among long ones (a trailer beside its episodes) comes
            # round to that file again; a run never shows the same episode twice.
            if episode is None or episode["id"] in placed:
                break
            placed.add(episode["id"])
            slot = programme_slot(channel["id"], day_str, t, episode, show, block=first.block)
            out.append(slot)
            self.library.last_placed[episode["id"]] = t
            room -= seconds(episode)
            t = slot.end_ts
        return out, t
