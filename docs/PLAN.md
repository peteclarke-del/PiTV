# PiTV — Design Plan

A Raspberry Pi 4 that behaves like a 1980s UK television: four (or more) continuously
"broadcasting" channels, a weekly schedule built from the NAS library, an infra-red remote,
a teletext-style on-screen guide, and a web interface for viewing the schedule and
administering everything. Nothing here is code yet; this document is the design to agree
before implementation starts.

---

## 1. Goals and constraints

| Requirement | Decision |
|---|---|
| Hardware | Raspberry Pi 4, 4 GB, composite PAL out (or HDMI via converter) to a 14\" 4:3 colour CRT, OSMC RF remote, USB HDD cache, wired Ethernet to the Synology NAS (SMB) |
| OS | Raspberry Pi OS Lite 64-bit (Bookworm), no desktop, boots straight into the player |
| Video decode | Pi 4 hardware H.264 (and HEVC) via V4L2 M2M; see §5.2 |
| Boot | Target ~15 s from power to picture; test card shown while the NAS mounts |
| Channels | Defined in the admin UI, each can be enabled/disabled. Default six: 1 and 2 programmes only, 3 and 4 with adverts, 5 music (blocks by decade/genre, two concerts a day), 6 cartoons (family-safe adverts only) |
| Broadcast day | 08:00–00:00 scheduled; 00:00–08:00 replays that day's schedule from 08:00 (see §4.6) |
| Era | Programmes: any age, with a healthy mix either side of 1980 (weights editable per channel). Adverts: 1980s/1990s only. Year from folder/file names, NFO, or overrides |
| Watershed | 21:00. UK certificate rules: U/PG any time, 12 not before 20:00, 15 not before 21:00, 18 not before 22:00 |
| Horizon | 7 days generated at a time; episodes advance in order per show; minimal repeats week to week |
| Remote | Channel 1–4 direct, channel +/−, volume +/−, mute, pause/play, guide (menu), up/down/left/right/OK/back |
| Web | Fast, reactive, clean: public schedule view plus an admin area for sources, channels, schedules, weighting, patterns, player control |
| Overhead | One mpv process, one small Python player daemon, one small Python web service, SQLite. No X, no Kodi |

---

## 2. How "broadcast" works (the core idea)

Each channel has a schedule: an ordered list of *slots* `(channel, start, end, media file,
offset into file, kind)` stored in SQLite. Channels are virtual. Only the channel you are
watching is actually decoded. At any instant the player computes, from the wall clock,
which slot each channel is in and how far through it is. Changing channel means:
load that channel's current file in mpv and seek to `now − slot.start + slot.offset`.

Consequences:

- All channels appear to run all the time, even while the Pi is off. Turn it on at 19:42
  and channel 1 is 12 minutes into whatever started at 19:30.
- A programme split into parts (for a mid-programme ad break) is just two slots pointing
  at the same file with different offsets. The model supports that from day one even if
  v1 only puts adverts between programmes.
- The on-screen guide and the web schedule both read the same schedule table, so they are
  always "real time" and always agree.
- The Pi has no real-time clock, so NTP must succeed on boot for the clock to be right.
  If the network is late the player still runs; it just realigns when time syncs.

---

## 3. Content library

### 3.1 Sources (all on the Synology NAS, over SMB)

Sources are rows in the database, managed in the admin UI, not hard-coded paths.
Each source has a type (`tv`, `movie`, `advert`, `ident`), a mount path, an enabled flag
and a last-scanned time. Initial sources:

| Share | Type | Expected layout | Notes |
|---|---|---|---|
| `smb://synologynas/tvshows/` | tv | `Show Name (1984)/Season 02/Show Name - S02E05 - Title.mkv` | Season/episode from `SxxEyy` (also `2x05`, `Season 2/05 - Title`). Show year from folder |
| `smb://synologynas/movies/` | movie | `Title (1985)/Title (1985).mkv` | Year from folder, then file name |
| `smb://synologynas/ads/` | advert | `1984/Product.mp4` or `1984 - Product.mp4` | Advert year from the sub-folder or a leading year in the file name |
| (optional) any folder | ident | `ch1/*.mp4` | Channel idents; the folder name gives the channel number |

Both shares require authentication (guest access is denied), so the Pi mounts them with
CIFS using a root-only credentials file at `/etc/pitv/smb-credentials`, via systemd
automount units so boot never blocks on the network. Recommended mount options:
`vers=3.0,ro,noserverino,cache=loose,actimeo=60,_netdev,x-systemd.automount,x-systemd.idle-timeout=0`.
Adding a source in the admin UI records the SMB path; the mount unit for it is generated
by the install tooling (mounting needs root, the web service does not run as root).

