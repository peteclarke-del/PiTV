"""What each PiTV setting is, for people: the pane it sits on, how familiar a user must be to
want it, its label, help and range.

The admin renders its settings panes from this table (GET /api/settings/schema), and settings
validation takes its ranges from it, so a bound is written once. Defaults stay in
db.DEFAULT_SETTINGS; a test keeps the two tables in step. The keymap (edited on the Player
page) and the password hash are deliberately absent.

Levels: `basic` is what a household needs to run the set; `standard` shapes the schedule;
`advanced` tunes the scheduler's arithmetic, the player's plumbing and the exchange with
pitv_content."""

from __future__ import annotations

from typing import Any

LEVELS = ("basic", "standard", "advanced")

PANES: tuple[tuple[str, str, str], ...] = (
    ("day", "Broadcast day", "When each channel's day runs and how far ahead PiTV builds it."),
    ("programming", "Programming", "What PiTV picks for a slot: the mix of eras, series and films, repeats and dayparts."),
    ("certificates", "Certificates", "When each certificate may air, and children's programming."),
    ("adverts", "Adverts", "Which adverts fill the breaks and which are kept off family channels."),
    ("music", "Music", "How the music channel's day is built."),
    ("player", "Player and screen", "How the set behaves: the remote, on-screen graphics, decoding and self-protection."),
    ("content", "Cache and pitv_content", "PiTV's side of the shared cache, and what it asks pitv_content for."),
    ("maintenance", "Maintenance", "PiTV's own daily housekeeping."),
)

_CERTS = ["U", "PG", "12", "12A", "15", "18"]


def _f(key: str, pane: str, level: str, label: str, kind: str, help_: str, **extra: Any) -> dict[str, Any]:
    return {"key": key, "pane": pane, "level": level, "label": label, "type": kind, "help": help_, **extra}


