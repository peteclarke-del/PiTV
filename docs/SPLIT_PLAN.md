# A split system: stations, content workers and receivers

A plan, not yet a design to build from. Pete asked on 20 September 2026 whether PiTV could be
split so that one or more content back ends run on their own machines and the televisions are
driven by small boards (a Pi Zero 2 W or similar) that only connect to a stream for each
channel, and said it should be planned before any code changes. This page says what that would
look like, what already exists, what is hard, and the order to find out in. Nothing here is
built, and the single-box PiTV stays the supported shape throughout.

## Decided

Pete's decision, 20 September 2026: PiTV is to be built so that it runs all in one and also
with a remote front end. Both are supported shapes of the same software, not a fork: the
all-in-one install is the three roles below on one machine, and it keeps passing the same
tests. From here on, nothing in the front end may assume a local database or local media, and
nothing the front end needs may exist only in-process on the station.

## The short answer

Feasible, and closer than it looks, because the hard half exists. PiTV already serves every
channel over HTTP (PLAN 6.5): `/channel/<n>.m3u8` is a live HLS stream of what the schedule
says is on, from the same file and offset the television plays, about fifteen seconds behind,
with cache copies repackaged rather than re-encoded so that more viewers cost almost nothing.
A receiver is therefore "mpv pointed at that address, with a remote control". The work is in
everything around the picture: the on-screen badge and guide, the remote, the channel change,
what the screen shows when the network or the station is away, and keeping every stream
decodable by the smallest board.

The part that needs real design is "one or more content back ends". One pitv_content on its
own machine is a configuration change. Several sharing the work is a new protocol.

## Three roles

| Role | What it is | Runs today as |
|---|---|---|
| Station | The schedule, the catalogue, the cache, the admin, the guide, and the origin of every channel's stream. One per household. Headless: no television attached | PiTV's web service and scheduler, on the Pi 4 with the television |
| Content worker | Finds, fetches, copies and encodes material into the station's cache, and indexes the library. One or more | pitv_content, on the same machine |
| Receiver | A television's tuner: plays one channel's stream, changes channel, draws the badge and the guide, reads the remote. Holds no schedule, no database and no media | PiTV's player, on the same machine, reading the station's database directly |

The single-box install is all three on one Pi 4. The split is the same software with the roles
on different machines, which is the constraint that keeps this honest: no role may need
anything the others do not already offer over the network.

## What exists, and what each role still needs

### The station

Exists: everything. It already runs without a player attached (the development machine does,
most of the day).

Needs:

1. Streams that are always up. Today a stream starts on the first request and stops after
   `stream_idle_seconds`, so the first viewer of a channel waits for ffmpeg to start. For
   receivers, every enabled channel's stream should run continuously (a setting), so a channel
   change is a playlist fetch. In copy mode seven streams cost little; the cost is wherever a
   re-encode is needed (item 2).
2. A guarantee about what is in the stream. A Pi Zero 2 W decodes H.264 in hardware up to
   1080p and nothing else: no HEVC. The cache already holds what the Pi 4 can play, which
   includes HEVC up to 2160p. For a receiver profile the station must serve H.264 at the
   screen's size at most, which means either the cache is built to that profile (pitv_content
   encodes HEVC and oversize sources once, on a machine that can afford it, which is the
   better answer and is what content workers are for) or the stream re-encodes live (a Pi 4
   cannot do that for seven channels). This is the single biggest sizing question.
3. The static and the idents travel in the stream already (idents are programmes). The channel
   change static is drawn by the receiver.
4. An API for receivers: what is on now and next per channel, the guide grid, the channel
   list with names, colours and numbers, and the settings a receiver needs (badge seconds, OSD
   scale). Most of this exists for the web guide (`/api/now`, `/api/schedule`,
   `/api/schedule/day/<day>`, and `/api/events` for changes); it needs to be treated as a stable
   interface and versioned.
5. Receiver registry in the admin: which receivers exist, what each is showing, last seen.
   The stream log already records viewers by address.

### The receiver

Exists: mpv, the OSD renderer (badge, guide, volume, static), the remote handling, the
keeper that restarts mpv. All of it currently assumes a local SQLite database and local files.

Needs:

1. A data source that is the station's API instead of the database: `slot_at`, `next_programmes`
   and the guide query become HTTP calls with a short cache. This is the main refactor, and it
   is worth doing even for the single box: the player would stop sharing a database with the
   scheduler.
