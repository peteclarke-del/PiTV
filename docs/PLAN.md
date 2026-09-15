# PiTV design

A Raspberry Pi 4 that behaves like a 1980s UK television: six continuously "broadcasting"
channels, a weekly schedule built from a programme catalogue that starts with the NAS
library, an RF remote, a teletext-style on-screen guide, and a web interface for viewing the
schedule and administering everything. A companion app on the same Pi, pitv_content, owns
the sources and fills the cache; the interface between the two is
[CONTENT_CONTRACT.md](CONTENT_CONTRACT.md).
This document describes the design as built. [REQUIREMENTS.md](REQUIREMENTS.md) is the
checklist of what was asked for and where each item lives.

## 1. Goals and constraints

| Requirement | Decision |
|---|---|
| Hardware | Raspberry Pi 4 (4 GB), HDMI-to-SCART converter into a 14" 4:3 colour CRT (the Pi's composite output is an alternative), OSMC RF remote, USB hard drive for the cache, wired Ethernet to the Synology NAS over SMB |
| OS | DietPi (Bookworm) on the SD-card image built by `installer/`; Raspberry Pi OS Lite 64-bit (Bookworm) for a manual install. No desktop; boots straight into the player |
| Video decode | Pi 4 hardware H.264 and HEVC through V4L2 M2M (section 5.3). Anything else is re-encoded into the cache by pitv_content so everything on air takes the hardware path |
| Boot | Target about 15 s from power to picture. A test card is drawn on the framebuffer within seconds and stays up until the clock is synchronised and the first file plays |
| Channels | Rows in the database, each can be enabled or disabled. Six by default: 1 and 2 programmes only, 3 and 4 with adverts, 5 music, 6 cartoons |
| Broadcast day | 08:00 to 00:00 scheduled; 00:00 to 08:00 replays that day's schedule (section 4.9) |
| Era | Programmes of any age with a mix either side of 1980 (1920-1979 weight 0.45, 1980-1989 0.40, 1990-1999 0.15; editable globally and per channel). Adverts 1980s and 1990s only. Year from pitv_content's library index, or an admin override |
| Watershed | Films: 12 not before 20:00, 15 not before 21:00, 18 not before 22:00. TV episodes obey only the 15 and 18 rules (a 12-rated series was routinely repeated in the daytime). No children's programmes after 21:00 except on the cartoon channel |
| Horizon | 7 days built ahead, topped up when fewer than 2 days remain; episodes advance in order per show; repeats minimised |
| Remote | OSMC RF remote (USB dongle, appears as a keyboard): arrows, OK, Back, Home, Info, Play/Pause, Stop, volume. Any remote with number keys tunes channels 1 to 9 directly |
| Content | pitv_content owns the sources (NAS shares and online providers), indexes them and fills the cache; PiTV imports its index as the programme catalogue and plays from the cache (sections 3 and 7) |
| Web | Public now/next, guide and virtual remote; an admin area behind a single password, in two sections: PiTV (catalogue, channels, weighting, schedule, wanted list, player, logs, system) and pitv_content (sources, status, runs, settings, providers) |
| Overhead | One mpv process, one Python player daemon, one Python web service, SQLite. No X, no Kodi, and no cron or systemd timers on PiTV's side |

## 2. How "broadcast" works

Each channel has a schedule: an ordered list of slots `(channel, start, end, media file,
offset into file, kind)` in SQLite. Channels are virtual; only the channel being watched is
decoded. Every half second the player computes from the wall clock which slot the current
channel is in and how far through it is. Changing channel means loading that channel's
current file in mpv and seeking to `now - slot.start + slot.offset`.

Consequences:

- All channels appear to run all the time, even while the Pi is off. Turn it on at 19:42
  and channel 1 is 12 minutes into whatever started at 19:30.
- A programme split into parts is two slots pointing at the same file with different
  offsets. The model supports that even though adverts are only placed between programmes.
- The on-screen guide and the web guide read the same schedule table through the same
  lookups (`pitv/guide.py`), so they always agree.
- The Pi has no real-time clock. At boot the player shows the test card and waits up to
  `clock_wait_seconds` (120) for NTP before tuning; if the clock steps by more than a minute
  later it re-tunes to the live position at once.

## 3. Programme catalogue

PiTV schedules from a catalogue: the `sources`, `shows` and `media` tables. It does not read
the NAS to build it. pitv_content owns every source, both the NAS shares and the online
providers, indexes them and publishes a library index; PiTV imports that index
(`pitv/catalogue.py`) and adds custom programming through channel line-ups (section 4.2).
The index format, like everything else that passes between the two apps, is fixed in
[CONTENT_CONTRACT.md](CONTENT_CONTRACT.md).

The reason is to have one indexer. PiTV used to walk the shares, parse names, read NFO files
and probe every file with ffprobe, and pitv_content then read the same files again to copy or
transcode them. Two sets of rules could disagree about what a file was, and every file was
read twice over CIFS. pitv_content has to open every file it delivers anyway, so it indexes
and PiTV decides what airs. PiTV no longer needs ffprobe.

### 3.1 Sources

A source is one of pitv_content's NAS shares, or one of the folders under `acquire_dir` where
it files what it has fetched. Each has an id, a type (`tv`, `movie`, `advert`, `ident`,
`music`), a category that matters only for `tv` sources (`general`, `sport` or `kids`; series
inherit it), a root on the Pi, an informational SMB URL and an enabled flag. The installer
seeds pitv_content's sources from its share list (section 9). After that they are edited on
the Sources page in the pitv_content section of PiTV's admin, which writes through
pitv_content's API (`PUT {content_tool_url}/api/sources`), because pitv_content has no
interface of its own. When pitv_content is down the page shows the sources from the last
imported index, read-only.

PiTV's `sources` table is a mirror of the index, not configuration: `uid` is pitv_content's
source id, `location` is `nas` or `cache`, and `last_indexed_at` and `index_summary` record
the last import that included the source. The rows give imported items a source and the admin
something to show; nothing in PiTV walks them.

The installer's default shares, and the layouts pitv_content expects on them:

| Share | Type | Layout |
|---|---|---|
| `smb://synologynas/tvshows/` | tv | `Show Name (1984)/Season 02/Show Name - S02E05 - Title.mkv`; also `2x05` and `Season 2/05 - Title`. Show year from the folder |
| `smb://synologynas/movies/` | movie | `Title (1985)/Title (1985).mkv`; year from the folder, then the file name |
| `smb://synologynas/ads/` | advert | `1984/Product.mp4` or `1984 - Product.mp4`; year from the sub-folder or a leading year |
| `smb://synologynas/tvsports/` | tv, category sport | As tvshows; episodes dated, e.g. `World of Sport Wrestling - 1985-03-16.mp4` |
| `smb://synologynas/music videos/` | music | `<Genre>/<Artist> - <Title> (<Year>).mp4`; concerts under `Concerts/<Genre>/` |
| `<cache>/acquired/{tvshows,tvsports,movies,ads,music videos}` | as above | Where pitv_content files what it fetches (section 7). Indexed with `location: cache`, so PiTV treats these items as already cached |
| any folder | ident | `ch1/*.mp4`; the folder name gives the channel the ident belongs to |