### 3.2 Scanner (`pitv scan`)

- Walks every enabled source, parses show/season/episode/year from names.
- Reads Kodi-style `tvshow.nfo` / `<movie>.nfo` when present for premiered year,
  certificate (`mpaa`), genres, and plot.
- Probes duration **and video codec** with `ffprobe` once per file; cached in SQLite keyed
  on path, size, mtime so re-scans are fast. Codec is recorded so the admin UI can show
  which files will hardware-decode (H.264/HEVC) and which will fall back to software.
- Overrides (year, certificate, genre, daypart preference, exclude, strip/weekly hint,
  home channel) are edited in the admin UI and stored in the database; a YAML import/export
  exists for backup.
- Runs nightly (systemd timer), on demand from the admin UI (with live progress), and
  after a source is added. New content is only used for future days.

### 3.3 Classification rules

| Attribute | Source order | Default when unknown |
|---|---|---|
| Year | folder → file name → NFO → override | excluded (not date-appropriate), flagged in admin "needs attention" list |
| Certificate | NFO → override → filename tag `[15]` | Movies: treat as 15 (post-watershed). TV: treat as PG |
| Genre / audience | NFO genres → override | "general" |
| Kids content | genre Animation/Children/Family or override | no |

Era eligibility and weights are global defaults with per-channel overrides, all editable
in the admin UI. Defaults: programmes 1920–1979 weight 0.45, 1980–1989 0.40, 1990–1999 0.15
(unknown year 0.2); adverts 1980–1989 0.85, 1990–1999 0.15 and nothing outside that.
A show counts as 80s if it premiered in the window; a per-show override handles long
runners (e.g. a show that started in 1978 and ran to 1986).

---

## 4. Scheduler (`pitv schedule`)

Builds 7 days for every enabled channel into the `schedule` table. Never rewrites slots in
the past, the one currently airing, or slots an admin has locked. Runs when the remaining
horizon drops below 2 days (weekly in practice), and on demand from the admin UI for a
whole week, one day, or one channel.

### 4.1 Channels are data

A channel row holds: number, name, short name, logo, enabled, **pattern**, **era weights**,
**genre weights**, **daypart profile**, ads enabled, advert count per break, ident
behaviour, and a colour for the guide. Defaults ship for four channels:

| Ch | Style | Pattern | Content lean |
|---|---|---|---|
| 1 | Mainstream (BBC1-ish) | `show` | Drama, sitcom, light entertainment, afternoon films |
| 2 | Alternative (BBC2-ish) | `show` | Documentaries, cult, older films, comedy |
| 3 | Commercial (ITV-ish) | `show, ad, ad` | Soaps, quiz, action drama, kids' teatime |
| 4 | Commercial (C4-ish) | `show, ad, ad` | Alternative comedy, imports, films, late night |

Leans are soft weights, not hard rules; with a small library they relax automatically.

### 4.2 Patterns

A pattern is an ordered list of tokens the channel cycles through when filling gaps:

| Token | Meaning |
|---|---|
| `show` | any programme (TV episode or movie, chosen by daypart and weights) |
| `tv` | a TV episode specifically |
| `movie` | a movie specifically |
| `ad` | one advert |
| `ident` | a channel ident / continuity clip |
| `break` | shorthand for the channel's configured advert count (e.g. 2 ads) |

Examples: `show, ad, ad` (ITV-style), `show, show, movie, ad, ad`, `tv, tv, ident, movie`.
Anchored shows (§4.3) are placed first and count as a `show` token where they land, so the
pattern resumes cleanly after them. If a token cannot be satisfied (no adverts in the
library, no movie fits the gap) the scheduler skips that token and notes it in the run log.

### 4.3 Anchors: strips and weekly slots

Real 80s scheduling is about regularity, and it also gives episode order for free.

- **Strip**: a show airs every weekday at the same time on its home channel
  (soaps, kids shows, daytime sitcoms). One episode per day, in order.
- **Weekly**: a show airs once a week, same day and time (prime-time drama, sitcoms).
- Each show is assigned a *home channel* (automatically, or pinned in the admin UI) so
  the same series never appears on two channels.
- A persistent per-show cursor (`show_cursor` table) records the next episode; it advances
  every time an episode is placed. When a series ends it rests for a configurable number
  of weeks, then restarts from S01E01. The cursor can be reset or moved in the admin UI.

The scheduler first pins anchors into the week, then fills the remaining gaps.

### 4.4 Music and cartoon channels

