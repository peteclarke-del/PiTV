# PiTV — Design Plan

A Raspberry Pi 4 that behaves like a 1980s UK television: four continuously "broadcasting"
channels, a weekly schedule built from the NAS library, an infra-red remote, and a
teletext-style programme guide. Nothing here is code yet; this document is the design to
agree before implementation starts.

---

## 1. Goals and constraints

| Requirement | Decision |
|---|---|
| Hardware | Raspberry Pi 4, 4 GB, HDMI to a TV, IR receiver on GPIO (or HDMI-CEC), wired Ethernet to the Synology NAS (SMB) |
| OS | Raspberry Pi OS Lite 64-bit (Bookworm), no desktop, boots straight into the player |
| Boot | Target ~15 s from power to picture; test card shown while the NAS mounts |
| Channels | 1 and 2: programmes only. 3 and 4: programme, 2 adverts, programme, 2 adverts ... |
| Broadcast day | 08:00–00:00 scheduled; 00:00–08:00 replays that day's schedule from 08:00 (see §4.6) |
| Era | 1980s core, occasional 1990s; per-item year from folder/file names, NFO, or overrides |
| Watershed | 21:00. UK certificate rules: U/PG any time, 12 not before 20:00, 15 not before 21:00, 18 not before 22:00 |
| Horizon | 7 days generated at a time; episodes advance in order per show; minimal repeats week to week |
| Remote | Channel 1–4 direct, channel +/−, volume +/−, mute, pause/play, guide (menu), up/down/left/right/OK/back |
| Overhead | One mpv process, one small Python daemon, SQLite. No X, no Kodi, no web stack at runtime |

---

## 2. How "broadcast" works (the core idea)

Each channel has a schedule: an ordered list of *slots* `(channel, start, end, media file,
offset into file, kind)` stored in SQLite. Channels are virtual. Only the channel you are
watching is actually decoded. At any instant the player computes, from the wall clock,
which slot each channel is in and how far through it is. Changing channel means:
load that channel's current file in mpv and seek to `now − slot.start + slot.offset`.

Consequences:

- All four channels appear to run all the time, even while the Pi is off. Turn it on at
  19:42 and channel 1 is 12 minutes into whatever started at 19:30.
- A programme split into parts (for a mid-programme ad break) is just two slots pointing
  at the same file with different offsets. The model supports that from day one even if
  v1 only puts adverts between programmes.
- The guide is read straight from the schedule table, so it is always "real time".
- The Pi has no real-time clock, so NTP must succeed on boot for the clock to be right.
  If the network is late the player still runs; it just realigns when time syncs.

---

## 3. Content library

### 3.1 Sources (all on the Synology NAS, over SMB)

| Share | Expected layout | Notes |
|---|---|---|
| `smb://synologynas/tvshows/` | `Show Name (1984)/Season 02/Show Name - S02E05 - Title.mkv` | Season/episode from `SxxEyy` (also `2x05`, `Season 2/05 - Title`). Show year from folder |
| `smb://synologynas/movies/` | `Title (1985)/Title (1985).mkv` | Year from folder, then file name |
| `smb://synologynas/pitv/` (new, or a folder inside an existing share) | `Adverts/1984/Product.mp4`, `Idents/ch1/*.mp4`, `Static/static.mp4`, `TestCard/testcard.png` | Advert year from sub-folder or a leading `1984 - ` in the file name |

Both shares require authentication (guest access is denied), so the Pi mounts them with
CIFS using a root-only credentials file at `/etc/pitv/smb-credentials`, via systemd
automount units so boot never blocks on the network. Recommended mount options:
`vers=3.0,ro,noserverino,cache=loose,actimeo=60,_netdev,x-systemd.automount,x-systemd.idle-timeout=0`.
Read-only mounts protect the library; the adverts share is read-only too since the Pi
never writes there. NFS would be marginally lighter on the Pi and can be swapped in by
changing the mount units only.

### 3.2 Scanner (`pitv scan`)