The NAS shares require authentication and are mounted read-only with CIFS from a root-only
credentials file (`/etc/pitv/smb-credentials`) through systemd automount units, so boot
never blocks on the network. Mount options (`systemd/mnt-share.mount.template`):
`vers=3.0,ro,noserverino,cache=loose,actimeo=60,uid=pitv,gid=pitv,iocharset=utf8,_netdev,x-systemd.automount,x-systemd.idle-timeout=0,x-systemd.mount-timeout=30`.
`install.sh` writes one mount and automount unit per share named in `SHARES`. PiTV's
installer keeps the mounts because both apps read the shares: pitv_content to index and
deliver, PiTV to play an original when its cache copy is missing (section 5.2). A source added
in the admin must point at a folder that is already mounted, because mounting needs root and
neither web service runs as root.

### 3.2 Importing the index (`pitv catalogue`)

- Where from. `GET {content_tool_url}/api/library`; when the API is down, the same document
  at `<cache_dir>/index/library.json`, which pitv_content writes atomically. `pitv catalogue
  --file index.json` imports a given file, and `--reindex` asks pitv_content to re-index its
  sources first (`POST /api/index`).
- Schema 2 only. Any other document is rejected, the attempt is logged as a failed
  `catalogue` run and the catalogue is left as it was.
- Rows. A media row carries `uid`, `origin` (`nas`, `cache` or `online`: where the original
  lives), `path` (the original) and `cache_path` (the copy pitv_content delivered, which is
  what plays). A show row is keyed on the index's series uid.
- Stable ids. Rows are matched on the index uid (`nas:<source>:<relpath>` for items,
  `show:<source>:<folder>` for series), then on path for rows the old scanner created, so
  schedules, history, overrides, episode cursors and line-ups keep their references across
  imports and across the move away from the scanner. Importing the same index twice changes
  nothing.
- Missing material. A complete index (`"complete": true`) marks anything it no longer lists
  as `missing` and disables sources it no longer lists. Rows are never deleted, so a file that
  comes back keeps its history. An incomplete index only adds and updates. Material fetched
  online is managed by delivery reports and is never marked missing by an import.
- Cache sources. An item from a `cache` source (uid `cache:<source>:<relpath>`) is its own
  cache copy: `cache_path` is set on import and it counts as cached at once.
- Derived on import: `hwdec` from the video codec (H.264 or HEVC), `kids`, the `cartoon`
  category, advert family safety, music concerts and the "needs attention" note (section 3.3).
- Afterwards the line-ups are restored from `lineups.json` if the table is empty, new series
  and films are placed into line-ups (section 4.2), and `catalogue.json` is rewritten.
- When. Daily at `catalogue_hour` (04:00) from the player's maintenance thread; whenever
  pitv_content rewrites the index file (each maintenance pass, every ten minutes, compares its
  modification time); and on demand from the admin, as a background job that either imports
  the current index or asks for a re-index first. Every import is a `catalogue` run in the run
  log. `pitv catalogue` also writes the `catalogue` log; imports started by the player or the
  web service go to their own logs.

### 3.3 Classification rules

pitv_content supplies the facts; PiTV applies admin overrides and its own scheduling defaults.

| Attribute | Source order | When unknown |
|---|---|---|
| Year | index, then override | scheduled at `unknown_year_weight` (0.2) and listed under "needs attention" |
| Certificate | index, then override | films treated as 15 (post-watershed), TV as PG |
| Genres | index, then override | none |
| Kids | category `kids`, or a genre in animation, children, children's, kids, family, cartoon, or override | no |
| Category | source category (general, sport, kids); a series that is not sport and has a genre in `cartoon_genres` becomes `cartoon`; editable per show | general |
| Family-safe (adverts) | pitv_content's `family_safe` flag; else its tags (alcohol, tobacco, adult, gambling or 18 make an advert unsafe); else `adult_advert_keywords` matched as whole words against the title and file name; editable per advert | safe |
| Concert (music) | the index's `concert` flag, else any music item of 35 minutes or more | no |

The keyword list is only a fallback: pitv_content sees the files and their metadata, PiTV
sees names. It matches whole words so that "ale" does not catch "sale" or "gin" catch
"engineering", which would keep harmless adverts off the cartoon channel.

The "needs attention" note flags items with no duration, no year, films with no certificate,
and files of 720 lines or more that the Pi can only decode in software (pitv_content
transcodes those when they are scheduled).

Overrides (title, year, certificate, genres, plot, kids, season, episode, artist) are stored
in a JSON column on the row and applied on top of indexed values, so a re-import never undoes
an admin's correction. Show-level scheduling fields (strip or weekly mode, anchor time and
days, rest weeks, category, excluded) are ordinary columns; the channel comes from the
line-up (section 4.2). `GET /api/export` returns settings, channels, sources and every
override as JSON for backup.

Era weights are a global setting with per-channel overrides. A series gets the best weight
of any year in its run (premiere year to premiere year plus number of seasons), so a show
that started in 1978 and ran to 1986 counts as 80s. Adverts use `advert_era_weights`
(1980-1989 0.85, 1990-1999 0.15) and anything outside those decades is never scheduled.

### 3.4 Custom programming and the catalogue mirror

The catalogue starts from the NAS, meaning whatever pitv_content indexes, and is extended
with custom programming: a line-up entry that names a series or film not in the index, added
by title or from pitv_content's catalogue of titles it can fetch. With NAS-only off the
scheduler places such an entry ahead of time and requests it (section 4.2). When pitv_content
delivers it, the delivery report's `meta` becomes a catalogue row with origin `online` and its
cache path set; a series also gets a show row of its own. Transient material is removed after
airing (section 4.2).

`catalogue.json` beside the database lists every series and film PiTV can schedule, where
each comes from and whether it is cached, plus the custom line-up entries. It is rewritten
after each import and after each delivery that creates an entry, and `GET
/api/catalogue/export` returns the same document. It is a record for inspection and backup,
not an input: after a database rebuild the catalogue comes back from the next import and the
line-ups from `lineups.json`.

## 4. Scheduler (`pitv schedule`)

Builds `horizon_days` (7) broadcast days for every enabled channel into the `schedule`
table. It never rewrites slots that have started or that an admin has locked. The player's
maintenance thread checks every ten minutes and extends the horizon when fewer than
`rebuild_when_days_left` (2) days remain; the admin UI can build on demand with a start
day, a number of days, a channel subset and a force flag, as a background job with progress.

### 4.1 Channels are data

