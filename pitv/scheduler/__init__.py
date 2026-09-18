"""PiTV scheduling, one responsibility per module.

``slots``
    What a schedule is made of: slots, the series they come from, titles. Plain data.
``policy``
    What the configured settings mean: units, channel inheritance, timing formulae.
``rules``
    Pure eligibility rules: broadcast day bounds, eras, certificates, dayparts, decades,
    patterns.
``library``
    What a build has to schedule and what has aired of it: the usable catalogue, the line-up
    entries with nothing on disk, each channel's bands, history and cursors. Loaded once.
``select``
    Choosing one thing for a gap: a programme, an advert, an ident. Every weighting rule.
``bands``
    Titled stretches of a day: the timetable, what a band may use, filling a band and the
    time between bands. Nothing here knows what music is.
``runs``
    Runs of short episodes, local or remote, and the placeholder slot for material not on
    disk yet.
``overnight``
    The small hours: a replay of the day, or more of a band channel's own material.
``build``
    The day walk: kept slots, anchors and bands as fixed points, the pattern between them.
    It asks the modules above and emits slots; it decides nothing about eligibility.
``horizon``
    Which days to build and when: the nightly extension, refill of thin days, rebuilding a
    channel from a point, starting over.
``listing``
    Human-readable schedule output.

A new rule goes in the module that owns its question; a new tunable is declared in
``db.DEFAULT_SETTINGS`` and read through ``SchedulerPolicy``.
"""
