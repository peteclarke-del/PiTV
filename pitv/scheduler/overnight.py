"""The small hours: what a channel shows between closedown and the next broadcast day.

A channel with a pattern replays its own day, as the old overnight services did, looping a
short day and taking care not to butt the same series against the day's end or tomorrow's
opening. A channel built from bands has no day worth replaying as a whole; it carries on from
its own material with the repeat gaps still holding, so the night brings what has not just
aired rather than the same concert three times."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from . import bands
from .clock import broadcast_ts
from .policy import SchedulerPolicy
from .slots import Slot

MAX_LOOPS = 12      # a thin day is replayed again until morning, but not forever


def replay(channel: dict[str, Any], day: date, day_end: int, next_day_start: int, day_slots: list[Slot], *,
           policy: SchedulerPolicy, tz: ZoneInfo, day_start_min: int,
           next_day_slots: Callable[[], list[Slot]], tomorrow_first: int | None,
           caption: Callable[[int, int], Slot], ident: Callable[[int, int], Slot | None],
           leave_out: Callable[[Slot], bool] | None = None) -> list[Slot]:
    """Replay the day from the channel's `overnight_replay_from` until the next day starts.
    `leave_out` says which of the day's slots are not to be repeated: what has been withdrawn
    since it aired, and what may not air in the small hours at all (children's programmes on a
    channel that keeps the cutoff).

    `next_day_slots` is consulted only when there is nothing to replay: a day built before the
    library had anything in it. Showing a caption until morning is worse than opening tomorrow
    early. `tomorrow_first` is the series tomorrow opens with, which the replay must not end
    on. `caption` makes the closedown card for whatever is left.

    `ident` makes the channel's ident for a gap, or None. The handover from the day into the
    small hours is a break like any other and is announced like one: the day's last programme
    ends, the ident says whose channel this is, and the replay begins. Without it the night
    began mid-sentence, cutting from the end of one programme straight into another."""
    day_str = day.isoformat()
    replay_from = channel.get("overnight_replay_from") or policy.day_start
    from_ts = broadcast_ts(day, replay_from, day_start_min, tz)
    ordered = sorted(day_slots, key=lambda s: s.start_ts)
    # What is left out takes the break that followed it along, as a programme that would run into
    # tomorrow's opener does below: otherwise two breaks close up around the gap and the small
    # hours show a run of adverts with two idents in it.
    source, dropping = [], False
    for s in ordered:
        if s.start_ts < from_ts or s.kind == "filler":
            continue
        if s.kind == "programme":
            dropping = bool(leave_out and leave_out(s))
        if not dropping and not (leave_out and leave_out(s)):
            source.append(s)
    if not source:
        source = next_day_slots()
    # A break only makes sense attached to a programme. Starting a replay part-way through the
    # source day must not begin with the adverts that preceded its first programme.
    first_programme = next((i for i, s in enumerate(source) if s.kind == "programme"), len(source))
    source = source[first_programme:]
    # The replay follows straight on from the day's last programme: never start it with
    # another episode of that same series.
    last_prog = next((s for s in reversed(ordered) if s.kind == "programme"), None)
    if last_prog is not None and last_prog.show_id is not None:
        first = next((i for i, s in enumerate(source)
                      if s.kind == "programme" and s.show_id != last_prog.show_id), len(source))
        source = source[first:]
    out: list[Slot] = []
    t = max(day_end, max((s.end_ts for s in day_slots), default=day_end))
    # The day closed on a programme, so the handover into the night carries an ident. A day that
    # ended on a caption or a break has had its say already.
    if last_prog is not None and ordered and ordered[-1].kind == "programme" and t < next_day_start:
        opener = ident(t, next_day_start - t)
        if opener is not None:
            out.append(opener)
            t = opener.end_ts
    queue = deque(source)
    loops = 0
    suppress_break = False
    while t < next_day_start:
        if not queue:
            loops += 1
            if not source or loops > MAX_LOOPS:
                break
            queue.extend(source)
        s = queue.popleft()
        if (s.kind == "programme" and tomorrow_first is not None and s.show_id == tomorrow_first
                and t + s.duration >= next_day_start):
            # The adverts which followed this programme in the source belong to it too.
            # Suppress the whole unit, otherwise a thin channel can end with every advert
            # from the source day and no programme between them.
            suppress_break = True
            continue  # would run straight into the same series at 08:00
        if s.kind != "programme" and suppress_break:
            continue
        if s.kind == "programme":
            suppress_break = False
        elif s.kind == "ident" and _ident_in_break(out):
            continue      # one ident to a break, in the small hours as in the day
        end = min(t + s.duration, next_day_start)
        # A replayed placeholder keeps its request (wanted_id, or wanted_spec until save)
        # so the delivered file binds to the replay too.
        out.append(replace(s, day=day_str, start_ts=t, end_ts=end, replay=1, locked=0))
        t = end
    if t < next_day_start:
        out.append(caption(t, next_day_start))
    return out


def _ident_in_break(out: list[Slot]) -> bool:
    """Whether the break now being replayed already has its ident."""
    for s in reversed(out):
        if s.kind == "ident":
            return True
        if s.kind == "programme":
            return False
    return False


def from_pool(channel_id: int, kinds: tuple[str, ...], decades: tuple[int, ...], day_end: int,
              next_day_start: int, day_slots: list[Slot], filler: bands.Filler) -> tuple[list[bands.Placement], int]:
    """The small hours on a channel built from bands: more of its own material, chosen the same
    way as its free time, and where it ends. What is left is the closedown caption."""
    start = max(day_end, max((s.end_ts for s in day_slots), default=day_end))
    return bands.fill_free(channel_id, kinds, decades, start, next_day_start, filler)