A channel row holds: number, name, short name, colour, enabled, pattern, ads enabled, adverts
per break, idents enabled, era weights, genre weights, kind (TV/movie) weights, daypart
profile (weekday, Saturday, Sunday), overnight replay start, content type (`general`,
`music`, `cartoons`), family-safe adverts flag, allowed and excluded genre lists, a NAS-only
override (`inherit`, `yes`, `no`) and a description. Per-channel weights are optional and
fall back to the global settings. No rule in the code refers to a channel by name or
number; the six channels below are seed data for a fresh install and every value is
editable in the admin UI.

| Ch | Name | Pattern | TV/movie | Lean |
|---|---|---|---|---|
| 1 | PiTV One | `show` | 0.75/0.25 | Mainstream: drama, sitcoms, light entertainment, afternoon films |
| 2 | PiTV Two | `show` | 0.60/0.40 | Alternative: documentaries, cult, older films, comedy |
| 3 | PiTV Three | `show, ad, ad` | 0.80/0.20 | Commercial: soaps, quiz, action drama, kids' teatime |
| 4 | PiTV Four | `show, ad, ad` | 0.55/0.45 | Alternative commercial: comedy, imports, films, late night |
| 5 | PiTV Music | blocks (section 4.5) | | Music videos by genre and decade, two concerts a day |
| 6 | PiTV Toons | `show, show, ad, ad` | 1.0/0.0 | Cartoons all day; family-safe adverts only |

The lean is the description. What a channel actually carries is its line-up (section 4.2),
generated from the allowed genre lists (for example drama, comedy, family and quiz on One;
documentary, science fiction, cult and sport on Two; animation only on Toons) and then
edited by hand. Weights and dayparts shape when those programmes air.

### 4.2 Line-ups

A line-up is the list of series and films a channel carries. It is the source of truth for
which programme belongs where, and every series or film belongs to exactly one channel, so a
programme on one channel never appears on another. Adverts, idents and music videos are not
programmes and stay shared by rule.

- Generation. After every catalogue import, series and films that belong to no line-up are
  placed on the enabled general or cartoon channel that suits them best. A channel accepts an
  item when it has at least one of the channel's allowed genres and none of its excluded ones
  (an empty allowed list accepts anything). Among the channels that accept it, the item goes
  where load in hours, divided by how well it fits, is lowest. Sport and everything else are
  balanced separately, so a channel that takes the long sport series still gets its share of
  ordinary programmes; fit is the share of the channel's allowed genres the item matches, so
  a narrowly defined channel attracts what it specialises in. Items the index gives no genres
  are accepted by every general channel and spread by load. Items no channel accepts are
  flagged in the catalogue's attention list. Rebalance redistributes everything that is not
  pinned.
- Editing. In the admin a channel's line-up can gain any series or film from a searchable
  list (catalogue titles, plus pitv_content's catalogue of fetchable titles when its API is
  up), lose entries, or move entries to another channel. A hand edit pins the entry so
  rebalancing leaves it alone. Changing a show's or film's channel in the catalogue editors
  moves its line-up entry.
- External entries. An entry can name a series or film that is not in the index: added by
  title, or from pitv_content's catalogue of fetchable titles. This is custom programming
  (section 3.4). What the scheduler does with it depends on NAS-only (below).
- Retention. Material fetched for an external entry is transient by default: it lives only
  in the cache, is never removed before it has aired, and is deleted
  `transient_keep_days` (7) after its last airing, or straight after airing when the entry
  asks for that. An entry can be switched to keep instead.
- Persistence. Line-ups live in the database and are mirrored to `lineups.json` beside it on
  every change. The admin exports and imports the same document, and an empty line-up table
  (a rebuilt database) is restored from the mirror on the next catalogue import.

NAS-only. A global switch (`nas_only`, on by default), overridable per channel, limits
scheduling to material already in the catalogue (on the NAS or in the cache). When it is off
for a channel, its external entries may be placed like any other programme, but only on
broadcast days at least `external_lead_days` (2) after today's date, at `external_weight`
(0.7) relative to catalogue programmes, using `episode_minutes` or `external_episode_minutes`
(30) as the length. Each placement is a placeholder slot plus a wanted item for pitv_content
(section 7.2): a series asks for its next episode number, a film for itself. When
pitv_content delivers the file, its delivery report creates the catalogue entry, binds it to
the placeholder, sets the slot to the file's real length and rebuilds the rest of that
channel-day from the earliest change. A placeholder still empty at the readiness checks is
replaced like any missing file, and the error is logged.

### 4.3 Patterns

A pattern is an ordered list of tokens the channel cycles through when filling gaps:

| Token | Meaning |
|---|---|
| `show` | any programme (TV episode or film, chosen by daypart and weights) |
| `tv` | a TV episode |
| `movie` | a film |
| `ad` | one advert |
| `ident` | a channel ident |
| `break` | the channel's configured number of adverts |

`ad` and `break` tokens are dropped when the channel's adverts are off. An anchored show
(section 4.4) counts as a `show` token where it lands, so the pattern resumes cleanly. When
no programme fits a gap the scheduler pads with adverts or idents and then writes a
`filler` slot (the player shows the test card), noting it in the run log.

### 4.4 Anchors: strips and weekly slots

Real 80s scheduling is about regularity, and it also gives episode order for free.

- Strip: a show airs every weekday at the same time on its channel. One episode per
  day, in order.
- Weekly: once a week, same day and time.
- A series airs only on the channel whose line-up carries it (section 4.2).
- The next episode is whatever follows the latest one already placed (in history or the
  schedule), or the cursor set in the admin UI (`show_cursor`). When a series ends it rests
  for `rest_weeks` (default `series_rest_weeks`, 4) and restarts from the first episode.

Anchors are pinned into the day first; the gaps are filled afterwards.

### 4.5 Music and cartoon channels

- Music videos are catalogue items from pitv_content's `music` sources. The day is a
  sequence of blocks by decade and genre with two full concerts (13:30 and 20:30), covering
  the 1970s to the 2000s: Seventies
  Breakfast, Eighties Pop, Nineties Morning, Disco & Soul, Concert, Noughties, Eighties Chart
  Show, Rock & Metal, Concert, Nineties Indie & Dance, Late Soul (`music_blocks`,
  `music_decades`, editable in Weighting). A block picks videos matching its genre and decade,
  widening to decade only, then anything in the allowed decades, then repeats, so a thin
  library never leaves gaps. Videos are not repeated within 36 hours nor concerts within 14
  days unless nothing else fits. Guides show a block as one programme with the current video
  beneath it.
- Cartoons: the cartoon channel's genre list (Animation, Cartoon, Anime by default) draws
  animated series into its line-up; the catalogue import also tags them with category
  `cartoon`, which the dayparts and admin use. Children's programmes may run all evening
  there. Its adverts are family-safe only: no alcohol, tobacco, adult or gambling brands,
  decided in the order given in section 3.3 and editable per advert.