- Walks the shares, parses show/season/episode/year from names.
- Reads Kodi-style `tvshow.nfo` / `<movie>.nfo` when present for premiered year,
  certificate (`mpaa`), genres, and plot.
- Probes duration with `ffprobe` once per file; cached in SQLite keyed on path, size,
  mtime so re-scans are fast.
- Applies `overrides.yaml` last: per-show or per-movie year, certificate, genre,
  daypart preference, exclude flag, "strip" vs "weekly" scheduling hint.
- Runs nightly (systemd timer) and on demand. New content is only used for future days.

### 3.3 Classification rules

| Attribute | Source order | Default when unknown |
|---|---|---|
| Year | folder → file name → NFO → overrides | excluded (not date-appropriate) |
| Certificate | NFO → overrides → filename tag `[15]` | Movies: treat as 15 (post-watershed). TV: treat as PG |
| Genre / audience | NFO genres → overrides | "general" |
| Kids content | genre Animation/Children/Family or override | no |

Era eligibility is configurable; proposed default:

```yaml
eras:
  - years: [1980, 1989]   # weight 0.85
  - years: [1990, 1999]   # weight 0.15
```

A show counts as 80s if it premiered in the window; a per-show override handles long
runners (e.g. a show that started in 1978 and ran to 1986).

---

## 4. Scheduler (`pitv schedule`)

Builds 7 days × 4 channels into the `schedule` table. Never rewrites slots in the past or
the one currently airing. Runs when the remaining horizon drops below 2 days (weekly in
practice), and can be re-run manually.

### 4.1 Channel personalities

| Ch | Style | Adverts | Content lean |
|---|---|---|---|
| 1 | Mainstream (BBC1-ish) | none, optional idents between programmes | Drama, sitcom, light entertainment, afternoon films |
| 2 | Alternative (BBC2-ish) | none | Documentaries, cult, older films, comedy |
| 3 | Commercial (ITV-ish) | 2 between programmes | Soaps, quiz, action drama, kids' teatime |
| 4 | Commercial (C4-ish) | 2 between programmes | Alternative comedy, imports, films, late night |