FIELDS: tuple[dict[str, Any], ...] = (
    # --- broadcast day -------------------------------------------------------------------------
    _f("timezone", "day", "basic", "Timezone", "text", "IANA zone name the schedule is built in, e.g. Europe/London."),
    _f("day_start", "day", "basic", "Day start", "time", "When a broadcast day begins and the overnight replay ends."),
    _f("day_end", "day", "basic", "Day end", "time", "When fresh programming stops and the overnight replay begins; 00:00 is midnight."),
    _f("horizon_days", "day", "standard", "Days built ahead", "int", "How many days ahead the schedule is kept built.", min=1, max=31),
    _f("rebuild_when_days_left", "day", "advanced", "Extend when fewer than", "int",
       "Days left before the schedule is extended automatically.", min=0, max=30),
    _f("end_of_day_overrun_minutes", "day", "advanced", "End of day overrun (minutes)", "int",
       "How far past day end the last programme may run before filler is used instead.", min=0, max=180),
    _f("start_rounding_minutes", "day", "advanced", "Start rounding (minutes)", "int",
       "Programme start times are rounded up to a multiple of this.", min=1, max=30),
    _f("duration_tolerance_minutes", "day", "advanced", "Duration tolerance (minutes)", "int",
       "How far a programme may overrun the gap it is chosen to fill.", min=0, max=60),
    # --- programming ---------------------------------------------------------------------------
    _f("kind_weights", "programming", "standard", "TV and film balance", "kind_weights",
       "The overall mix of episodes and films; dayparts and channels adjust it."),
    _f("era_weights", "programming", "standard", "Era weights", "weights",
       "How strongly programmes are favoured by year. Channels can override this.",
       options={"keyLabel": "Years", "keyPlaceholder": "1980-1989", "addLabel": "Add era"}),
    _f("unknown_year_weight", "programming", "advanced", "Unknown year weight", "float",
       "Programmes with no year still air at this weight; 0 excludes them.", min=0, max=2, step=0.05),
    _f("era_pool_normalise", "programming", "advanced", "Era pool normalisation", "slider",
       "0 weights every title equally; 1 makes each era's share of airtime follow the era weights "
       "however many titles it has.", min=0, max=1, step=0.05),
    _f("movie_repeat_days", "programming", "standard", "Film repeat days", "int",
       "Minimum days before a film is shown again.", min=0, max=365),
    _f("show_daily_limit", "programming", "standard", "Episodes per show per day", "int",
       "Most episodes of one series a channel shows in a day.", min=1, max=10),
    _f("series_rest_weeks", "programming", "standard", "Series rest (weeks)", "int",
       "Weeks a series rests after its last episode before starting again.", min=0, max=104),
    _f("sport_back_to_back_weekends", "programming", "standard", "Sport back to back at weekends", "bool",
       "Let sport follow sport through weekend afternoons."),
    _f("cartoon_genres", "programming", "standard", "Cartoon genres", "chips",
       "Series with any of these genres go to a cartoons channel.", options={"lower": True}),
    _f("show_repeat_penalty", "programming", "advanced", "Show repeat penalty", "float",
       "Weight multiplier for each earlier airing of the same series that day.", min=0, max=1, step=0.05),
    _f("same_slot_bonus", "programming", "advanced", "Same slot bonus", "float",
       "Multiplier favouring a series at the time it aired yesterday, so regulars keep their slot.",
       min=0, max=10, step=0.5),
    _f("genre_repeat_penalty", "programming", "advanced", "Genre repeat penalty", "float",
       "Multiplier when the previous programme shared a genre.", min=0, max=1, step=0.05),
    _f("dayparts", "programming", "advanced", "Weekday dayparts", "dayparts",
       "TV, film, children's and sport weights by time of day, with an optional longest programme. "
       "Channels may override any of the three tables."),
    _f("dayparts_saturday", "programming", "advanced", "Saturday dayparts", "dayparts", "As weekdays, for Saturday."),
    _f("dayparts_sunday", "programming", "advanced", "Sunday dayparts", "dayparts", "As weekdays, for Sunday."),
    # --- certificates --------------------------------------------------------------------------
    _f("kids_cutoff", "certificates", "basic", "Children's programmes until", "time",
       "Children's programmes are not scheduled after this time."),
    _f("watershed", "certificates", "standard", "Film watershed", "times",
       "Earliest start for films of each certificate.",
       options={"keyLabel": "Certificate", "valueLabel": "From", "keyPlaceholder": "15", "addLabel": "Add certificate"}),
    _f("tv_watershed", "certificates", "standard", "TV watershed", "times",
       "Earliest start for episodes of each certificate; unlisted certificates are unrestricted.",
       options={"keyLabel": "Certificate", "valueLabel": "From", "keyPlaceholder": "18", "addLabel": "Add certificate"}),
    _f("weekend_kids_breakfast", "certificates", "standard", "Weekend children's breakfast", "bool",
       "Favour children's programmes at breakfast on Saturday and Sunday."),
    _f("unknown_movie_certificate", "certificates", "advanced", "Film with no certificate", "choice",
       "Certificate assumed for films that have none.", choices=_CERTS),
    _f("unknown_tv_certificate", "certificates", "advanced", "Episode with no certificate", "choice",
       "Certificate assumed for episodes that have none.", choices=_CERTS),
    # --- adverts -------------------------------------------------------------------------------
    _f("advert_era_weights", "adverts", "standard", "Advert era weights", "weights",
       "Which adverts fill breaks, by year. Adverts outside these years are never shown.",
       options={"keyLabel": "Years", "keyPlaceholder": "1980-1989", "addLabel": "Add era"}),
    _f("adult_advert_keywords", "adverts", "standard", "Adult advert keywords", "chips",
       "Used only when pitv_content gives no family-safety verdict: a whole word from this list in "
       "the title keeps the advert off family channels.", options={"lower": True}),
    _f("advert_year_window", "adverts", "advanced", "Advert year window", "int",
       "Prefer adverts from within this many years of the programme.", min=0, max=30),
    _f("advert_repeat_penalty_hours", "adverts", "advanced", "Advert repeat gap (hours)", "int",
       "Avoid repeating an advert within this many hours.", min=0, max=168),
    # --- music ---------------------------------------------------------------------------------
    _f("music_decades", "music", "basic", "Decades played", "decades", "The music channel plays these decades only."),
    _f("music_blocks", "music", "standard", "Blocks", "music_blocks",
       "The music day in time order: each block plays videos matching any of its genres and decades "
       "(empty means any); a concert block plays one full concert."),
    _f("music_concert_repeat_days", "music", "advanced", "Concert repeat (days)", "int",
       "Minimum days before the same concert is shown again.", min=0, max=365),
    _f("music_video_repeat_hours", "music", "advanced", "Video repeat (hours)", "int",
       "Minimum hours before the same video is played again.", min=0, max=720),
    # --- player and screen ---------------------------------------------------------------------
    _f("nas_fallback", "player", "basic", "Play from the NAS when the cache lacks a file", "bool",
       "Otherwise the technical difficulties card is shown until the cache copy arrives."),
    _f("nav_keys_change_channel", "player", "basic", "Up and down change channel", "bool",
       "When the guide is closed; the OSMC remote has no channel keys."),
    _f("nav_keys_change_volume", "player", "basic", "Left and right change volume", "bool", "When the guide is closed."),
    _f("badge_seconds", "player", "standard", "Channel badge seconds", "int",
       "How long the channel badge stays on screen after a change.", min=1, max=60),
    _f("channel_switch_static", "player", "standard", "Static between channels", "bool",
       "A short burst of snow covers the seek when changing channel."),
    _f("osd_scale", "player", "standard", "On-screen text size", "float",
       "1.25 suits a 14 inch 4:3 set at 576 lines.", min=0.5, max=2.5, step=0.05),
    _f("osd_safe_margin", "player", "advanced", "Overscan-safe margin", "float",
       "Fraction of each screen edge kept clear of graphics; a CRT hides about 5 to 8%.", min=0, max=0.2, step=0.01),
    _f("display_aspect", "player", "advanced", "Screen shape", "choice",
       "The physical screen; PAL's 720x576 frame has non-square pixels, so mpv must be told.",
       choices=["4:3", "16:9"]),
    _f("drm_connector", "player", "advanced", "Video output", "text",
       "Force the output, e.g. Composite-1 or HDMI-A-1; empty lets mpv choose."),
    _f("pi_hwdec", "player", "advanced", "Pi hardware decoders", "text",
       "mpv --hwdec list tried in order on the Pi, e.g. drm-prime,v4l2m2m-copy."),
    _f("audio_device", "player", "advanced", "Audio device", "text", "mpv audio device name; auto picks the default output."),
    _f("clock_wait_seconds", "player", "advanced", "Clock wait at boot (seconds)", "int",
       "The Pi has no clock battery: at boot the player waits this long for network time before tuning.",
       min=0, max=900),
    _f("memory_limit_mb", "player", "advanced", "Player memory limit (MB)", "int",
       "The player restarts itself above this; systemd's own cap sits higher.", min=200, max=3000),
    # --- cache and pitv_content ----------------------------------------------------------------
    _f("nas_only", "content", "basic", "Only schedule what is on disk", "bool",
       "Off: line-up entries not on the NAS or in the cache may be scheduled ahead and fetched by "
       "pitv_content. Channels can override this."),
    _f("cache_dir", "content", "standard", "Cache folder", "path",
       "The shared folder on the attached drive: pitv_content writes copies here, PiTV plays from it. "
       "Empty disables the cache."),
    _f("cache_max_gb", "content", "standard", "Cache size (GB)", "int",
       "pitv_content fills the cache; PiTV evicts the least recently played copies above this.", min=1, max=100_000),
    _f("content_tool_url", "content", "standard", "pitv_content address", "text",
       "Where pitv_content's API listens: this machine or the local network."),
    _f("acquire_fill_gaps", "content", "standard", "Request missing episodes", "bool",
       "Gaps between the episodes on disk go to the wanted list for pitv_content to fetch."),
    _f("acquire_dir", "content", "advanced", "Download folder", "path",
       "Where pitv_content files what it fetches; empty uses the cache folder's acquired folder."),
    _f("external_lead_days", "content", "advanced", "Lead time for fetches (days)", "int",
       "Material not on disk is scheduled at least this far ahead, so pitv_content has time to fetch it.",
       min=0, max=14),
    _f("external_episode_minutes", "content", "advanced", "Assumed episode length (minutes)", "int",
       "For series whose files are not on disk yet.", min=1, max=240),
    _f("external_weight", "content", "advanced", "Weight of material not on disk", "float",
       "How readily it is picked next to material on disk; 1 is equal footing.", min=0, max=2, step=0.05),
    _f("transient_keep_days", "content", "advanced", "Keep transient files (days)", "int",
       "Fetched transient material is deleted this long after it airs.", min=0, max=365),
    _f("browse_roots", "content", "advanced", "Folders the picker may browse", "chips",
       "Absolute paths the folder picker may open, for example for pitv_content's source roots."),
    _f("content_profile", "content", "advanced", "Encoding profile", "readonly",
       "What PiTV asks pitv_content to encode to."),
    # --- maintenance ---------------------------------------------------------------------------
    _f("catalogue_hour", "maintenance", "standard", "Catalogue import hour", "int",
       "Daily import of pitv_content's library index; the schedule is extended afterwards.", min=0, max=23),
    _f("readiness_hours", "maintenance", "standard", "Readiness check hours", "hours",
       "PiTV checks tomorrow's files at these hours and substitutes anything missing."),
    _f("history_keep_days", "maintenance", "advanced", "Keep airing history (days)", "int",
       "Airing history older than this is pruned.", min=1, max=3650),
)

BY_KEY: dict[str, dict[str, Any]] = {f["key"]: f for f in FIELDS}
# Settings with no field: edited elsewhere or never by hand.
UNLISTED = frozenset({"keymap", "admin_password_hash"})


def bounds(key: str) -> tuple[float, float] | None:
    """The (min, max) a numeric setting must fall within, or None when it only needs to be a
    finite, non-negative number."""
    field = BY_KEY.get(key)
    if not field or "min" not in field:
        return None
    return field["min"], field["max"]


def schema(values: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """The panes and fields with each setting's default and current value, for the admin."""
    pane_label = {pid: label for pid, label, _ in PANES}
    return {
        "levels": list(LEVELS),
        "panes": [{"id": pid, "label": label, "help": help_} for pid, label, help_ in PANES],
        "fields": [{**f, "group": pane_label[f["pane"]], "default": defaults.get(f["key"]), "value": values.get(f["key"])}
                   for f in FIELDS],
    }