### 4.6 Sport and the weekend

Sport lives on its own share (a `tv` source with category `sport`) and is meant to be
wrestling, snooker, motorcycle racing and strongman competitions, which is what 1980s ITV
and BBC2 filled Saturday afternoons and midweek late slots with. Sport episodes follow the
same order rule as everything else, by the season and episode numbers in the index. Each
daypart carries a `sport` weight: below 0.5 sport is ineligible unless nothing else fits;
3 and above forms a block in which sport programmes may follow each other, and at weekends
the same sport series may follow itself (`sport_back_to_back_weekends`). The week is
modelled on a mid-80s schedule:

- Weekdays: sport only in the late slot from 22:30 (Sportsnight territory).
- Saturday: children's television all morning, a sport block from 12:30 to 17:15, family
  teatime, prime-time entertainment, sport again from 22:15 (the Match of the Day slot).
- Sunday: quiet morning, an afternoon block from 14:00 or the Sunday film, teatime sport
  from 17:00, drama in the evening.

A programme that would overrun the end of its daypart by more than half an hour is heavily
penalised (sport most of all), so a block never swallows the evening.

### 4.7 Dayparts

Weekday defaults; Saturday and Sunday have their own tables, and a channel can override all
three. Each daypart carries `tv`, `movie`, `kids` and `sport` weights and an optional
maximum programme length.

| Time | Daypart | Prefers |
|---|---|---|
| 08:00 | Breakfast | short, light; kids at weekends (`weekend_kids_breakfast`) |
| 09:30 | Morning | sitcoms, quiz, repeats |
| 12:00 | Lunchtime | short comedies, soaps |
| 13:30 | Matinee | U/PG films |
| 15:30 | Children's | kids' shows, cartoons |
| 17:30 | Early evening | soaps, quiz, light entertainment |
| 19:00 | Prime time | drama, sitcom, entertainment |
| 21:00 | Post-watershed | adult drama, 15 films |
| 22:30 | Late | films, cult, sport |

Certificate and kids rules override any daypart.

### 4.8 Gap filling

For each channel and day, walk from 08:00 to 00:00 following the pattern:

1. If a kept slot or an anchor starts here, place it.
2. Otherwise take the next token. Only programmes in the channel's line-up are candidates,
   plus its external entries when NAS-only is off for it and the day is far enough ahead.
   For a programme token, first choose the kind (TV or film) by the channel's kind weights
   multiplied by the daypart's, then choose an item by weight.
   An item's weight is its era weight (divided by the size of its era pool to the power
   `era_pool_normalise`, 0.5, so eras with few titles are not drowned out), multiplied by the
   daypart weight for its kind, kids and sport factors, the channel's genre weights, a
   penalty (`genre_repeat_penalty`, 0.4) when it shares a genre with the previous programme,
   a bonus (`same_slot_bonus`, 3.0) when the show aired within half an hour of this time
   yesterday, and a bonus for filling the gap within ten minutes. Certificate rules, duration
   (gap plus `duration_tolerance_minutes`, 5) and the same series back to back are hard
   limits. A show may air at most `show_daily_limit` (2) times a day per channel, each
   repeat weighted by `show_repeat_penalty` (0.3).
3. If nothing fits, the rules relax in two steps (ignore daypart preferences and daily
   limits, then allow recently aired films and, as the very last resort, sport outside its
   dayparts); certificates are never relaxed.
4. Adverts must be from the configured decades, prefer a year within
   `advert_year_window` (3) of the surrounding programme, are penalised within
   `advert_repeat_penalty_hours` (6) and never repeat within a quarter of an hour if any other
   fits. Idents prefer the channel's own folder.
5. On advert channels a programme's start is padded with adverts up to the next
   `start_rounding_minutes` (5) boundary, because "19:35" reads right in a listing.
6. The last programme of the day may run `end_of_day_overrun_minutes` (30) past midnight.

Selection is seeded per channel-day from the build's start day, so rebuilding the same week
reproduces it.

### 4.9 Repeat control and overnight

- Films: not within `movie_repeat_days` (21) of any airing on any channel; beyond that,
  weighted by time since last shown. A thin library relaxes this, preferring the film aired
  longest ago.
- Episodes only advance; repeats happen only when a series wraps.
- The `history` table logs every programme that actually aired (written by the player), so
  the scheduler knows what was shown, not just what was planned.
- From 00:00 to 08:00 each channel replays its day from `overnight_replay_from` (08:00 by
  default, per channel), looping a short day, never butting the same series against the
  day's last programme or tomorrow's first, and filling any remainder up to 08:00.

### 4.10 Manual editing

In the admin Schedule tab a slot can be locked or unlocked, removed, replaced with a chosen
programme, or a programme can be inserted at a time or before a slot; each edit rebuilds the
rest of that channel-day around the change (a replaced or inserted programme is locked so
the rebuild keeps it). "Rebuild from here" regenerates the rest of the day. Slots that have
already started and overnight replays cannot be edited; the programme on air is truncated
when something is inserted over its end.

## 5. Player daemon (`pitv-player`)

### 5.1 Structure

```
controller (main thread, half-second loop)
 ├─ clock -> schedule lookup -> "what should be on this channel now"
 ├─ mpv over its JSON IPC socket (loadfile, seek, pause, volume, mute, overlay-add)
 ├─ input: evdev (RF dongle, keyboard, CEC), terminal keys for development, mpv window keys
 ├─ control socket /run/pitv/player.sock: the web service sends the same actions the remote does
 │   and subscribes to the state stream
 ├─ OSD: channel badge, volume bar, guide, messages (Pillow -> BGRA -> overlay-add)
 ├─ history writer and player_state.json (volume, mute, last channel)
 └─ maintenance thread: catalogue import, schedule top-up, delivery reports, cache eviction,
     readiness checks, pruning (pitv/player/maintenance.py)
```

- One long-lived mpv started once, no X: on the Pi `--vo=gpu --gpu-context=drm --fullscreen
  --ao=alsa --hwdec=drm-prime,v4l2m2m-copy --profile=fast --monitoraspect=4:3`, plus
  `--drm-connector` and `--audio-device` when set. `--sub=no`, `--no-config`, a 64 MiB
  demuxer cache with 20 s read-ahead.
- Channel change: `loadfile <path> replace start=<offset>` with a 0.35 s burst of static
  (`channel_switch_static`) to cover the seek.
- Slot boundaries: on each tick, if the current slot has ended, load what is now due. If mpv
  reaches end-of-file before the slot ends (the file is shorter than scheduled), the
  continuity card ("Programmes will continue shortly") holds until the next slot rather than
  reloading past the end. Every five seconds the position is compared with the schedule and
  re-seeked when more than six seconds out.