2. Play = open `http://<station>/channel/<n>.m3u8`. Channel change = static overlay, load the
   other playlist, badge. Drift correction, cache lookups, NAS fallback and readiness all
   disappear from the receiver; they are the station's.
3. A holding picture of its own for "no station" and "no network", since it has no media.
4. Configuration: the station's address, the screen profile, the remote's keymap. Nothing else.
   Ideally discovered (mDNS: the station advertises `_pitv._tcp`) so that a receiver is an SD
   card image with no typing.
5. Time: NTP only. The stream carries the schedule's timing; the receiver's clock is only for
   the on-screen clock and the guide's "now" line.

Hardware notes to verify rather than assume: a Zero 2 W has 512 MB, 2.4 GHz Wi-Fi only (a
576-line H.264 stream at 2 to 4 Mbit/s is comfortable, 1080p at 8 to 12 is not reliably), and
composite video out on test pads, which is what a real CRT wants. A Pi 3A+ or a Pi 4 as a
receiver needs no different software.

### Content workers

This section was corrected by pitv_content's session on 20 September, from its code.

One worker on another machine: possible, with three things to settle.

- The cache is reached over a mount (NFS or SMB from the station, or the cache lives on the
  worker's disk and the station mounts it). Anything the worker indexes over a mount must be a
  `nas` source with the share as its `remote`, never `local`. A `nas` root that is not mounted
  is called unreachable and the index stays incomplete, so PiTV retires nothing; a `local`
  folder has no such check by design, so a mount that failed and left an empty directory would
  be read as "readable and empty", a complete index would be published, and PiTV would be told
  that everything there had gone. `local` is for folders on the worker's own disk, which is
  what idents are on a single box. On a split system the idents folder is either on the
  worker's disk or a `nas` source.
- Its API. `serve` refuses any bind that is not loopback, in code, not configuration. Reaching
  it from another machine is therefore one of two things: an SSH tunnel, which needs nothing
  from pitv_content at all and keeps the no-back-door rule as it stands, or authentication
  added to pitv_content with a decision about how secrets are held, which is real work. The
  tunnel is the default until there is a reason for the other.
- The screen profile. pitv_content already encodes what it fetches to the profile PiTV gives
  it, provided PiTV pushes the profile's values (`video_profile_values`, agreed in the review)
  and they reach catalogue runs as well as cache runs. The cache's copies of NAS originals are
  governed by PiTV's own copy rule (`pi_can_play`), which is what puts HEVC at 2160p in the
  cache today. Making the cache safe for a small receiver is PiTV's lever: a receiver profile
  whose copy rule admits H.264 up to the screen's size and sends everything else to be encoded
  once.

Several workers: closed today, and not only a matter of design.

1. A cache run holds an exclusive file lock on the manifest's running marker for its whole
   life, and a second run is refused: "another cache run is active". Delivery and encoding are
   the same run, so splitting workers by duty needs the run split first. And a file lock is
   the thing that travels worst over a mount: NFSv3 needs lockd, SMB semantics vary, and a lock
   that silently fails to be exclusive is the failure the marker exists to prevent, since two
   runs writing one cache is how half-written targets happen. With the cache on a mount the
   marker should stop being something workers arbitrate among themselves and become something
   the station hands out: a lease on the run.
2. Shared knowledge, not only shared files. The index, the memory of what was not found and
   the record of where each series was last found are each written whole and atomically, so
   two workers would not corrupt them; they would lose each other's knowledge, last writer
   winning. Each would search for the same missing episode because neither saw the other's
   rest, and each would learn a provider's refusal separately, since the backoff and the
   breaker live in the process.
3. make-room and the cap are already the station's, which is right.
4. Providers ration by address. Several workers behind one household connection do not get
   more searches; without shared state they find the same things twice and spend the ration
   faster. More workers help encoding and copying, not finding.

So the honest order for content is: one worker on another machine first (tunnel, mount, `nas`
sources, pushed profile values); then, only if encoding proves to be the limit, split the run
into delivery and encode stages with a station-held lease; and a second finder never.

## The seam: what a front end asks of a station

Measured in the code on 20 September. The player (`pitv/player/controller.py`, a thousand lines)
touches the database in about twenty places, and they reduce to a short list. That list is the
interface, `Station`, with two implementations: `LocalStation` over the database and files,
which is today's behaviour exactly, and `RemoteStation` over the station's HTTP API and streams.

| The front end asks | Local answer | Remote answer |
|---|---|---|
| Settings it needs (badge seconds, OSD scale, static on or off, timezone, day start) | `all_settings`, `tz_of` | `GET /api/receiver/config`, refreshed on the station's change event |
| The channels, in order, with number, name, colour | `enabled_channels` | the same call |
| What is on a channel at a moment, and its place in a band | `slot_at`, `block_entry` | `GET /api/now` |
| What comes next | `next_programmes` | `GET /api/now` and `/api/schedule` |
| The guide grid for a window | the guide query | `GET /api/schedule/day/<day>` |
| What to play for a channel | the slot's cache copy, else the NAS original, else the card, at an offset | the channel's stream address; the station has already done all of that |
| "This was watched" | a row in `history` | `POST /api/receiver/watched`, best effort |
| "The schedule changed" | the control socket | `GET /api/events` |

Two things move out of the player for good, in both shapes, because they are the station's
work and only live in the player process by history: the maintenance pass (importing the index,
applying delivery reports, asking for band material, eviction, readiness; `player/maintenance.py`)
and the write side of the database. A station with no television attached must still do them,
and a receiver must never.

Build order for the seam, each step shippable on the single box:

1. `Station` and `LocalStation`, with the controller's database calls routed through them. No
   behaviour changes; the suite is the proof. Done (`pitv/player/station.py`): the controller
   no longer opens the database, and a test fails if a query is added to it.
2. The maintenance pass moves from the player to the web service (the station), which already
   runs background jobs. The player keeps only what concerns the screen.
3. The receiver endpoints on the station, versioned, and `RemoteStation` against them.
4. `pitv receiver --station <address>`: the player started with `RemoteStation`, playing
   streams. The same OSD, remote handling and static.
5. Always-on streams on the station, and the transport decided by the measurements below.

## What is genuinely hard

- Channel change speed. HLS starts in two to six seconds however it is tuned; the static
  covers some of it, and it is what the single box does in under a second. Options to measure:
  short segments (two seconds), always-on streams, the receiver pre-opening the neighbouring
  channels, or a lower-latency transport (MPEG-TS over HTTP or multicast UDP on the LAN, which
  mpv also plays and which changes channel like a real tuner). This decides whether the split
  feels like television, so it is measured first.
- One format for all. The smallest receiver sets the cache profile for everyone unless the
  station keeps two renditions.
- The player refactor. It is the most code, and it touches the part that must never stop.
- Failure behaviour. A single box fails as a whole; a split system can half-fail (station up,
  Wi-Fi poor), and each half-failure needs a picture on the screen that says what is true.

## The order to find out in

Each step is small, and the first two change no code.

0. Try it. Put mpv on a Pi Zero 2 W (or any spare Pi), point it at the development station's
   `/channel/1.m3u8` and at two other channels, and measure: does it play smoothly over the
   house Wi-Fi, how long does a channel change take, what does CPU and memory look like, does
   composite out look right on the CRT. An afternoon, and it answers the biggest questions.
1. Measure the station: seven always-on streams on a Pi 4 with today's cache. How many are
   copies and how many re-encodes, and what each costs. This sizes item 2 above.
2. Decide from 0 and 1: transport (HLS or TS), whether the cache profile must become
   H.264-only, and whether a Zero 2 W is enough or the receiver should be a 3A+.
3. Station: always-on streams and the receiver API, behind settings, single box unaffected.
4. Receiver: the player's data source becomes an interface with two implementations, the
   database (today) and the station's API. Ship the single box on the interface first.
5. Receiver image: mpv, the receiver agent, discovery, the holding pictures.
6. One content worker on another machine: an SSH tunnel to its API, the cache on a mount, `nas`
   sources for anything reached over one, the installer.
7. An encode worker, only if 6 shows encoding is the limit: the run split into stages, with a
   lease the station hands out in place of the file lock.

## What this does not change

The schedule stays the authority and the configuration stays in the admin. pitv_content stays
the only thing that goes online. The no-back-door rule stays: nothing new listens beyond the
LAN, and anything a worker's API needs from another machine is decided explicitly. The
single-box install remains the default and must keep passing the same tests.

## Decisions for Pete before step 3

- Is a few seconds of channel change acceptable, or must it feel like the single box? (Step 0
  says what the honest number is.)
- Is the whole cache becoming H.264 at screen size acceptable? It costs one encode per HEVC or
  oversize file, once, and saves cache space on a standard definition screen.
- How many televisions, and are any on Wi-Fi only?
- Should receivers be zero-configuration (discovery) from the start?
