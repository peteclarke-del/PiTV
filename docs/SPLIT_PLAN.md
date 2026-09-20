# A split system: stations, content workers and receivers

A plan, not yet a design to build from. Pete asked on 20 September 2026 whether PiTV could be
split so that one or more content back ends run on their own machines and the televisions are
driven by small boards (a Pi Zero 2 W or similar) that only connect to a stream for each
channel, and said it should be planned before any code changes. This page says what that would
look like, what already exists, what is hard, and the order to find out in. Nothing here is
built, and the single-box PiTV stays the supported shape throughout.

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
   scale). Most of this exists for the web guide (`/api/now`, `/api/guide`); it needs to be
   treated as a stable interface and versioned.
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

One worker on another machine: already possible in principle. pitv_content talks to PiTV over
HTTP (manifest, report, make-room) and to the cache over a filesystem. Moved to another host
it needs the cache directory mounted read-write (NFS or SMB from the station, or the cache
lives on the worker's disk and the station mounts it), its loopback-only API reached another
way (PiTV's admin proxies to it today and the no-back-door rule stands: the answer is an SSH
tunnel or a shared secret on a LAN-only listener, to be decided, not assumed), and `local`
sources such as idents reached over the same mount.

Several workers: new design, with these questions open.

1. Who decides who does what. Today one process sorts the manifest and works it. With several,
   either the station hands out work (a claim or lease per request: "worker B has w:1893 until
   19:40") or the workers are given disjoint duties (one does delivery, one does bands, one
   does encodes). Duties are far simpler and match how the load actually splits: encoding is
   CPU, fetching is network and patience, copying is disk.
2. One writer for the index and for the fingerprint store. Both are single-writer today.
3. make-room and the cap are already the station's, which is right.
4. Providers ration by address. Several workers behind one household connection do not get
   more searches; they get the same allowance spent faster. More workers help encoding and
   copying, not finding.

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
6. One content worker on another machine: mounts, the API's reachability, the installer.
7. Several workers by duty, only if 6 shows a need.

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
