# PiTV and pitv_content: contract, schema 2

This document is the interface between the two applications. Both run on the same Pi and
share the cache drive. Changes to it are agreed by both projects before either side ships.

## Responsibilities

| | PiTV | pitv_content |
|---|---|---|
| Owns | the programme catalogue, channel line-ups, the schedule, playback, the web app and admin, cache eviction, readiness | the sources (NAS shares and online providers), the NAS index, finding, fetching, trimming and encoding, placing files in the cache |
| Reads | the library index, delivery reports, the cache, the NAS (read-only, fallback playback only) | the request manifest, the NAS (read-only), online providers |
| Writes | its database and JSON mirrors (`catalogue.json`, `lineups.json`); deletes cache files by LRU and transient expiry | the library index, files in the cache, delivery reports, its status file and log |

PiTV never indexes the NAS and never downloads or encodes. pitv_content never decides what is
scheduled and never deletes from the cache. The scheduled services never write to the NAS;
pitv_content's `--dest nas` catalogue option is a manual operator action outside this
contract, and anything it files reaches PiTV through the index like any other NAS file.

## Flow

1. pitv_content indexes its NAS sources and publishes the library index.
2. PiTV imports the index into its catalogue. Custom programming (titles not on the NAS)
   is added to the catalogue in PiTV's admin as line-up entries.
3. PiTV builds the schedule from each channel's line-up and publishes the request manifest:
   every programme, advert, ident and music video it needs through the end of the next
   broadcast day.
4. pitv_content works the manifest by priority and deadline: items with a NAS source are
   copied or transcoded into the cache; items without one are searched for online, fetched,
   encoded and cached.
5. pitv_content reports each delivery with the file's path and properties. PiTV records the
   cache path, corrects slot lengths where the real duration differs, and creates catalogue
   entries for material fetched online.
6. At air time PiTV plays the cache copy. If it is missing or unplayable PiTV logs an error
   and, when `nas_fallback` is on, plays the NAS original. If neither is playable it shows the
   technical difficulties card and logs the failure.

## Authentication

pitv_content has no admin login, so the three PiTV endpoints it calls
(`GET /api/content/manifest`, `POST /api/content/report`, `POST /api/content/make-room`) accept
a shared token as `Authorization: Bearer <token>`. PiTV generates it on first start and keeps it
in `<PiTV data dir>/content-token` (`/var/lib/pitv/content-token` on the Pi), mode 0600, owned
by the `pitv` user both services run as. pitv_content reads the file on each call, so a token
rotated from PiTV's admin (Content, Status) takes effect without a restart; on another machine
the admin copies the value across. The token opens only those three endpoints; an admin session
also works on them. Without either, once an admin password is set, PiTV answers 401. PiTV also
refuses cross-site browser requests that change state, which a script that sends neither
`Origin` nor `Sec-Fetch-Site` never trips.

pitv_content's own API is not authenticated: it listens on loopback, and PiTV's admin reaches it
through PiTV's proxy. If the two apps are ever split across machines, pitv_content's API needs a
token of its own before it is exposed on the LAN.

## 1. Library index (pitv_content to PiTV)

`GET {content_tool_url}/api/library`, and the same document written atomically to
`<cache_dir>/index/library.json` so PiTV can import it when the API is down.
`POST {content_tool_url}/api/index` starts a re-index and returns its `job_id`; the file's
`generated_ts` changes when it completes. PiTV waits for that job (`GET /api/jobs` until the
entry with that `job_id` has `finished_ts`) before it imports, so a re-index started from the
admin is imported as soon as it is done. A job that wrote an index ends `ok` even when the index
is incomplete (that is carried by `"complete": false`); `failed` means no index was produced,
and PiTV logs it and imports the index it already had.

```json
{
  "schema": 2,
  "generated_ts": 1789430400,
  "complete": true,
  "sources": [
    {"id": "tvshows", "name": "TV Shows", "type": "tv", "category": "general",
     "root": "/mnt/tvshows", "remote": "smb://synologynas/tvshows/", "enabled": true}
  ],
  "shows": [
    {"uid": "show:tvshows:Minder (1979)", "source": "tvshows", "title": "Minder", "year": 1979,
     "genres": ["Drama", "Comedy"], "certificate": "12", "plot": "...", "category": "general"}
  ],
  "items": [
    {"uid": "nas:tvshows:Minder (1979)/Season 02/Minder - S02E05 - The Beach.mkv",
     "source": "tvshows", "kind": "episode", "show_uid": "show:tvshows:Minder (1979)",
     "season": 2, "episode": 5, "title": "The Beach", "year": 1980,
     "genres": [], "certificate": null, "plot": null,
     "duration": 3120.4, "vcodec": "h264", "acodec": "aac", "width": 720, "height": 576,
     "interlaced": true, "size": 734003200, "mtime": 1700000000,
     "path": "/mnt/tvshows/Minder (1979)/Season 02/Minder - S02E05 - The Beach.mkv"}
  ]
}
```