- Every programme logs one line with where it played from (`cache` or `nas`), hwdec and
  deinterlace choice, and two seconds later one line with what mpv actually did (codec,
  hwdec in use, size, fps, vo, ao, dropped frames).

### 5.2 What plays: the cache, then the NAS, then the card

Everything on air is meant to play from the cache on the attached drive. `MediaCache.locate`
(`pitv/player/cache.py`) resolves a catalogue item, and `play_live`
(`pitv/player/controller.py`) acts on the answer:

1. The cache copy: the path pitv_content reported (`cache_path`), else the `<media_id>_*`
   target it was asked to fill, else, for material that only ever lived in the cache
   (origins `cache` and `online`), the item's own path. A `.part` file or an empty file does
   not count.
2. The NAS original, only when `nas_fallback` is on (the default) and the item came from the
   NAS. The player logs an error when it does this, because pitv_content missed a request,
   and its status (shown on the admin Player page) says it is playing from the NAS.
3. Nothing playable: the player shows the test card with "We are experiencing technical
   difficulties" and "Normal service will be resumed as soon as possible", logs
   `TECHNICAL DIFFICULTIES` with the channel, title and reason, and tries the slot again
   every 30 s in case the file arrives late. The next slot is tried normally when it starts.

If mpv fails on a cache copy that exists (a damaged file), the chain continues from step 2.

`nas_fallback` is a setting because the two positions serve different purposes. On, a missed
delivery costs an error in the log and a read over SMB, though the original may be a file the
Pi can only decode in software, which is why it is the fallback and not the plan. Off, the
NAS is never read at air time, so everything on screen has been through pitv_content and a
delivery failure shows at once instead of being covered up. On is the default because a
picture beats a caption on an ordinary evening; off is for proving the delivery chain.

The player no longer substitutes a programme live. Rebuilding the day at air time changed
what the guide had just promised and hid delivery failures that belong in pitv_content.
Substitution happens only in the readiness checks (section 7.3), hours before air, when a
rebuilt day is still a plan.

### 5.3 Hardware decoding on the Pi 4

The Pi 4 decodes H.264 and HEVC in hardware through V4L2 M2M (`bcm2835-codec`, `rpivid`).
MPEG-2 and VC-1 hardware decode is not available on the Pi 4. Per file, from the video codec
recorded in the catalogue: H.264 and HEVC get `pi_hwdec` (`drm-prime,v4l2m2m-copy`:
zero-copy first, one frame copy as fallback); anything else gets `hwdec=no` without a failed
attempt. On the desktop the player uses `auto-safe`. Deinterlacing is switched on only for
files the catalogue marks as interlaced. Audio passes through untouched.

Transcoding is not PiTV's job. Files the Pi cannot hardware-decode, or more than 1.5 times
576 lines tall, are marked `action: transcode` in the request manifest (section 7.2) and
re-encoded to the CRT profile by pitv_content into the cache, which is the copy the player
plays. The admin Catalogue page shows each file's codec and hardware-decode status, and the
"needs attention" list flags the problems listed in section 3.3. A live preview stream of
what is on air was considered and dropped: it competes with the decoder for the same
hardware.

### 5.4 Remote control

The OSMC RF remote (2.4 GHz USB dongle) presents itself as a USB keyboard, so there is no IR
receiver, no LIRC and no learning of codes: the player reads every keyboard-like input
device through evdev and picks up the dongle if it appears late. Key codes map to actions
through the `keymap` setting; the admin Player page shows the last key pressed and can learn
a button per action. IR remotes set up with `ir-keytable`, HDMI-CEC and a plain keyboard
take the same path.

The OSMC remote has no number, channel or mute keys, so the defaults are:

| Button | Guide closed | Guide open |
|---|---|---|
| Up / Down | Channel up / down (`nav_keys_change_channel`) | Move the channel highlight |
| Left / Right | Volume down / up (`nav_keys_change_volume`) | Previous / next programme (left stops at what is on now) |
| OK | Show the channel badge | Tune to the highlighted channel and close |
| Back | Return to live after pause or restart | Close the guide |
| Home or Menu | Open the guide | Close the guide |
| Info | Channel badge: what is on, progress, what is next | |
| Play/Pause | Pause holds the frame; play resumes behind live until Back or a channel change | Closes the guide and pauses |
| Stop | Mute / unmute | Closes the guide and mutes |
| Vol + / - | Volume with an on-screen bar | Closes the guide and adjusts |
| Rewind (or `r`) | Restart the current programme from its beginning | |

Number keys on any remote tune channels 1 to 9 directly, guide open or not.

### 5.5 On-screen guide

The display is a 4:3 CRT, so overlays sit inside an overscan-safe margin (`osd_safe_margin`,
7%), use larger type (`osd_scale`, 1.25) and are laid out for 576 lines. 4:3 content fills
the screen; widescreen films are letterboxed by mpv.

Teletext look: monospace bold font, the seven teletext colours on black, a blue "P100 PiTV
GUIDE" header with the clock, rendered by Pillow and pushed to mpv as an image overlay so
playback continues underneath. Every channel gets two lines (now, next); the highlighted
channel gets four: the selected programme with its time and title, its subtitle, up to two
lines of plot from the catalogue, and a navigation hint. Up and down move the highlight, left and
right walk that channel's programmes forward and back to now, OK tunes. The overlay
re-renders on every key press and every 30 s so the clock and the "now" marker stay live.

## 6. Web interface (`pitv-web`)

### 6.1 Stack

- Backend: Python, FastAPI on uvicorn, one worker, `sqlite3` with one connection per request
  and per background job. Same package as the catalogue import and the scheduler, so the
  admin UI runs them in-process as background jobs with progress events instead of shelling out. Talks to
  the player over its Unix control socket and relays the player's state stream to the
  browser. Runs as the `pitv` user on port 80 through `CAP_NET_BIND_SERVICE`.
- Frontend: Svelte 5 with Vite, hash-routed, built on the desktop into static files
  committed to the repo (`pitv/web/static/`, about 245 KB of JS and 26 KB of CSS) and served
  by FastAPI. The Pi never runs Node.
- Live data: one Server-Sent Events stream (`/api/events`) carries player state, catalogue
  and schedule changes and job progress. Pages that read file-based status (the Content page,
  auto-refreshing logs) poll every five seconds.
- Auth: the public pages are open on the LAN; the admin area is behind a single password.
  Until a password is set the admin is open (the UI prompts for one). PBKDF2 hash, signed
  cookie valid 30 days, login limited to 8 attempts per five minutes per address.
- Look: clean and minimal with the PiTV logo and a teletext-style clock; works on a phone,
  which is the second remote.

### 6.2 Public pages