Channels have a `content` type: `general`, `music` or `cartoons`, and can be enabled or
disabled individually. Defaults ship six: four general, **PiTV Music** (5) and **PiTV Toons** (6).

- **Music** comes from `smb://synologynas/music videos/` (source type `music`,
  `<Genre>/<Artist> - <Title> (<Year>).mp4`, concerts under `Concerts/<Genre>/` or anything
  over 35 minutes). The day is a sequence of **blocks** by decade and genre with two full
  concerts (13:30 and 20:30 by default), covering the 1970s to the 2000s inclusive:
  Seventies Breakfast, Eighties Pop, Nineties Morning, Disco & Soul, Concert, Noughties,
  Eighties Chart Show, Rock & Metal, Concert, Nineties Indie & Dance, Late Soul. A block picks
  videos matching its genre and decade, widening to decade-only, then anything in the allowed
  decades, then repeats, so a thin library never leaves gaps. Guides show a block as one
  programme with the current video beneath it.
- **Cartoons**: series with an Animation/Cartoon/Anime genre are routed to the cartoon channel
  automatically (or set a show's category to `cartoon`). Children's programmes may run all
  evening there. Its adverts are **family-safe only**: adverts are flagged from folder and
  keyword heuristics (alcohol, tobacco, adult, gambling brands) and from `<tag>`s in their NFO,
  editable per advert in the admin UI.

### 4.5 Sport and the weekend

Sport lives on its own share (`smb://synologynas/tvsports/`, a `tv` source with category
`sport`) and is limited to wrestling, snooker, motorcycle racing and strongman competitions,
which is what 1980s ITV and BBC2 actually filled Saturday afternoons and midweek late slots
with. Sport shows follow the same date-order rule as everything else (files named by date,
e.g. `World of Sport Wrestling - 1985-03-16.mp4`, sort by year then month/day). Each daypart
carries a `sport` weight: below 0.5 sport is ineligible unless nothing else fits, above 3 it
forms a block. The week is modelled on a typical mid-80s schedule:

- Weekdays: sport only in the late slot (Sportsnight / Midweek Sports Special territory).
- Saturday: children's television all morning, a sport block from 12:30 to about 17:15
  (wrestling, racing, snooker), family teatime, prime-time entertainment, sport again late
  (the Match of the Day slot), a film after that.
- Sunday: quiet morning, an afternoon block from 14:00 (snooker, motorcycle racing) or the
  Sunday film, teatime sport, drama in the evening.

At weekends two or more sport programmes may run back to back, including the same series;
a programme may not overrun the end of its daypart by more than about half an hour, so a
block never swallows the evening.

### 4.6 Dayparts (weekday defaults, editable per channel)

| Time | Daypart | Prefers |
|---|---|---|
| 08:00–09:30 | Breakfast | short, light, kids on weekends |
| 09:30–12:00 | Morning | sitcoms, quiz, repeats |
| 12:00–13:30 | Lunchtime | short comedies, soaps |
| 13:30–15:30 | Matinee | U/PG films |
| 15:30–17:30 | Children's | kids shows, cartoons |
| 17:30–19:00 | Early evening | soaps, quiz, light entertainment |
| 19:00–21:00 | Prime time | drama, sitcom, entertainment |
| 21:00–23:00 | Post-watershed | adult drama, 15 films |
| 23:00–00:00 | Late | films, cult, 18 films |

Hard rules override any daypart: certificate vs. time, and "no kids-only content after
21:00".

### 4.7 Gap filling

For each channel and day, walk from 08:00 to 00:00 following the channel's pattern:

1. If an anchor starts here, place it.
2. Otherwise take the next pattern token and pick a candidate for the gap up to the next
   anchor: eligible era, certificate OK for the start time, daypart weight, duration fits
   (with a tolerance so a 52-minute drama can overrun a 50-minute gap and push the next
   unanchored item), not the same show already today on this channel, not the same genre
   as the previous item.
3. Score candidates by days-since-last-aired (from `history`), variety, daypart fit and
   the channel's weights; pick with weighted randomness seeded per week so re-runs are
   reproducible.
4. Adverts are chosen with a year within ±3 of the surrounding programme when possible.
5. Small leftover gaps (< shortest eligible item) are filled with idents/adverts or simply
   absorbed: 80s schedules happily started programmes at 19:35.

Start times are rounded to 5 minutes where padding allows, because "19:35" reads right in
a listing and "19:37" does not.

### 4.8 Repeat control and overnight

- Movies: never twice in one week; across weeks, weighted by time since last airing.
- Episodes: only ever advance; repeats only happen when a series wraps.
- The `history` table logs every slot that actually aired (written by the player) so the
  scheduler knows what was really shown, not just what was planned.
- From 00:00 to 08:00 each channel replays that day's schedule starting from the 08:00
  slot, cut off at 08:00 when the new day begins. The replay start time is a per-channel
  setting (e.g. replay from 19:00 instead).

### 4.9 Manual editing

In the admin UI a slot can be: locked (regeneration leaves it alone), replaced with a
chosen item (following items shift), removed, or moved to another time; a specific show
can be dropped into a time on a day, and "regenerate from here" rebuilds the rest of that
day. Edits touching the past or the current slot are refused.

---

## 5. Player daemon (`pitv-player`, the systemd service)

### 5.1 Structure

```
controller (state machine, 1 Hz tick)
 ├─ clock → schedule lookup → "what should be on channel N right now"
 ├─ mpv via JSON IPC socket (loadfile, seek, pause, volume, mute, overlays)
 ├─ input: evdev (IR remote via kernel gpio-ir + ir-keytable; keyboard for dev; CEC optional)
 ├─ control socket (/run/pitv/player.sock): the web service sends the same commands the remote does
 ├─ OSD: channel badge, volume bar, guide (Pillow → BGRA → mpv overlay-add)
 └─ history writer (logs what aired) + state publisher (channel, slot, position, volume)
```

- One long-lived mpv process started once with no X, driven over
  `--input-ipc-server=/run/pitv/mpv.sock`, OSD enabled, audio via ALSA to HDMI.
- Channel change = `loadfile <path> replace start=<offset>`; a short local "static"
  clip (or a 300 ms burst overlay) covers the seek latency and looks the part.
- Slot boundaries: on each tick, if the current slot has ended, load the next slot. If
  mpv reaches end-of-file early (bad duration), the tick loads whatever is now due.
- Drift check: if the schedule position and mpv position differ by more than a few
  seconds while not paused, re-seek.

### 5.2 Hardware decoding on the Pi 4

The Pi 4 decodes H.264 (up to 1080p60) and HEVC (up to 4K) in hardware through the
kernel V4L2 memory-to-memory interface (`bcm2835-codec` and `rpivid`). MPEG-2 and VC-1
hardware decode is **not** available on the Pi 4, so DVD-sourced MPEG-2 rips decode in
software; at SD resolution that is a light load on the Pi 4's CPU.

mpv configuration, in order of preference, verified on the Pi during Phase 7:

1. `--vo=gpu --gpu-context=drm --hwdec=drm-prime` — zero-copy: decoded frames go straight
   to the display without touching the CPU. Needs the Raspberry Pi ffmpeg build (in
   Raspberry Pi OS Bookworm's own `ffmpeg`/`mpv` packages this is present).
2. `--vo=gpu --gpu-context=drm --hwdec=v4l2m2m-copy` — hardware decode with one frame
   copy; works with any ffmpeg that has V4L2 M2M enabled.
3. `--hwdec=no` software fallback, automatic per file when the codec is unsupported.

`--hwdec` is set per file from the codec recorded by the scanner, so an H.264 file gets
the hardware path and an MPEG-2 file goes straight to software without a failed attempt.
Deinterlacing (`--deinterlace=yes`, `--vf=bwdif`) is enabled only for files the scanner
reports as interlaced, because most 80s rips are interlaced SD and the Pi has no
hardware deinterlacer. Audio passes through untouched.

The admin library page shows each file's codec and whether it will hardware-decode, and
the "needs attention" list flags anything the Pi may struggle with (e.g. 1080p MPEG-2).

**Transcoding is not PiTV's job.** Files the Pi cannot hardware-decode (MPEG-2, VC-1,
MPEG-4 ASP) or that are far above 576 lines are marked `action: transcode` in the content
manifest (§7) and re-encoded to the CRT profile by pitv_content into the cache; the player
prefers that copy, so everything on air takes the hardware decode path. A live preview
stream of what is on air was considered and dropped: it competes with the decoder for the
same hardware and the phone would show a stream of the TV you are sitting in front of.

### 5.3 Remote control

The remote is the **OSMC RF remote** (2.4 GHz USB dongle). It presents itself to Linux as an
ordinary USB keyboard, so there is no IR receiver, no LIRC and no learning of codes: the
player reads every keyboard-like input device through evdev and hot-plugs the dongle if it
appears late. Key codes map to actions through the `keymap` setting; the admin Player page
shows the last key pressed so any button can be reassigned by pressing it. IR remotes set up
with the kernel driver and `ir-keytable`, HDMI-CEC and a plain keyboard take the same path.

The OSMC remote has no number, channel or mute keys, so the defaults are:

| Button | Guide closed | Guide open |
|---|---|---|
| Up / Down | Channel up / down | Move channel highlight |
| Left / Right | Volume down / up | Previous / next programme (left stops at what's on now) |
| OK | Show channel badge | Tune to the highlighted channel and close |
| Back | Return to live after pause/restart | Close guide |
| Home or Menu | Open the guide | Close guide |
| Info (i) | Channel badge: what's on, progress, what's next | |
| Play/Pause | Pause holds the frame; play resumes (channel runs behind live until Back or a channel change) | |
| Stop | Mute / unmute | |
| Vol + / − | Volume, with on-screen bar | |

Both "up/down change channel" and "left/right change volume" are settings, and any
remote with number keys tunes channels 1–9 directly.

### 5.4 On-screen guide

The display is a 4:3 CRT, so overlays sit inside a configurable overscan-safe margin (7%
by default), use larger type, and are laid out for 576 lines. 4:3 content fills the screen;
widescreen films are letterboxed by mpv.

Teletext / Ceefax look: block font, 8-colour palette, black background, rendered by Pillow
and pushed to mpv as an image overlay so playback continues underneath. Layout:

```
 PiTV GUIDE                                   Tue 19:42
 ┌─ 1 PiTV One ─────────────────────────────────────────┐
 │ ▶ 19:30–20:00  Show Name  S02E05 "Episode Title"     │
 │   20:00–20:50  Next programme ...                    │
 ├─ 2 PiTV Two ─────────────────────────────────────────┤
 │   19:00–20:35  Film Title (1983)  PG                 │
 ├─ 3 PiTV Three ───────────────────────────────────────┤
 ...
```

Up/down highlights a channel; left/right walks that channel's programmes forward and back
to "now". A plot line from the NFO shows for the highlighted entry when available. The
overlay re-renders on every key press and every 30 s so the clock and "now" marker stay
live.

---

## 6. Web interface (`pitv-web`, second systemd service)

### 6.1 Stack

- **Backend**: Python, FastAPI on uvicorn, one worker, async SQLite. Same package as the
  scheduler, so the admin UI calls the scanner and scheduler in-process (as background
  tasks with progress events) instead of shelling out. Talks to the player over its Unix
  control socket. Runs as the `pitv` user, not root.
- **Frontend**: Svelte 5 + Vite, built on the desktop into static files committed to the
  repo (`pitv/web/static/`), served by FastAPI. The Pi never runs Node. Bundles are tens of
  kilobytes, first paint is immediate, and every view is reactive to live data.
- **Live data**: one Server-Sent Events stream (`/api/events`) pushing player state,
  now-playing changes, scan/schedule progress and schedule edits. No polling.
- **Auth**: the schedule view is open on the LAN; the admin area is behind a single
  password (set on first run), session cookie, HTTPS optional via a reverse proxy if
  ever exposed. Rate-limited login.
- **Look**: clean and minimal by default with a subtle 80s accent (the PiTV logo, a
  teletext-style clock); optional full "Ceefax mode" theme for the public guide page.
  Works on a phone: the phone is the second remote.

### 6.2 Public pages

| Page | Content |
|---|---|
| `/` Now & Next | Every channel: what's on with a progress bar, what's next, live via SSE. Tap a channel to tune the TV |
| `/guide` | EPG grid: channels as rows, time across, now-line, scrolls back to the start of the current programme and forward through the whole generated week; day picker; click for details (episode, year, certificate, plot) |
| `/remote` | Virtual remote: channels, ch±, vol±, mute, pause, guide keys; same commands as the IR remote |

### 6.3 Admin area (`/admin`)

| Section | What you can do |
|---|---|
| Dashboard | Player state, schedule horizon, last scan, items needing attention, quick actions (scan now, build week, restart player) |
| Sources | Add/edit/remove tv, movie, advert and ident sources (path, type, enabled); scan one or all with live progress and results |
| Library | Browse shows, episodes, movies, adverts; search/filter; edit per-item overrides (year, certificate, genre, kids, exclude, home channel, strip/weekly, rest weeks); reset episode cursor; see codec and hardware-decode status; "needs attention" list (no year, no certificate, unsupported codec) |
| Channels | Add/remove/reorder channels; name, number, logo, colour, enabled; ads on/off and ads per break; pattern editor (drag tokens: show/tv/movie/ad/ident/break); era weights; genre weights; daypart profile editor; overnight replay start |
| Weighting | Global defaults for era weights, movie/TV balance, repeat penalties, variety rules, watershed certificate times, start-time rounding; per-channel overrides live in Channels |
| Schedule | The same EPG grid, editable: lock, replace, remove, move, insert a specific show/movie, regenerate a day/channel/week from here; diff preview before applying; run log with skipped tokens and constraint warnings |
| Player | Now playing with position; virtual remote; volume; restart player; view recent history (what actually aired) |
| System | Time sync status, mounts and free space, service status, journal tail, backup/restore of the database and overrides export, software update |

### 6.4 API

All UI actions go through a JSON API (`/api/...`) so a script or Home Assistant can do the
same things: `GET /api/now`, `GET /api/schedule?channel=&from=&to=`, `POST /api/player/key`,
`POST /api/scan`, `POST /api/schedule/build`, CRUD for sources/channels/overrides. OpenAPI
docs are served automatically by FastAPI at `/api/docs`.

---

## 7. Local cache and acquiring missing programmes

### 7.1 The cache on the attached drive

A 1 TB USB hard drive on the Pi holds a cache (`cache_dir`, capped by `cache_max_gb`, default
600 GB). PiTV publishes a manifest of everything scheduled through the end of the next
broadcast day on every channel, soonest first; pitv_content copies (or transcodes) those files
into the cache so tomorrow's television is local before it starts at 08:00. A day of four
channels is roughly 60–150 GB depending on bitrates. Playback always prefers the cached copy,
then a transcoded copy, then the NAS original, so a NAS hiccup at 19:59 never interrupts the
20:00 film. The player's maintenance thread evicts least-recently-used files when the cap is
reached; files in the current manifest are protected (`pitv/player/cache.py`).

### 7.2 Division of responsibilities: PiTV and pitv_content

PiTV schedules, requests and streams; the separate **pitv_content** tool (project
`PiTV_content`, formerly `PiTV_ads`) finds, fetches, trims, encodes and places content. Both
run on the same Pi and share its cache drive. The NAS is read-only: PiTV mounts the shares
read-only and nothing is ever written to them; everything pitv_content produces lives on the
Pi's cache drive. PiTV contains no download or encoding code of its own.

| | PiTV | pitv_content |
|---|---|---|
| Owns | schedule, player, remote, guide, web app, cache eviction, readiness | search, download, trimming, encoding to the CRT profile, filing |
| Reads | NAS shares (read-only), `<cache>` and `<cache>/acquired/...` | `GET /api/content/manifest` (or `pitv content-manifest`) |
| Writes | the database, `<cache>/reports/*.applied` markers; deletes LRU cache files | `<cache>/<media_id>_<stem>.<ext>` (copies/transcodes), `<cache>/acquired/...` (fetched items in Kodi layouts), `pitv_content.status.json`, `logs/pitv-content.log`, `reports/*.json` |
| Talks | `POST /api/content/report`, `/api/content/tool` (status), `systemctl start pitv-content.service` | `POST /api/content/report`, `POST /api/content/make-room` |

**Contract.** The manifest (`schema: 1`) lists every file scheduled from now through the end
of the next broadcast day: `media_id`, Pi path, `share`/`relpath`/`remote`, codec, size,
interlacing, `channels`, `first_air_ts`, `deadline_ts`, `priority`, and a `target`
(`<cache>/<media_id>_<stem>.<ext>`) with `action` `copy` (already H.264/HEVC) or
`transcode` (CRT profile: 768x576 4:3, H.264, AAC, deinterlaced if needed). It also carries
the **wanted list** (missing episodes found by gap detection, films, adverts, music videos
added in the admin UI) with destination folders under `<cache>/acquired/`, duration ranges,
an exact search phrase in `hints[0]`, year tolerance, plus `free_bytes`, `pi`, the running
marker and reports directory paths. The report gives per-item and per-wanted status;
"bot check" and "rate limit" failures are retried without using up an attempt; reports may
also be dropped as files in `<cache>/reports/` and are picked up by maintenance.

**Shared-drive rules.** pitv_content writes `.part` files and renames atomically, never
deletes, and touches `<cache>/.pitv_content.running` while working; PiTV ignores `.part`
files, never evicts files under two hours old or in the current manifest, and does not evict
while the marker is fresh.

**Nightly workflow** (Pi local time): ~04:00 PiTV scans the NAS and the acquired folders and
tops up the 7-day schedule; 01:00 pitv_content main run; 05:00 catch-up run; 06:00 and 07:00
PiTV **readiness** checks verify every programme through tomorrow is playable (cache,
transcoded copy or NAS original). A programme whose file is missing while its share is
mounted is replaced and the rest of that channel-day rebalanced, logged as an ERROR; if a
whole share is down nothing is substituted and the player shows the test card at air time.
The same substitution happens live if a file fails when it comes on air.

**Status in the admin UI.** pitv_content has no interface of its own, so the admin Content
tab reads its status file, service and timer state, log and reports, and can start a run.

## 8. Boot and system setup

- Raspberry Pi OS Lite 64-bit. Static IP on Ethernet (no DHCP wait). NTP via
  `systemd-timesyncd` pointed at the NAS or router.
- `config.txt`: `boot_delay=0`, `disable_splash=1`, `dtoverlay=disable-bt`,
  `dtoverlay=gpio-ir,gpio_pin=18`, `hdmi_drive=2`, `dtoverlay=vc4-kms-v3d`,
  `gpu_mem` left at default (KMS does not use it).
- Disable: bluetooth, hciuart, avahi, triggerhappy, ModemManager, apt timers,
  rpi-eeprom-update, man-db, dphys-swapfile.
- Services: `pitv-splash` (test card within seconds), `pitv-player` (after CIFS
  automounts and time sync), `pitv-web` (starts in parallel, does not delay the picture).
  The nightly scan, schedule top-up, readiness checks and cache eviction run from a
  maintenance thread inside the player (`pitv/player/maintenance.py`), so there are no
  timers. Expected: ~10–12 s to test card, ~15 s to programme.
- Data lives in `/var/lib/pitv/pitv.db`; logs to journald with a size cap.
- Optional later: read-only root overlay so pulling the plug never corrupts the SD card
  (the database moves to a small writable partition).

---

## 8.0 Installing: the SD-card installer

The image is DietPi (smaller and faster to boot than Pi OS Lite) with both apps' sources
baked in; `installer/` holds the image build, the first-boot provisioning script and a Go
installer for Linux and Windows. The card is split into a fixed-size system partition and a
work partition (logs, databases, state) so nothing that grows can fill the system. Modes:
clean (card and USB drive wiped), normal (card only), upgrade (new code over SSH, everything
else kept). First boot sizes partitions, prepares the USB drive without touching existing
content, creates the maintenance SSH user, configures Wi-Fi if asked, installs and primes both
apps and writes `/work/install/install.log`, which the admin shows under Logs. See
`installer/README.md`.

## 8.1 Resilience: 24/7 operation and recovery

- **Watchdogs.** Both services are `Type=notify` with `WatchdogSec`: the player pings systemd
  from its main loop every half second, the web service from an async heartbeat; a hung
  process is killed and restarted (`Restart=always`, no start-rate limit). The Pi's hardware
  watchdog is enabled (`dtparam=watchdog=on`, `RuntimeWatchdogSec=15`), so a kernel or
  systemd hang reboots the board. pitv_content's timer job has a 6 h `TimeoutStartSec`; its
  API service is expected to carry the same watchdog pattern.
- **Memory.** Units carry `MemoryHigh`/`MemoryMax` (player 900 M/1.2 G, web 400 M/600 M) so a
  leak is killed and restarted rather than taking the Pi down; the player also logs its own
  and mpv's RSS every five minutes and exits for a clean restart above `memory_limit_mb`
  (playback resumes at the live position within seconds). The web process does the same
  above 400 MB. History, old schedule days and run logs are pruned; logs rotate.
- **Power loss.** The schedule lives in SQLite (WAL, `synchronous=NORMAL`, safe against
  corruption) and every state file is written atomically, so nothing needs replaying. The
  player derives its position from the wall clock, so after a two-hour outage it tunes to
  what is on *now*. Because the Pi has no RTC, at boot it shows the test card and waits up to
  `clock_wait_seconds` (120 s) for NTP before tuning, and if the clock steps by more than a
  minute at any later time it re-tunes live at once. pitv_content resumes where it left off:
  it skips targets that already exist, cleans stale `.part` files, and its catch-up run and
  PiTV's readiness checks cover anything a reboot interrupted.
- **Storage.** The cache drive is mounted `nofail` so a missing or failing disk never blocks
  boot; the NAS shares are automounts that retry; the player falls back to the NAS original
  or the test card and logs an error.

## 9. Code layout

```
PiTV/
├── pitv/                      Python package (3.11+): FastAPI, uvicorn, sqlite3, evdev, Pillow
│   ├── config.py              bootstrap settings only (paths, sockets, web port); everything else lives in the DB
│   ├── db.py                  SQLite schema, migrations, default settings/channels, helpers
│   ├── guide.py               "what's on" lookups shared by the OSD guide and the web API
│   ├── content.py             the pitv_content contract: manifest, reports (§7)
│   ├── readiness.py           are tomorrow's files playable; substitute and rebalance
│   ├── wanted.py              the wanted list (gap detection)
│   ├── library/               scanner.py, naming.py (regexes), nfo.py, probe.py (ffprobe cache: duration, codec, interlace)
│   ├── scheduler/             build.py (week builder, anchors, gap fill, overnight, rebuild), rules.py (era/cert/daypart), listing.py
│   ├── player/                controller.py, mpv_ipc.py, hwdec.py, input.py, control_socket.py, osd.py, cache.py, maintenance.py
│   ├── web/                   app.py (FastAPI), api/ (public, admin, wanted, content), events.py (SSE), auth.py, tasks.py, static/ (built frontend)
│   ├── logsetup.py, sdnotify.py, splash.py, devtools.py (fake library)
│   └── cli.py                 `pitv scan | schedule | listing | play | web | readiness | content-manifest | ...`
├── web/                       Svelte + Vite source; `npm run build` writes to pitv/web/static/
├── assets/                    testcard.png (generated on first run when absent)
├── systemd/                   pitv-player.service, pitv-web.service, pitv-splash.service, mount unit templates
├── setup/                     install.sh (Pi provisioning), boot-trim.sh, dev.sh (desktop helper)
├── tests/                     unit tests; a fake-library generator makes tiny ffmpeg clips so everything runs on a desktop
└── docs/                      PLAN.md (this file), REQUIREMENTS.md (checklist)
```

Development happens on the desktop (mpv, ffprobe and Python 3.12 are already here) with a
generated fake library and a `--now` clock override so a week can be built and inspected
without the NAS. `pitv listing` prints a Radio Times style listing, and the web UI runs
locally against the same database.

Database tables: `settings`, `sources`, `channels`, `media` (episodes, films, adverts,
idents and music videos; overrides in a JSON column), `shows`, `show_cursor`, `schedule`
(locks are a column), `history`, `probe_cache`, `wanted`, `run_log`.

---

## 10. Build phases

| Phase | Deliverable | Checkpoint |
|---|---|---|
| 0 | Scaffold, DB schema and migrations, bootstrap config, fake-library generator, test harness | `pytest` green on desktop |
| 1 | Scanner: naming parsers, NFO, ffprobe cache (duration, codec, interlace), overrides | `pitv scan` on the fake library and on the real NAS |
| 2 | Scheduler: channels-as-data, patterns, anchors, gap fill, ads, watershed, overnight, history, manual edits | `pitv listing` shows a believable week with no rule violations |
| 3 | Web: FastAPI API + SSE, Svelte app: Now & Next, guide, admin for sources/library/channels/weighting/schedule | Runs on desktop against the fake library |
| 4 | Player core: mpv IPC, per-file hwdec selection, live-offset channel switching, keyboard input, channel badge, control socket, web remote | Runs on desktop in a window |
| 5 | On-screen guide overlay with teletext rendering and navigation | Desktop |
| 6 | IR remote via ir-keytable/evdev, volume, mute, pause | On the Pi |
| 7 | Pi provisioning: install script, systemd units, CIFS automounts, boot trimming, test card splash, hwdec verification (drm-prime vs v4l2m2m) | Cold boot to picture ≈ 15 s; 1080p H.264 plays with low CPU |
| 8 | Polish: static on channel change, idents, mid-programme ad breaks, Ceefax web theme, read-only root | Optional |

---

## 11. Open questions (assumptions used until answered)

1. **NAS shares**: confirmed as `smb://synologynas/tvshows/` and `smb://synologynas/movies/`.
   Adverts live in `smb://synologynas/ads/`. Still needed: an SMB account for the Pi (read-only
   is enough) and ideally a sample directory listing so the name parsers can be checked.
2. **Pause semantics**: assumed "pause holds, play resumes where you paused, changing
   channel rejoins live". The alternative is real-TV behaviour where resume jumps to live.
   "Restart" is read as play/resume; a separate "restart programme from the beginning" key
   is easy to add if wanted.
3. **Overnight**: assumed replay of the day from 08:00, per-channel setting.
4. **Ratings and genres**: are the shares scraped with NFO files? If not, the admin
   overrides are the only source of certificates, and unknown movies default to
   post-watershed.
5. **Remote**: OSMC RF remote (USB dongle, appears as a keyboard). Resolved; see §5.3.
6. **Channel names** for the on-screen guide and badges (editable in admin anyway).
7. **Mid-programme ad breaks** on ad channels: v1 places adverts only between programmes;
   the data model already allows a split at the halfway point later.
8. **Era weights**: assumed 85% 1980s / 15% 1990s, editable.
9. **Web exposure**: assumed LAN only; the admin password is the only protection.
