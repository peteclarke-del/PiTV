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

from . import display

LEVELS = ("basic", "standard", "advanced")

PANES: tuple[tuple[str, str, str], ...] = (
    ("screen", "Screen and quality", "The set PiTV drives: the picture it plays, and the quality pitv_content fetches and encodes."),
    ("day", "Broadcast day", "When each channel's day runs and how far ahead PiTV builds it."),
    ("programming", "Programming", "What PiTV picks for a slot: the mix of eras, series and films, repeats and dayparts."),
    ("certificates", "Certificates", "When each certificate may air, and children's programming."),
    ("adverts", "Adverts", "Which adverts fill the breaks and which are kept off family channels."),
    ("player", "Player", "How the set behaves: the remote, decoding and self-protection."),
    ("content", "Cache and pitv_content", "PiTV's side of the shared cache, and what it asks pitv_content for."),
    ("maintenance", "Maintenance", "PiTV's own daily housekeeping."),
)

_CERTS = ["U", "PG", "12", "12A", "15", "18"]
_SCREEN_HELP = ("Sets the picture PiTV plays and pitv_content encodes to, and fetches the best source up to two "
                "steps higher (equal at 4K). Choosing one also resets the screen shape and on-screen margins below. "
                "The player asks for this HDMI mode; a set that cannot show it stays blank until you choose again. "
                "pitv_content's catalogue runs follow the same screen. 4K encodes on a Pi 4 run far slower than "
                "real time, so the 4K screen suits material that arrives as HEVC or a faster pitv_content machine.")


def _f(key: str, pane: str, level: str, label: str, kind: str, help_: str, **extra: Any) -> dict[str, Any]:
    return {"key": key, "pane": pane, "level": level, "label": label, "type": kind, "help": help_, **extra}