Leans are soft weights, not hard rules; with a small library they relax automatically.
Channel names are configurable (the "PiTV" branding is the user's choice).

### 4.2 Dayparts (weekday defaults, all configurable)

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

### 4.3 Anchors: strips and weekly slots

Real 80s scheduling is about regularity, and it also gives episode order for free.

- **Strip**: a show airs every weekday at the same time on its home channel
  (soaps, kids shows, daytime sitcoms). One episode per day, in order.
- **Weekly**: a show airs once a week, same day and time (prime-time drama, sitcoms).
- Each show is assigned a *home channel* so the same series never appears on two channels.
- A persistent per-show cursor (`show_cursor` table) records the next episode; it advances
  every time an episode is placed. When a series ends it rests for a configurable number
  of weeks, then restarts from S01E01.

The scheduler first pins anchors into the week, then fills the remaining gaps.

### 4.4 Gap filling

For each channel and day, walk from 08:00 to 00:00:

1. If an anchor starts here, place it.
2. Otherwise pick a candidate for the gap up to the next anchor: eligible era, certificate
   OK for the start time, daypart weight, duration fits (with a tolerance so a 52-minute
   drama can overrun a 50-minute gap and push the next unanchored item), not the same
   show already today on this channel, not the same genre as the previous item.
3. Score candidates by days-since-last-aired (from `history`), variety, and daypart fit;
   pick with weighted randomness seeded per week so re-runs are reproducible.
4. On channels 3 and 4 insert two adverts after every programme, choosing adverts whose
   year is within ±3 of the programme's year when possible.
5. Small leftover gaps (< shortest eligible item) are filled with idents/adverts or simply
   absorbed: 80s schedules happily started programmes at 19:35.

Start times are rounded to 5 minutes where padding allows, because "19:35" reads right in
a listing and "19:37" does not.

### 4.5 Repeat control

- Movies: never twice in one week; across weeks, weighted by time since last airing.
- Episodes: only ever advance; repeats only happen when a series wraps.
- The `history` table logs every slot that actually aired (written by the player) so the
  scheduler knows what was really shown, not just what was planned.

### 4.6 Overnight

From 00:00 to 08:00 each channel replays that day's schedule starting from the 08:00 slot,
cut off at 08:00 when the new day begins. This is what "once we get to midnight, the shows
repeat until 8am" is taken to mean. Alternative (replay the evening from 19:00 instead) is a
one-line config change.

---

## 5. Player daemon (`pitv-player`, the systemd service)

### 5.1 Structure

```
controller (state machine, 1 Hz tick)
 ├─ clock → schedule lookup → "what should be on channel N right now"
 ├─ mpv via JSON IPC socket (loadfile, seek, pause, volume, mute, overlays)
 ├─ input: evdev (IR remote via kernel gpio-ir + ir-keytable; keyboard for dev; CEC optional)
 ├─ OSD: channel badge, volume bar, guide (Pillow → BGRA → mpv overlay-add)
 └─ history writer (logs what aired)
```

- One long-lived mpv process, started once with `--vo=gpu --gpu-context=drm` (no X),
  `--hwdec=auto-safe` (Pi 4 hardware decodes H.264 and HEVC; the mostly SD 80s content
  decodes fine in software anyway), `--input-ipc-server=/run/pitv/mpv.sock`, OSD enabled,
  audio via ALSA to HDMI.
- Channel change = `loadfile <path> replace start=<offset>`; a short local "static"
  clip (or a 300 ms burst overlay) covers the seek latency and looks the part.
- Slot boundaries: on each tick, if the current slot has ended, load the next slot. If
  mpv reaches end-of-file early (bad duration), the tick loads whatever is now due.
- Drift check: if the schedule position and mpv position differ by more than a few
  seconds while not paused, re-seek.

### 5.2 Remote control mapping

Kernel IR (`dtoverlay=gpio-ir,gpio_pin=18` + `ir-keytable`) turns any IR remote into a
normal Linux input device, so no LIRC daemon is needed and the same code path handles a
keyboard or CEC. If a classic `lircd` setup is preferred, the input module is the only
piece that changes.

| Key | Action |
|---|---|
| 1–4 | Select channel |
| CH+ / CH− | Next / previous channel (wraps 4→1) |
| VOL+ / VOL− | mpv volume ±5, on-screen bar |
| MUTE | Toggle mute |
| PLAY/PAUSE | Pause holds the frame; play resumes from the paused point (that channel runs behind live until you change channel, at which point you rejoin live). See open question 2 |
| MENU / GUIDE | Toggle the programme guide |
| UP / DOWN | Guide: move channel highlight |
| LEFT / RIGHT | Guide: previous / next programme on the highlighted channel. Left stops at what's on now |
| OK | Guide: switch to highlighted channel and close guide |
| BACK / EXIT | Close guide |
| INFO | Show the channel badge (what's on, start–end, what's next) |

### 5.3 On-screen guide

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

## 6. Boot and system setup

- Raspberry Pi OS Lite 64-bit. Static IP on Ethernet (no DHCP wait). NTP via
  `systemd-timesyncd` pointed at the NAS or router.
- `config.txt`: `boot_delay=0`, `disable_splash=1`, `dtoverlay=disable-bt`,
  `dtoverlay=gpio-ir,gpio_pin=18`, `hdmi_drive=2`, KMS driver on.
- Disable: bluetooth, hciuart, avahi, triggerhappy, ModemManager, apt timers,
  rpi-eeprom-update, man-db, dphys-swapfile.
- `pitv-splash.service` shows the test card on the DRM console as soon as the kernel is
  up (a few seconds in), then `pitv-player.service` takes over once the CIFS automounts
  (`/mnt/tvshows`, `/mnt/movies`, `/mnt/pitv`) and time sync are ready. Expected: ~10–12 s to test card, ~15 s to programme.
- Data lives in `/var/lib/pitv/pitv.db`; logs to journald with a size cap.
- Optional later: read-only root overlay so pulling the plug never corrupts the SD card.

---

## 7. Code layout

```
PiTV/
├── pitv/                      Python package (3.11+, stdlib + python-mpv-jsonipc-free IPC, evdev, Pillow, PyYAML)
│   ├── config.py              load/validate config.yaml + overrides.yaml
│   ├── db.py                  SQLite schema and helpers
│   ├── library/               scanner.py, naming.py (regexes), nfo.py, probe.py (ffprobe cache)
│   ├── scheduler/             build.py (week builder), anchors.py, fill.py, rules.py (era/cert/daypart), overnight.py
│   ├── player/                controller.py, mpv_ipc.py, input_evdev.py, osd/ (badge.py, guide.py, teletext.py)
│   └── cli.py                 `pitv scan | schedule | listing | play | simulate`
├── assets/                    testcard.png, static.mp4, fonts/ (a free teletext-style bitmap font)
├── config/                    config.example.yaml, overrides.example.yaml, keymap.example.toml
├── systemd/                   pitv-player.service, pitv-splash.service, pitv-scan.timer, pitv-schedule.timer, mount units
├── setup/                     install.sh (Pi provisioning), boot-trim.sh, ir-keytable setup
├── tests/                     unit tests; a fake-library generator makes tiny ffmpeg clips so everything runs on a desktop
└── docs/PLAN.md               this file
```

Development happens on the desktop (mpv, ffprobe and Python 3.12 are already here) with a
generated fake library and a `--now` clock override so a week can be built and inspected
without the NAS. `pitv listing` prints a Radio Times style listing for checking the
scheduler's output before it ever touches the Pi.

Database tables: `media`, `shows`, `episodes`, `show_cursor`, `schedule`, `history`,
`probe_cache`.

---

## 8. Build phases

| Phase | Deliverable | Checkpoint |
|---|---|---|
| 0 | Scaffold, config loading, DB schema, fake-library generator, test harness | `pytest` green on desktop |
| 1 | Scanner: naming parsers, NFO, ffprobe cache, overrides | `pitv scan` on the fake library and on the real NAS |
| 2 | Scheduler: anchors, gap fill, ads, watershed, overnight, history | `pitv listing` shows a believable week with no rule violations |
| 3 | Player core: mpv IPC, live-offset channel switching, keyboard input, channel badge | Runs on desktop in a window |
| 4 | Guide overlay with teletext rendering and navigation | Desktop |
| 5 | IR remote via ir-keytable/evdev, volume, mute, pause | On the Pi |
| 6 | Pi provisioning: install script, systemd units, CIFS automounts, boot trimming, test card splash | Cold boot to picture ≈ 15 s |
| 7 | Polish: static on channel change, idents, mid-programme ad breaks, read-only root | Optional |

---

## 9. Open questions (assumptions used until answered)

1. **NAS shares**: confirmed as `smb://synologynas/tvshows/` and `smb://synologynas/movies/`.
   Still needed: an SMB account for the Pi (read-only is enough), where the adverts folder
   will live (assumed a new `pitv` share), and ideally a sample directory listing of each
   share so the name parsers can be checked against the real convention.
2. **Pause semantics**: assumed "pause holds, play resumes where you paused, changing
   channel rejoins live". The alternative is real-TV behaviour where resume jumps to live.
   "Restart" is read as play/resume; a separate "restart programme from the beginning" key
   is easy to add if wanted.
3. **Overnight**: assumed replay of the day from 08:00. Alternative: replay from 19:00.
4. **Ratings and genres**: are the shares scraped with NFO files? If not, the overrides
   file is the only source of certificates, and unknown movies default to post-watershed.
5. **Remote**: which IR remote and receiver? Assumed a TSOP-type receiver on GPIO 18 with
   the kernel IR driver. HDMI-CEC (using the TV's own remote) can be added as a second input.
6. **Channel names** for the on-screen guide and badges.
7. **Mid-programme ad breaks** on channels 3 and 4: v1 places adverts only between
   programmes; the data model already allows a split at the halfway point later.
8. **Era weights**: assumed 85% 1980s / 15% 1990s.