| Page | Content |
|---|---|
| `#/` Now & Next | Every channel: what is on with a progress bar (during an ad break, the programme that follows), what is next, live. Tap a channel to tune the TV |
| `#/guide` | EPG grid, channels as rows and time across, one broadcast day at a time with a day picker across the built horizon, a now-line and "Now" button, and a details drawer (episode, year, certificate, plot) |
| `#/remote` | Virtual remote: channels, channel and volume steps, mute, pause, guide keys, plus the player's status. Disabled while the player is offline |

### 6.3 Admin area (`#/admin`)

The admin is organised into two labelled sections, PiTV and pitv_content, so it is obvious
which app a page or setting affects. A page belongs to the app whose code acts on it. Sources
are edited in PiTV's admin but belong to pitv_content, because only pitv_content reads them;
the wanted list stays under PiTV because it is PiTV's data, which pitv_content sees only
through the manifest. The frontend is being reorganised to this layout alongside the move to
the imported catalogue; the tables describe the target.

PiTV:

| Page | What you can do |
|---|---|
| Dashboard | Catalogue counts (cached, NAS only, fetched online), line-up summary per channel with unfetched placeholders and unplaced items, schedule horizon, readiness result, recent runs and jobs; import the catalogue (optionally re-indexing first), build or force-rebuild the week, check readiness |
| Catalogue | The last import (when, from where, counts) with import, re-index and import, and upload an index file; browse shows, episodes, films, adverts, idents and music with where each comes from and whether it is cached; per-show editor (overrides, channel, strip or weekly anchor, rest weeks, category, next-episode cursor, upcoming airings); per-item editor (overrides, channel for films, exclude, family-safe, concert, cache status, recent and upcoming airings); "needs attention" list with inline year and certificate fixes, including items no channel accepts |
| Channels | Add, edit, delete channels; number, name, colour, enabled, description; adverts on/off and per break; pattern editor (add, remove, reorder tokens); allowed and excluded genres as compact multi-select lists with catalogue counts; NAS-only override; era, genre and TV/movie weights; weekday, Saturday and Sunday daypart tables; overnight replay start; content type and family-safe adverts. Each channel's line-up in a drawer: add from a searchable list or by title, remove, move, enable, transient and remove-after-airing toggles, state per entry (on disk, not on disk, fetching, scheduled). Generate, rebalance, export and import line-ups |
| Weighting | PiTV's global defaults: broadcast day, horizon, era and advert era weights, TV/movie balance, watershed times and unknown certificates, kids cutoff, variety and repeat settings, timing, advert rules and the fallback advert keywords, player settings (navigation keys, badge time, static, hardware decoders, audio device, overscan margin, text scale, DRM connector), cache (directory, size cap, NAS fallback) and the pitv_content API address, NAS-only and external scheduling (lead days, episode length, weight, transient retention), maintenance hours (catalogue import, readiness), music blocks and decades; reset to defaults |
| Schedule | The EPG grid, editable: lock, remove, replace, insert at a time or before a slot, rebuild from here; build jobs and notes from the last edit |
| Wanted | The wanted list for pitv_content: requests raised by line-up placeholders (marked with their channel and as transient) and items added by hand (film, episode, advert or music video, optionally with a URL); retry, delete, queue missing episodes |
| Player | Now playing and whether from the cache or the NAS, stream details, virtual remote, cache usage, maintenance status, restart the player; remote keymap editor with press-to-learn |
| Logs | Player, web, catalogue, schedule and install logs, pitv_content's log, and the journal of both units; level and text filters, auto-refresh, copy |
| System | Version, time sync, services, database and disk usage, mounts, jobs; export settings and overrides; set or change the admin password |

pitv_content:

| Page | What you can do |
|---|---|
| Sources | pitv_content's sources through its API: add, edit, enable, disable and remove (id, name, type, category, root, SMB URL), with health (mounted, readable, item count, last indexed) and a folder picker confined to `browse_roots`. Read-only, from the last imported index, when pitv_content is down. A change takes effect at pitv_content's next index; the catalogue import can ask for one straight away |
| Content | Overview (status file, service and timer, last reports, manifest summary), run now, and through its own API its settings (schema-driven form), providers (enable, order, kinds, options, add), catalogue of fetchable titles, jobs and log |

### 6.4 API