- Every source carries `location`: `nas` for pitv_content's NAS shares, `cache` for the
  folders under `acquire_dir` holding material it has already fetched. Items from a `cache`
  source have uid `cache:<source id>:<relpath>`, their `path` is the cache file, and PiTV treats
  them as already cached.
- Source `type` (`tv`, `movie`, `advert`, `ident`, `music`) decides item kinds. `category`
  matters only for `tv` sources and is one of `general`, `sport` (weekend blocks and sport
  daypart weights) or `kids` (children's series).
- `kind` is `episode`, `movie`, `advert`, `ident` or `music`. Episodes carry `show_uid`,
  `season`, `episode`; adverts carry `family_safe` (bool) and `tags`; music carries `artist`
  and `concert`; idents carry `channel_hint`, the channel number the file was made for, which
  PiTV uses once to assign the ident to that channel.
- `uid` is stable for as long as the file keeps its path: `nas:<source id>:<relpath>` for
  items and `show:<source id>:<folder>` for series. `path` is the absolute path on the Pi's
  read-only mount, used only for fallback playback.
- A `complete` index lists every item; PiTV marks anything absent from it as missing. An
  incomplete index (`"complete": false`) only adds and updates.
- `sources` are pitv_content's NAS sources. PiTV shows them and edits them through
  pitv_content's API; it does not use them itself.

## 2. Request manifest (PiTV to pitv_content)

`GET /api/content/manifest?days=1` on PiTV, or `pitv content-manifest --out file` when the web
service is down. Schema 2.

```json
{
  "schema": 2,
  "generated_ts": 1789430400, "horizon_ts": 1789542000,
  "cache_dir": "/mnt/cache/pitv", "acquire_dir": "/mnt/cache/pitv/acquired",
  "free_bytes": 500000000000, "cache_max_bytes": 644245094400, "pi": true,
  "profile": {"name": "crt_pal", "label": "CRT (PAL)", "width": 768, "height": 576, "aspect": "4:3",
              "max_source_height": 1080, "vcodec": "h264", "acodec": "aac", "max_bitrate_kbps": 4000,
              "frame_rate": 25, "deinterlace": "if_interlaced"},
  "running_marker": "/mnt/cache/pitv/.pitv_content.running",
  "reports_dir": "/mnt/cache/pitv/reports",
  "items": [
    {"request_id": "m:1234", "media_id": 1234, "wanted_id": null, "uid": "nas:tvshows:...",
     "kind": "episode", "show_title": "Minder", "season": 2, "episode": 5,
     "title": "The Beach", "year": 1980, "artist": null, "duration": 3120.4,
     "channels": [3], "first_air_ts": 1789462800, "deadline_ts": 1789461900, "priority": 0,
     "source": {"path": "/mnt/tvshows/...", "vcodec": "h264", "height": 576, "interlaced": true},
     "action": "transcode", "target": "/mnt/cache/pitv/1234_Minder - S02E05 - The Beach.mp4",
     "already_cached": false, "transient": false},
    {"request_id": "w:77", "media_id": null, "wanted_id": 77, "uid": null,
     "kind": "episode", "show_title": "The Tripods", "season": 1, "episode": 3,
     "title": "Episode 3", "year": 1984, "duration": 1500,
     "channels": [2], "first_air_ts": 1789549200, "deadline_ts": 1789548300, "priority": 6,
     "source": null, "action": "fetch",
     "search": {"phrase": "The Tripods S01E03 1984 full episode", "hints": ["BBC"],
                "duration_minutes": [20, 60], "year_tolerance": 2},
     "dest_dir": "/mnt/cache/pitv/acquired/tvshows/The Tripods (1984)/Season 01",
     "transient": true}
  ],
  "wanted": []
}
```

- `profile` follows the screen chosen in PiTV's admin (`pitv/display.py`). Encode to
  `width`x`height` (square pixels, `aspect` the screen's shape), `vcodec` (H.264 up to 1080
  lines, HEVC above, which is what the Pi 4 decodes in hardware), at most `max_bitrate_kbps`,
  conformed to `frame_rate` when it is set (PAL 25, NTSC 29.97; null keeps the source rate).
  When fetching, look across every enabled provider and start from the best source available
  up to `max_source_height` lines: two rungs above the target on the 720, 1080, 1440, 2160
  ladder, and equal to it at 4K. Prefer the higher resolution, then the higher bitrate, among
  hits of the same title; never go above the ceiling, and take a lower source only when
  nothing better exists. A copy or transcode from the NAS uses the file there as it is.
  PiTV also sets pitv_content's own screen setting (`PUT /api/settings {"profile": <name>}`)
  whenever the admin changes the screen, so catalogue runs, which have no manifest, match.
- Every scheduled file appears once, however many slots or channels use it.
- `action` is `copy` (the source already decodes in hardware on the Pi and fits the profile),
  `transcode` (it does not), or `fetch` (there is no known source; find it online, encode it to
  the profile and file it under `dest_dir`).
- `target` is where a copy or transcode must end up. Fetched material is filed under
  `dest_dir` in the Kodi layout and its path is reported.
- `wanted` lists requests that are not scheduled yet (adverts or music videos added by hand,
  gaps in a series); same shape as a `fetch` item without air times.
- A `fetch` item for a title added in PiTV's admin carries `match`, the identity the admin
  confirmed from a lookup (section 8): `{"source": "tvmaze", "id": "1234", "url": "...",
  "imdb": "tt0086666"}`, with `url` and `imdb` when known. pitv_content fetches that title
  rather than whatever the search phrase finds first: for a series it may use the source's
  episode list for titles and running times, for a film the IMDb id to confirm a hit. Items
  without a confirmed identity have `"match": null`. A `ref` URL, when present, is a specific
  video to fetch and wins over both.

## 3. Delivery report (pitv_content to PiTV)

`POST /api/content/report` on PiTV; the same document may be dropped in `reports_dir`. PiTV
applies a report once: a dropped copy of one it already took over HTTP is recognised by
`run.tool` and `run.started_ts`, so both must be present and identical in the two copies.

```json
{
  "schema": 2,
  "items": [
    {"request_id": "m:1234", "media_id": 1234, "wanted_id": null, "status": "done", "message": "",
     "file": {"path": "/mnt/cache/pitv/1234_Minder - S02E05 - The Beach.mp4", "duration": 3120.0,
              "vcodec": "h264", "acodec": "aac", "width": 768, "height": 576, "interlaced": false,
              "size": 1234567890}},
    {"request_id": "w:77", "media_id": null, "wanted_id": 77, "status": "done", "message": "",
     "file": {"path": "/mnt/cache/pitv/acquired/tvshows/The Tripods (1984)/Season 01/The Tripods - S01E03 - Episode 3.mp4",
              "duration": 1712.0, "vcodec": "h264", "acodec": "aac", "width": 768, "height": 576,
              "interlaced": false, "size": 456789012},
     "meta": {"kind": "episode", "show_title": "The Tripods", "season": 1, "episode": 3,
              "title": "The Tripods", "year": 1984, "genres": ["Science Fiction"], "certificate": "PG",
              "plot": "...", "artist": null, "concert": false, "family_safe": true, "uid": "yt:abc123"}},
    {"request_id": "w:78", "media_id": null, "wanted_id": 78, "status": "failed",
     "message": "youtube bot check", "file": null}
  ],
  "run": {"started_ts": 1789430400, "finished_ts": 1789434000, "tool": "pitv-content 0.3.0",
          "log_tail": "..."}
}
```

- `status` is `done`, `failed` or `skipped` (already cached, or being written by another
  process). A failure whose message contains "bot check" or "rate limit" is temporary and
  does not use up an attempt; a request fails for good after three attempts.
- `file` properties are measured by pitv_content after encoding. PiTV trusts them: it records
  the cache path and, when the duration differs from what was scheduled, resizes the slot and
  rebuilds the rest of that channel-day.
- `meta` is required for fetched material, which PiTV has never seen; PiTV creates its
  catalogue entry from it. For episodes `show_title`, `season` and `episode` echo the request
  exactly, so the delivery is filed against it even when the fetched title differs.
- A `skipped` item still carries a `file` block measured from the existing target, except a
  skip whose message is "being written by another process": its file is incomplete, it has
  `"file": null`, and PiTV ignores it (no attempt used, no failure logged) until a later report
  delivers it measured.
- Every `file` block carries `vcodec` and `interlaced`: they decide how PiTV decodes the copy.
- A fetch that turns out to duplicate something already filed: if the existing copy is on the
  cache drive it is reported `done` with that file's block and meta. If it is only on the NAS
  the item is `failed` with `"existing_uid": "nas:<source id>:<relpath>"`; PiTV binds the
  request to that catalogue entry and counts neither an attempt nor a failure.
- Fetched advert `meta.family_safe` is `true`, `false` or `null`; `null` means no verdict and
  PiTV decides from tags and its keyword list, as it does for the index.
- A fetch request with `season: 0` is a special.
- Schema 1 reports (no `file` block) are still accepted during the transition: the path is
  recorded and the requested length is kept.

## 4. Sources

pitv_content's API owns the source configuration; PiTV's admin Sources page is a view of it.

- `GET {content_tool_url}/api/sources` returns the NAS sources as in the index, plus
  `health` (mounted, readable, item count, last indexed).
- `PUT {content_tool_url}/api/sources` with `{"id", "name", "type", "category", "root",
  "remote", "enabled"}` adds or edits one; `{"id", "delete": true}` removes one. Returns the
  list, or 400 `{"errors": {...}}`.
- Online providers stay on `GET/PUT /api/providers` as already agreed.

## 5. Shared cache rules

Unchanged from schema 1: pitv_content writes `.part` files and renames atomically, never
deletes, touches `running_marker` while working, and calls `POST /api/content/make-room`
before a large job. PiTV ignores `.part` files, never evicts a file in the current manifest or
younger than two hours, and does not evict while the marker is fresh.

## 6. Timing

01:00 pitv_content main run (index, then manifest). 04:00 PiTV imports the index and extends
the schedule. 05:00 pitv_content catch-up run. 06:00 and 07:00 PiTV readiness checks: anything
not playable from the cache (or the NAS, with fallback on) is replaced and logged as an error.

## 7. Host details

`GET {content_tool_url}/api/system` returns the machine pitv_content runs on, in the shape PiTV
reports for itself (`pitv/hostinfo.py`). PiTV's admin shows the two side by side, which stays
right if the applications are ever split across machines. Any reading that cannot be taken on
the platform is `null`.

```json
{"version": "0.3.0", "python": "3.12.3",
 "tools": {"ffmpeg": "5.1.6", "ffprobe": "5.1.6", "yt-dlp": "2026.08.19", "curl": "7.88.1"},
 "hostname": "pitv", "model": "Raspberry Pi 4 Model B Rev 1.5", "pi": true,
 "uptime_s": 583200, "load": [1.09, 1.39, 2.29], "temperature_c": 52.0,
 "memory": {"total": 4038000000, "available": 2511000000}}
```

- `tools` maps each external program the application depends on to its version, or `null`
  when it is missing. PiTV reports `mpv`; pitv_content reports `ffmpeg`, `ffprobe`, `yt-dlp` and
  `curl`. The admin renders whatever keys arrive. pitv_content reads the versions at start-up
  and hourly, so a request never waits on a subprocess.
- `model` is the board from `/proc/device-tree/model` on a Pi, else the OS and architecture.
- `uptime_s` is the machine's uptime from `/proc/uptime`; `temperature_c` is
  `thermal_zone0`; `memory` comes from `MemTotal` and `MemAvailable` in `/proc/meminfo`, in
  bytes.

## 8. Lookup (PiTV's admin asks, pitv_content answers)

`GET {content_tool_url}/api/lookup?kind=show|movie|advert|music&title=...&year=1988&artist=...&limit=8`
searches the internet for what the admin is about to add, so the right title is added and
later fetched. PiTV's admin calls it through PiTV's proxy (`/api/content/tool/api/lookup`);
PiTV never contacts the sources itself, as all online access is pitv_content's.

```json
{"candidates": [
  {"match": {"source": "tvmaze", "id": "1234", "url": "https://www.tvmaze.com/shows/1234/count-duckula",
             "imdb": "tt0086690"},
   "kind": "show", "title": "Count Duckula", "year": 1988, "end_year": 1993,
   "genres": ["Animation", "Comedy", "Children"], "runtime_minutes": 22, "episodes": 65,
   "network": "ITV", "country": "GB", "certificate": null,
   "summary": "Plain text, at most 600 characters.", "image": "https://.../poster.jpg"}
 ],
 "sources": ["tvmaze"], "errors": {}}
```

- Series come from a source with episode lists (TVmaze needs no key). Films come from TMDb,
  then OMDb (each needs a key, set in pitv_content's settings under Lookup; the order is the
  `film_lookup_order` setting): `match.source` is `tmdb` or `omdb`, with the IMDb id, a UK
  certificate (US when there is no UK one) and a poster; a film both return is offered once.
  Without a key the answer is 502 "no film source configured: set a TMDb or OMDb key". TMDb's
  terms require the credit "This product uses the TMDB API but is not endorsed or certified by
  TMDB." wherever its data is shown; PiTV's lookup results show it under TMDb candidates. Adverts and music videos are candidates for the video itself: `match.source` is the
  video site, `match.url` the video, and `duration_seconds`, `uploader`, `max_height` (the
  best resolution the video offers) and a thumbnail as `image` replace the series fields. A music candidate may carry `artist` and the release
  `year` from a music database.
- Candidates are ordered best first. Every field but `match`, `kind` and `title` may be
  missing or null. `summary` is plain text; `image` is an https URL the admin may show.
- `sources` names what was asked; `errors` maps a source that failed to its message, so
  partial answers are still usable. With no source reachable the answer is 502
  `{"error": ...}`.
- PiTV keeps the chosen `match` with the line-up entry (`lineups.json` included) and sends it
  on every fetch request for that title (section 2). For an advert or music video the chosen
  video's URL is kept as the wanted request's `ref`.