FIELDS: tuple[dict[str, Any], ...] = (
    # --- screen and quality --------------------------------------------------------------------
    _f("display_profile", "screen", "basic", "Screen", "choice", _SCREEN_HELP, choices=display.CHOICES),
    _f("content_profile", "screen", "basic", "Quality", "readonly", "What this screen asks of pitv_content."),
    _f("osd_scale", "screen", "standard", "On-screen text size", "float",
       "1.25 suits a 14 inch 4:3 set at 576 lines.", min=0.5, max=2.5, step=0.05),
    _f("display_aspect", "screen", "advanced", "Screen shape", "choice",
       "The physical screen; PAL's 720x576 frame has non-square pixels, so mpv must be told.",
       choices=["4:3", "16:9"]),
    _f("osd_safe_margin", "screen", "advanced", "Overscan-safe margin", "float",
       "Fraction of each screen edge kept clear of graphics; a CRT hides about 5 to 8%.", min=0, max=0.2, step=0.01),
    _f("drm_connector", "screen", "advanced", "Video output", "text",
       "Force the output, e.g. Composite-1 or HDMI-A-1; empty lets mpv choose."),
    # --- broadcast day -------------------------------------------------------------------------
    _f("timezone", "day", "basic", "Timezone", "text", "IANA zone name the schedule is built in, e.g. Europe/London."),
    _f("day_start", "day", "basic", "Day start", "time", "When a broadcast day begins and the overnight replay ends."),
    _f("day_end", "day", "basic", "Day end", "time", "When fresh programming stops and the overnight replay begins; 00:00 is midnight."),
    _f("horizon_days", "day", "standard", "Days built ahead", "int", "How many days ahead the schedule is kept built.", min=1, max=31),
    _f("rebuild_when_days_left", "day", "advanced", "Extend when fewer than", "int",
       "Days left before the schedule is extended automatically.", min=0, max=30),
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
    _f("show_repeat_penalty", "programming", "advanced", "Show repeat penalty", "float",
       "Weight multiplier for each earlier airing of the same series that day.", min=0, max=1, step=0.05),
    _f("series_cadence_days", "programming", "standard", "Series cadence (days)", "int",
       "A series airs one episode, then returns to the same slot after this many days. A channel may set its own (Channels, Programmes); strips and anchors remain exact.",
       min=1, max=28),
    _f("peak_from", "programming", "standard", "Peak hours from", "time",
       "With one episode a week there are rarely enough series to fill a day. Those due are kept for the peak hours, "
       "and films and children's programmes carry the rest, as the broadcasters ran their days."),
    _f("peak_until", "programming", "standard", "Peak hours until", "time",
       "Series are offered at other hours only while more are due than the peak hours still to come could hold."),
    _f("series_cadence_bonus", "programming", "advanced", "Series cadence bonus", "float",
       "Multiplier favouring the next episode close to its target weekday and time.",
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
    _f("genre_families", "programming", "advanced", "Genres that satisfy one another", "weights",
       "Each genre lists what else will satisfy a band or channel asking for it: Metal is satisfied "
       "by Hard Rock and the metal subgenres, Soul by Motown. It only ever widens, so nothing "
       "correctly labelled is refused, and it is read one way at a time: Hard Rock satisfying Metal "
       "does not make Metal satisfy Hard Rock. pitv_content is sent these with every request for "
       "band material, so what it collects is judged by the same rule.",
       options={"keyLabel": "Genre", "keyPlaceholder": "Metal", "addLabel": "Add genre", "values": "list"}),
    _f("unnamed_advert_keywords", "adverts", "standard", "Words that mean an advert has no name", "chips",
       "A whole word from this list in the title means nothing could identify the advert, usually a "
       "chapter of a compilation. It stays in the library, flagged for attention, and is not put in a "
       "break: the guide has nothing to call it.", options={"lower": True}),
    _f("max_break_minutes", "adverts", "standard", "Longest advert break (minutes)", "int",
       "Adverts stop here however wide the gap they are filling, so a hole in the day never becomes"
       " twenty minutes of advertising. What is left takes idents, then the caption.", min=1, max=30),
    _f("advert_year_window", "adverts", "advanced", "Advert year window", "int",
       "Prefer adverts from within this many years of the programme.", min=0, max=30),
    _f("advert_repeat_penalty_hours", "adverts", "advanced", "Advert repeat gap (hours)", "int",
       "Avoid repeating an advert within this many hours.", min=0, max=168),
    _f("short_episode_minutes", "programming", "standard", "Group episodes shorter than", "int",
       "Episodes shorter than this are run together under the series title, so a five minute"
       " cartoon does not take a slot of its own. 0 turns it off.", min=0, max=60),
    _f("short_episode_run_minutes", "programming", "standard", "Run them together for", "int",
       "How long a run of short episodes should last before the channel moves on.", min=5, max=120),
    _f("band_item_repeat_hours", "programming", "advanced", "Band item repeat (hours)", "int",
       "Minimum hours before a band plays the same short item (a music video, an episode) again.",
       min=0, max=720),
    _f("band_feature_repeat_days", "programming", "advanced", "Band feature repeat (days)", "int",
       "Minimum days before a band plays the same long item (a concert, a film) again.", min=0, max=365),
    # --- player and screen ---------------------------------------------------------------------
    _f("player_keepalive", "player", "basic", "Keep the television on", "bool",
       "Start the player whenever it is found stopped, within about half a minute, whether it was"
       " closed, crashed or never started. Off only while you are working on it."),
    _f("nas_fallback", "player", "basic", "Play from the NAS when the cache lacks a file", "bool",
       "Otherwise the technical difficulties card is shown until the cache copy arrives."),
    _f("nav_keys_change_channel", "player", "basic", "Up and down change channel", "bool",
       "When the guide is closed; the OSMC remote has no channel keys."),
    _f("nav_keys_change_volume", "player", "basic", "Left and right change volume", "bool", "When the guide is closed."),
    _f("streaming_enabled", "player", "basic", "Stream the channels over HTTP", "bool",
       "Watch a channel on a phone, a browser or VLC at http://<this machine>/channel/1 (2, 3 and so on). "
       "A stream starts when someone asks for it and stops when nobody is watching."),
    _f("stream_max_streams", "player", "advanced", "Streams at once", "int",
       "Channels that may stream at the same time. Each browser-safe stream is encoded to H.264/AAC.",
       min=1, max=8),
    _f("stream_segment_seconds", "player", "advanced", "Stream segment (seconds)", "int",
       "Shorter segments start sooner and lag less; longer ones are steadier on a poor network.", min=2, max=10),
    _f("stream_idle_seconds", "player", "advanced", "Stop a stream after (seconds)", "int",
       "How long a stream keeps running once nothing has asked for it.", min=10, max=3600),
    _f("stream_encoder", "player", "advanced", "Stream encoder", "text",
       "ffmpeg encoder used for browser-safe streams; empty picks h264_v4l2m2m on the Pi "
       "and libx264 elsewhere."),
    _f("badge_seconds", "player", "standard", "Channel badge seconds", "int",
       "How long the channel badge stays on screen after a change.", min=1, max=60),
    _f("channel_switch_static", "player", "standard", "Static between channels", "bool",
       "A short burst of snow covers the seek when changing channel."),
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
    _f("band_fetch", "content", "standard", "Find material for bands", "bool",
       "A band with too little of its own genres and decades in the library asks pitv_content to"
       " fetch some. All known shortfalls are queued up front and downloaded one at a time."),
    _f("band_fetch_hours", "content", "advanced", "Hours it may fetch for bands", "hours",
       "Hours when PiTV may queue band top-ups. All of them by default: pitv_content delivers what is scheduled first "
       "and collects for bands in the turns between, so it is never idle while a band could use more."),
    _f("band_stock_days", "content", "standard", "Days a band can run without a repeat", "int",
       "A band is topped up until it holds enough of its own material to run this many days without repeating an item. "
       "Past that, nothing more is asked for it.", min=1, max=60),
    _f("band_item_max_minutes", "programming", "standard", "Band items are under (minutes)", "int",
       "A band runs several short things under one title, so anything this long counts as a feature"
       " instead: a film or a concert, which only a band that opens with one will take. It is also the"
       " longest thing fetched for a band. A channel or a single band may set its own.", min=1, max=600),
    _f("external_new_per_day", "content", "standard", "New remote programmes a day", "int",
       "The most new episodes and films not yet on disk that the schedule may promise for one day,"
       " across all channels. Everything else comes from the library, so the schedule never depends"
       " on more than pitv_content can fetch in time. A title airs once a week, so a few a day go a"
       " long way.", min=0, max=200),
    _f("band_fit_minutes", "programming", "advanced", "A band is met within (minutes)", "int",
       "A concert or film is chosen to end within this many minutes of its band's end, where the"
       " library has one; the same goes for long items between bands.", min=0, max=60),
    _f("band_feature_overrun_minutes", "programming", "advanced", "A feature may overrun by (minutes)", "int",
       "When nothing ends that close, a band's opening feature may run this far past the band rather"
       " than the band open with none.", min=0, max=120),
    _f("band_card_message", "programming", "standard", "Band holding card", "text",
       "Shown under a band's name for whatever part of its time the library cannot fill, followed by"
       " when service resumes."),
    _f("band_fetch_gap_hours", "content", "advanced", "Ask again for a band after (hours)", "int",
       "How long PiTV leaves a band before asking pitv_content for more of the same.", min=1, max=168),
    _f("band_fetch_min", "content", "advanced", "Fewest items to ask for", "int",
       "A top-up never asks for fewer than this: a run costs a search either way.", min=1, max=200),
    _f("band_fetch_max", "content", "advanced", "Most items to ask for", "int",
       "Nor for more than this at once, so one band cannot take the whole night.", min=1, max=500),
    _f("acquire_dir", "content", "advanced", "Download folder", "path",
       "Where pitv_content files what it fetches; empty uses the cache folder's acquired folder."),
    _f("external_lead_hours", "content", "advanced", "Local-first window (hours)", "int",
       "Inside this window PiTV favours files already local or on the NAS. Beyond it remote line-up entries compete on variety.",
       min=0, max=168),
    _f("external_episode_minutes", "content", "advanced", "Assumed episode length (minutes)", "int",
       "For series whose files are not on disk yet.", min=1, max=240),
    _f("external_weight", "content", "advanced", "Weight of material not on disk", "float",
       "How readily remote material is picked once outside the local-first window; 1 is equal footing.", min=0, max=2, step=0.05),
    _f("transient_keep_days", "content", "advanced", "Keep transient files (days)", "int",
       "Fetched transient material is deleted this long after it airs.", min=0, max=365),
    _f("browse_roots", "content", "advanced", "Folders the picker may browse", "chips",
       "Absolute paths the folder picker may open, for example for pitv_content's source roots."),
    # --- maintenance ---------------------------------------------------------------------------
    _f("catalogue_hour", "maintenance", "standard", "Catalogue import hour", "int",
       "Daily import of pitv_content's library index; the schedule is extended afterwards.", min=0, max=23),
    _f("readiness_hours", "maintenance", "standard", "Readiness check hours", "hours",
       "PiTV checks tomorrow's files at these hours and substitutes anything missing."),
    _f("history_keep_days", "maintenance", "advanced", "Keep airing history (days)", "int",
       "Airing history older than this is pruned.", min=1, max=3650),
    _f("schedule_keep_days", "maintenance", "advanced", "Keep aired slots (days)", "int",
       "Slots that have aired are kept this long for the history and the \"what was on\" views, "
       "then pruned.", min=1, max=3650),
    _f("run_log_keep_days", "maintenance", "advanced", "Keep run logs (days)", "int",
       "Builds, imports and readiness checks are logged; entries older than this are pruned.",
       min=1, max=3650),
)

BY_KEY: dict[str, dict[str, Any]] = {f["key"]: f for f in FIELDS}
# Settings with no field: edited elsewhere or never by hand.
UNLISTED = frozenset({"keymap", "admin_password_hash", "content_fetch_kinds"})
# Fields shown but not stored: derived from other settings when the schema is served.
COMPUTED = frozenset({"content_profile"})


def choice_values(key: str) -> list[Any] | None:
    """The values a choice setting accepts (choices may be plain values or {value, label})."""
    choices = BY_KEY.get(key, {}).get("choices")
    return None if choices is None else [c["value"] if isinstance(c, dict) else c for c in choices]


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