Everything the UI does goes through the JSON API so a script or Home Assistant can do the
same: `GET /api/now`, `GET /api/schedule?start=&end=&channel=`, `GET /api/schedule/day/{day}`,
`POST /api/player/key|channel|volume`, `GET /api/catalogue` (last import and index file),
`POST /api/catalogue/refresh` (body `{"reindex": true}` asks pitv_content to re-index
first), `POST /api/catalogue/import` (a schema 2 index document), `GET /api/catalogue/export`,
`GET` and `PUT /api/sources` (a view of pitv_content's sources; edits go through its API),
`POST /api/schedule/build`, CRUD for channels and wanted items, read and edit for shows and
media, `GET /api/content/manifest`, `POST /api/content/report`, `POST /api/content/readiness`,
`GET /api/logs/{name}`. OpenAPI docs are at `/api/docs`. There is no scan endpoint and no
source CRUD: PiTV has nothing to scan and no sources of its own.

## 7. Local cache and pitv_content

The interface between the two apps is [CONTENT_CONTRACT.md](CONTENT_CONTRACT.md) (schema 2),
which both projects agree before either ships a change to it. This section describes how
PiTV uses it; where the two differ, the contract is right and this section is out of date.

### 7.1 The cache on the attached drive

A USB hard drive on the Pi holds the cache (`cache_dir`, `/mnt/cache/pitv` when installed;
capped by `cache_max_gb`, 600). PiTV publishes a request manifest of everything scheduled
through the end of the next broadcast day on every channel, soonest first; pitv_content
copies, transcodes or fetches each file into the cache so tomorrow's television is local
before 08:00. Playback uses the cache copy (section 5.2), so a NAS hiccup at 19:59 does not
interrupt the 20:00 film. The player's maintenance thread evicts least-recently-used files
when the cap is reached or the drive runs short; files in the current manifest and files
under two hours old are protected (`pitv/player/cache.py`).

### 7.2 Division of responsibilities

PiTV owns the catalogue, the schedule and playback; pitv_content owns the sources and
everything needed to get a file into the cache. Both run on the same Pi and share the cache
drive. PiTV never indexes the NAS and never downloads or encodes; pitv_content never decides
what is scheduled and never deletes from the cache. The scheduled services never write to the
NAS: the shares are mounted `ro` and everything pitv_content produces lives under the cache
directory.

| | PiTV | pitv_content |
|---|---|---|
| Owns | the programme catalogue, channel line-ups, the schedule, playback, remote and guide, the web app and admin, cache eviction, readiness | the sources (NAS shares and online providers), the NAS index, finding, fetching, trimming and encoding, placing files in the cache |
| Reads | the library index, delivery reports, the cache, the NAS (read-only, fallback playback only) | the request manifest, the NAS (read-only), online providers |
| Writes | its database and JSON mirrors (`catalogue.json`, `lineups.json`), `<cache>/reports/*.applied` markers; deletes cache files by LRU and transient expiry | the library index (`<cache>/index/library.json`), `<cache>/<media_id>_<stem>.<ext>` (copies and transcodes), `<cache>/acquired/...` (fetched material in Kodi layouts), delivery reports, `pitv_content.status.json`, `logs/pitv-content.log` |
| Calls | pitv_content's local API at `content_tool_url` (library, index, sources, status, run, settings, providers, catalogue, jobs, log); `systemctl start pitv-content.service` as a fallback | `POST /api/content/report`, `POST /api/content/make-room` |

Request manifest (`GET /api/content/manifest`, or `pitv content-manifest --out file` when the
web service is down). Every scheduled file from now through the end of the next broadcast day
is a request, listed once however many slots or channels use it, ordered by `priority` (hours
ahead divided by four) and `deadline_ts` (15 minutes before air). A NAS item is `copy` when it
already decodes in hardware on the Pi and is no taller than 1.5 times the profile's 576 lines,
otherwise `transcode` to `content_profile` (768x576 4:3, H.264, AAC, 4000 kbps, deinterlaced
if interlaced); either way its `target` is `<cache>/<media_id>_<stem>.<ext>`. An item with no
NAS original (a line-up placeholder, or fetched material that has since been evicted) is
`fetch`, with an exact search phrase, extra hints, a typical duration range, a year tolerance
(0 for music, otherwise 2) and a `dest_dir` under `acquire_dir` in the Kodi layout. Items
already in the cache are still listed, with `already_cached`, which is also how eviction knows
what to keep. `wanted` carries requests that are not scheduled yet: adverts and music videos
added by hand, and gaps in a series.

Delivery report (`POST /api/content/report`, or a file dropped in `<cache>/reports/`, which
maintenance applies and marks `.applied`). Each request comes back `done`, `failed` or
`skipped`, with the delivered file's path and properties as pitv_content measured them. PiTV
records the path as `cache_path`; when the delivered length differs from the slot by 30 s or
more it resizes the future slots and rebuilds the rest of that channel-day from the earliest
change. Fetched material carries `meta`, from which PiTV creates its catalogue entry before
binding it to the placeholder slots that asked for it (section 3.4). A report naming a file
that does not exist counts as a failure. Wanted items fail for good after three attempts,
except that "bot check" and "rate limit" failures are re-queued without using one. Schema 1
reports are still read during the transition.

Shared-drive rules. pitv_content writes `.part` files and renames atomically, never deletes,
and touches `<cache>/.pitv_content.running` while working; PiTV ignores `.part` files, never
evicts files under two hours old or in the current manifest, and does not evict while the
marker is fresh (under six hours).

### 7.3 Nightly workflow

Pi local time, as in the contract:

| Time | Who | What |
|---|---|---|
| 01:00 | pitv_content | Main run: re-index the sources and publish the library index, then work the manifest |
| 04:00 | PiTV | Import the index into the catalogue (`catalogue_hour`) and top up the schedule horizon |
| 05:00 | pitv_content | Catch-up run against the current manifest, which now includes what the 04:00 import and top-up added |
| 06:00, 07:00 | PiTV | Readiness checks (`readiness_hours`) |

PiTV also imports whenever the index file changes, so a re-index started from the admin is
schedulable within ten minutes rather than the next morning.

The readiness checks (`pitv/readiness.py`) cover every slot from now through the end of the
next broadcast day. A programme is ready when its cache copy exists. With `nas_fallback` on,
one whose NAS original is there also counts, logged as a warning, because it will play from
the NAS and pitv_content missed it. Anything else is an error: a file in neither place, or a
line-up placeholder pitv_content has not filled. Its channel-day is rebuilt from the first
failure using only programmes that are playable now (cached, or on a mounted share with
fallback on) and without new external placements, and the rebuild is logged. The check also
runs on the first maintenance pass after boot and on demand (Dashboard, `POST
/api/content/readiness`, `pitv readiness`). Nothing is substituted at air time: a programme
still unplayable when it comes on shows the technical difficulties card (section 5.2).

### 7.4 pitv_content in the admin

pitv_content has no interface of its own, so its pages sit in PiTV's admin under their own
heading (section 6.3). The Sources page edits its sources through its API. The Content page
reads its status file, service and timer state, log and reports, starts a run, and proxies its
local API (`/api/content/tool/api/*`) for settings, providers, its catalogue of fetchable
titles and jobs.

## 8. Boot and system setup

`setup/boot-trim.sh` (run by `install.sh`) does the following:

- `config.txt`: `boot_delay=0`, `disable_splash=1`, `hdmi_drive=2`, `disable_overscan=1`,
  `max_framebuffers=2`, `dtoverlay=disable-bt`, `dtparam=watchdog=on`, and the display block
  for `DISPLAY_MODE`: `hdmi576` (default: `hdmi_group=1`, `hdmi_mode=17`, 720x576p 50 Hz 4:3
  for the HDMI-to-SCART converter), `composite` (`enable_tvout=1`, PAL, 4:3,
  `vc4-kms-v3d,composite=1`), `hdmi43` (1024x768 for a 4:3 monitor) or `hdmi`.
- `cmdline.txt`: `quiet loglevel=3 vt.global_cursor_default=0 consoleblank=0`, no splash.
- Disabled: bluetooth, hciuart, avahi-daemon, triggerhappy, ModemManager, apt timers,
  man-db timer, rpi-eeprom-update, dphys-swapfile, cups, wpa_supplicant,
  systemd-networkd-wait-online. NetworkManager-wait-online capped at 10 s. journald capped at
  50 MB. `systemd-timesyncd` and `fake-hwclock` enabled. Hardware watchdog with
  `RuntimeWatchdogSec=15`.
- Services: `pitv-splash` (sysinit, draws the test card straight onto `/dev/fb0`),
  `pitv-player` (after network-online, time sync and the CIFS automounts, which it does not
  require), `pitv-web` (after the player; does not delay the picture). The nightly catalogue
  import, schedule top-up, readiness checks, report pick-up and cache eviction run from the
  maintenance thread inside the player, so PiTV needs no timers.
- Data in `/var/lib/pitv/pitv.db` (on the installer image `/var/lib/pitv` is a symlink into
  the work partition); rotating log files under `/var/lib/pitv/logs/` and the journal.

## 9. Installing: the SD-card installer

The image is DietPi with both apps' source trees baked in; `installer/` holds the image
build, the first-boot provisioning script and a Go installer for Linux and Windows. The card
is split into a fixed-size system partition and a work partition (logs, databases, journal)
so nothing that grows can fill the system. Modes: `clean` (card and USB drive wiped),
`normal` (card only), `upgrade` (new code over SSH, everything else kept). First boot sizes
the partitions, prepares the USB drive, creates the maintenance SSH user, installs both apps
and writes `/work/install/install.log`, which the admin shows under Logs.

The installer's share list becomes pitv_content's sources. PiTV's `setup/install.sh` mounts
the shares and writes them to `/etc/pitv/nas-sources.json` (id, name, type, category, root,
SMB URL); first boot passes that file to pitv_content's installer as `NAS_SOURCES`, which
seeds its sources on its first install. PiTV itself keeps only the cache settings
(`cache_dir`, `acquire_dir`). See [installer/README.md](../installer/README.md) and
[setup/README.md](../setup/README.md).

## 10. Resilience: 24/7 operation and recovery

- Watchdogs. Both services are `Type=notify` with `WatchdogSec` (player 90 s, web 60 s):
  the player pings systemd from its main loop every half second, the web service from an
  async heartbeat; a hung process is killed and restarted (`Restart=always`, no start-rate
  limit). The hardware watchdog (`dtparam=watchdog=on`, `RuntimeWatchdogSec=15`) reboots the
  board on a kernel or systemd hang.
- Memory. Units carry `MemoryHigh` and `MemoryMax` (player 900M/1200M, web 400M/600M). The
  player logs its own and mpv's RSS every five minutes and exits for a clean restart above
  `memory_limit_mb` (700), or when mpv exceeds 1.5 times that; playback resumes at the live
  position within seconds. The web process does the same above 400 MB. History older than
  `history_keep_days` (180), schedule older than 14 days and run logs older than 30 days are
  pruned; log files rotate.
- Power loss. The schedule is in SQLite (WAL, `synchronous=NORMAL`) and the state file is
  small, so nothing needs replaying. The player derives its position from the wall clock, so
  after a two-hour outage it tunes to what is on now (section 2 covers the clock wait). The
  first maintenance pass after boot imports the index file and runs a readiness check.
  pitv_content resumes where it left off: it skips targets that already exist, cleans stale
  `.part` files, and its catch-up run and PiTV's readiness checks cover anything a reboot
  interrupted.
- Missing content. The readiness checks at 06:00 and 07:00 find what pitv_content has not
  delivered and rebuild those channel-days from what is playable, hours before air. At air
  time the player plays the cache copy; if it is missing or unplayable it logs an error and,
  with `nas_fallback` on, plays the NAS original; failing that it shows the technical
  difficulties card, logs it and retries every 30 s (section 5.2). A missing file is always
  recorded in the logs, even when the fallback hides it from the viewer.
- pitv_content down. PiTV keeps broadcasting from the catalogue and cache it has. Imports fall
  back to the index file on the cache drive, the Sources page shows the last imported sources
  read-only, and the manifest is still served for when pitv_content comes back. Anything it
  fails to deliver meanwhile shows up at the next readiness check.
- Storage. The cache drive is mounted `nofail` so a missing disk never blocks boot; the
  player does not create parent directories for the cache, so an unmounted drive means no
  cache rather than a cache on the SD card. Without the cache every programme needs the NAS
  fallback. The NAS shares are automounts that retry; with the cache in place a NAS outage
  only matters for programmes pitv_content has not yet delivered.

## 11. Code layout

```
PiTV/
├── pitv/                      Python package (3.11+): FastAPI, uvicorn, sqlite3, Pillow, evdev on the Pi
│   ├── config.py              bootstrap settings only (paths, sockets, web port); everything else lives in the DB
│   ├── db.py                  SQLite schema, migrations, default settings and channels, helpers
│   ├── catalogue.py           import of pitv_content's library index into sources, shows and media; catalogue.json mirror
│   ├── tool_client.py         HTTP client for pitv_content's local API (used by the admin and the maintenance thread)
│   ├── content.py             PiTV's half of the contract: request manifest and delivery reports (section 7)
│   ├── readiness.py           is everything through tomorrow playable; rebuild what is not
│   ├── guide.py               "what's on" lookups shared by the OSD guide and the web API
│   ├── lineup.py              channel line-ups: generation by genre, editing, JSON mirror, deliveries, transient clean-up
│   ├── wanted.py              the wanted list (gap detection)
│   ├── scheduler/             build.py (week builder, anchors, gap fill, music, overnight, rebuild), rules.py, listing.py
│   ├── player/                controller.py, mpv_ipc.py, hwdec.py, input.py, control_socket.py, osd.py, cache.py, maintenance.py
│   ├── web/                   app.py, api/ (public, admin, wanted, content, settings_rules, deps), events.py (SSE), auth.py, tasks.py, player_client.py, static/
│   ├── logsetup.py, sdnotify.py, splash.py, devtools.py (fake library and its index)
│   └── cli.py                 pitv init | fake-library | catalogue | schedule | listing | reset-schedule | content-manifest | content-report | readiness | web | play
├── web/                       Svelte + Vite source; npm run build writes pitv/web/static/
├── systemd/                   pitv-player.service, pitv-web.service, pitv-splash.service, mount and automount templates
├── setup/                     install.sh (Pi provisioning), boot-trim.sh, dev.sh (desktop helper)
├── installer/                 SD-card image build, first-boot script, Go installer CLI
├── tests/                     pytest suite against a generated fake library and its index (tiny ffmpeg clips)
└── docs/                      PLAN.md (this file), REQUIREMENTS.md (checklist), CONTENT_CONTRACT.md (interface with pitv_content)
```

Development happens on the desktop with a generated fake library and a `--now` clock
override, so a week can be built and inspected without the NAS or pitv_content:
`pitv fake-library DIR --import` writes tiny H.264 clips, writes the schema 2 index
pitv_content would publish for them, and imports it. The windowed player is a 768x576 4:3
preview of the Pi's picture with the same scaler and file resolution. The test card is
generated into the data directory on first use.

Database tables: `meta`, `settings`, `sources` (a mirror of pitv_content's sources: `uid`,
`location`, `last_indexed_at`, `index_summary`), `channels`, `shows`, `media` (episodes,
films, adverts, idents and music videos, with `uid`, `origin`, `path`, `cache_path` and
overrides in a JSON column), `show_cursor`, `lineup`, `schedule` (placeholder slots carry a
`wanted_id`), `history`, `wanted`, `run_log`. The old `probe_cache` table is dropped on
upgrade.

## 12. Open questions

1. Metadata coverage: certificates and genres come from the index, so they exist only where
   pitv_content finds them on the shares. Without them the admin overrides are the only
   source; unknown films default to post-watershed and unknown episodes to PG.
2. Mid-programme ad breaks on the commercial channels: adverts are only placed between
   programmes. The slot model already allows a split at the halfway point.
3. Episode numbering for fetched series with nothing on disk starts at series 1, episode 1.
   A delivery is filed against the season and episode that were requested, so a series whose
   real numbering matters should have one episode on disk first.
4. Web exposure: LAN only is assumed; the admin password is the only protection and there
   is no HTTPS. A reverse proxy would be needed before exposing it further.
