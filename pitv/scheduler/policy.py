"""Typed access to every tunable scheduling rule.

The builder decides *what happens next*; this module says *what the configured policy means*.
Keeping units, inheritance and timing formulae here avoids scattering setting names, fallback
values and seconds/minutes conversions through the scheduling walk.

Defaults remain authoritative in :mod:`pitv.db`.  Channel columns override a global setting
only when they are non-NULL, so ``NULL`` consistently means "inherit".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..db import DEFAULT_SETTINGS
from .rules import hhmm_to_minutes

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR
WEEK = 7 * DAY


def _field(row: Any, key: str) -> Any:
    """Read a dict/sqlite Row field without requiring either concrete type."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


@dataclass(frozen=True)
class SchedulerPolicy:
    """Resolved scheduler configuration with explicit units and inheritance."""

    settings: Mapping[str, Any]
    now: int

    def value(self, key: str) -> Any:
        return self.settings.get(key, DEFAULT_SETTINGS.get(key))

    def integer(self, key: str) -> int:
        return int(self.value(key) or 0)

    def number(self, key: str) -> float:
        return float(self.value(key) or 0)

    def enabled(self, key: str) -> bool:
        return bool(self.value(key))

    def channel_value(self, channel: Any, key: str) -> Any:
        own = _field(channel, key)
        return own if own is not None else self.value(key)

    def channel_integer(self, channel: Any, key: str) -> int:
        return int(self.channel_value(channel, key) or 0)

    def channel_minutes_seconds(self, channel: Any, key: str) -> int:
        return self.channel_integer(channel, key) * MINUTE

    @property
    def day_start(self) -> str:
        return str(self.value("day_start") or "08:00")

    @property
    def day_start_minutes(self) -> int:
        return hhmm_to_minutes(self.day_start)

    @property
    def advert_break_seconds(self) -> int:
        return self.integer("max_break_minutes") * MINUTE

    @property
    def start_rounding_seconds(self) -> int:
        return self.integer("start_rounding_minutes") * MINUTE

    @property
    def duration_tolerance_seconds(self) -> int:
        return self.integer("duration_tolerance_minutes") * MINUTE

    def short_episode_seconds(self, channel: Any) -> tuple[int, int]:
        """(individual cutoff, minimum combined programme length)."""
        return (
            self.channel_minutes_seconds(channel, "short_episode_minutes"),
            self.channel_minutes_seconds(channel, "short_episode_run_minutes"),
        )

    def band_limits(self, channel: Any) -> tuple[int, int, int]:
        """(item minutes, item-repeat seconds, feature-repeat seconds)."""
        return (
            self.channel_integer(channel, "band_item_max_minutes"),
            self.channel_integer(channel, "band_item_repeat_hours") * HOUR,
            self.channel_integer(channel, "band_feature_repeat_days") * DAY,
        )

    def external_prepared(self, at: int) -> bool:
        return at - self.now > self.integer("external_lead_hours") * HOUR

    def external_weight(self, at: int) -> float:
        configured = max(1.0, self.number("external_weight"))
        return configured if self.external_prepared(at) else min(0.1, configured * 0.1)

    @property
    def cadence_seconds(self) -> int:
        return self.integer("series_cadence_days") * DAY

    def next_episode_due(self, last: int | None, at: int) -> bool:
        if not last:
            return True
        return at >= last + self.cadence_seconds - 12 * HOUR

    def cadence_factor(self, last: int | None, at: int, *, relaxed: bool = False) -> float:
        """Weight the next new episode towards the configured weekly slot."""
        if not last:
            return 1.0
        target = last + self.cadence_seconds
        distance = abs(at - target)
        if at < target - 36 * HOUR:
            return 0.3 if relaxed else 0.05
        bonus = self.number("series_cadence_bonus")
        if distance <= 12 * HOUR:
            return bonus
        if distance <= 36 * HOUR:
            return max(1.5, bonus * 0.6)
        return min(3.0, 1.0 + max(0, at - target) / WEEK)

    @property
    def movie_repeat_seconds(self) -> int:
        return self.integer("movie_repeat_days") * DAY

    @property
    def advert_repeat_seconds(self) -> int:
        return self.integer("advert_repeat_penalty_hours") * HOUR
