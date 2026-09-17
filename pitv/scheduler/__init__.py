"""PiTV scheduling, split by responsibility.

``policy``
    The central, typed interpretation of configurable rules and channel inheritance.
``rules``
    Pure eligibility rules for time, eras, certificates, dayparts and patterns.
``bands``
    Generic titled-block matching and selection (music is not special-cased).
``build``
    The stateful schedule walk: it applies policy and rules to library/line-up state.
``listing``
    Human-readable schedule output.

New tunable behaviour should normally be declared in ``db.DEFAULT_SETTINGS``, exposed through
``SchedulerPolicy``, and consumed by the builder. This keeps setting names and unit conversion
out of the scheduling algorithm.
"""
