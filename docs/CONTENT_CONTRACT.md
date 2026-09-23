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
scheduled and deletes derived cache/acquisition state only when PiTV explicitly requests a
Fresh rebuild. The scheduled services never write to the NAS;
pitv_content's `--dest nas` catalogue option is a manual operator action outside this
contract, and anything it files reaches PiTV through the index like any other NAS file.

## Flow

1. pitv_content indexes its NAS sources and publishes the library index.
2. PiTV imports the index into its catalogue. Custom programming (titles not on the NAS)
   is added to the catalogue in PiTV's admin as line-up entries.
3. PiTV builds the schedule from each channel's line-up and publishes the request manifest:
   every programme, advert, ident and music video it needs through the end of the next
   broadcast day, and everything that has to be fetched for as far ahead as the schedule is
   built, since a fetch needs days of notice where a copy needs minutes.
4. pitv_content works the manifest by priority and deadline: items with a NAS source are
   copied or transcoded into the cache; items without one are searched for online, fetched,
   encoded and cached. Remote-dependent scheduled items receive a 24-hour preparation boost;
   an urgent band-collection job goes ahead of other catalogue runs, never of cache or index
   work, since nothing fetched can be scheduled until an index names it.
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

A successful catalogue fetch (`POST /api/run` with `mode: "catalogue"`) performs this index
publication before the job completes. PiTV can therefore import newly acquired band material,
rebuild days that are still mostly filler, and request the next deficient band without waiting
for the nightly index run.

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
- `certificate` is the NFO's own text (`<certification>`, else `<mpaa>`), whitespace collapsed
  and at most 120 characters: `PG`, `UK:12 / UK:12+`, `US:R / US:Rated R`, `UK:All`. PiTV reads
  it (a British entry first when the text lists several) and owns that reading, so
  pitv_content does not interpret it. `NR`, `Not Rated`, `Unrated`, `N/A` and an empty tag are
  published as null.
- `ids`, on a show or an item, holds the NFO's online identifiers, only the keys it has:
  `{"imdb": "tt0080684", "tmdb": "1891", "tvdb": "76107"}` (`tt` and five to ten digits; one
  to nine digits for the other two). The field is absent when the NFO has none. An episode's
  `ids` are the episode's own; the show's `ids` identify the series. PiTV keeps what fits
  those shapes and sends it with its online checks (section 8).
- `encoded`, on an item pitv_content fetched, says whether it re-encoded the file (`true`) or
  filed it as found (`false`). It is absent on NAS material, where PiTV's own `pi_can_play`
  answers the same question. PiTV keeps it for eviction (section 5).
- A `complete` index lists every item; PiTV marks anything absent from it as missing. An
  incomplete index (`"complete": false`) only adds and updates.
- Anything that walks every source while a delivery is outstanding publishes incomplete: a full
  index job and a band run that ends by refreshing the library. A scan taken before the later
  slices of a delivery would otherwise retire the very files that delivery's own report had just
  filed, and would do it again every night. A run that republishes only the folders it wrote to
  inherits the completeness of the document it merges into and is unaffected. The index that a
  delivery run takes before it starts stays complete, because it precedes the files it omits
  rather than postdating them.
- PiTV never retires material whose origin is `online`, whatever an index says: that is what a
  delivery report filed, and reports are the only authority over it. So the exemption and the
  rule above are two independent protections for the same material, and the NAS sources and the
  acquired folders have only the second.
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
              "max_source_height": 720, "vcodec": "h264", "acodec": "aac", "max_bitrate_kbps": 4000,
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
     "channels": [2], "first_air_ts": 1789549200, "deadline_ts": 1789548300, "priority": 0,
     "source": null, "action": "fetch", "remote_required": true,
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
  up to `max_source_height` lines: 720 for a standard definition screen (which keeps a fetched
  H.264 source one pitv_content can file without re-encoding), two rungs above the target on the 720, 1080, 1440, 2160
  ladder for an HD screen, and equal to it at 4K. Prefer the higher resolution, then the higher bitrate, among
  hits of the same title; never go above the ceiling, and take a lower source only when
  nothing better exists. A copy or transcode from the NAS uses the file there as it is.
  Catalogue runs and band helpings have no manifest, so PiTV also sends the screen by
  `PUT /api/settings {"profile": <name>, "video_profile_values": {...}}`, where the values are
  this same `profile` object. `pitv/display.py` is the only table of screens: pitv_content
  encodes to the values, and the name is what it falls back to for any value not given or not
  usable, and is kept for its logs and admin. So a screen pitv_content has never heard of is
  obeyed in full, a known name with one unusable value still encodes to that screen rather than
  to nonsense, and a box with no PiTV keeps working. An empty object clears them and the name
  decides again; `frame_rate: null` means the source's own rate. Sending the same values again
  changes nothing and starts nothing, which is why PiTV may push after every start. PiTV sends it when the admin changes the screen, after every start of
  the player, and whenever the values change (a release that alters the table counts), trying
  again each maintenance pass until it is taken. A pitv_content from before it took the values
  answers 400 with `video_profile_values` as the only unknown setting; PiTV then sends the name
  alone, which that version resolves against a table of its own.
- Every scheduled file appears once, however many slots or channels use it.
- `action` is `copy` (the Pi can play the source as it is: H.264 to 1080 lines, HEVC to 2160,
  progressive standard definition MPEG-4 or MPEG-2; PiTV decides, whatever the screen),
  `transcode` (it cannot), or `fetch` (there is no known source; find it online, encode it to
  the profile and file it under `dest_dir`). Every `fetch` names something a person or a
  line-up asked for, so its title, year and genre are an instruction. PiTV never asks again
  for a file that came from a band collection (section 9): that row's title and year are
  pitv_content's own reading of an upload, and a search for them finds something else.
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
- A `match` whose `source` is `youtube_channel` names a creator's channel or a playlist
  (section 8, `kind=channel`). pitv_content lists `match.url` directly to get the videos in
  order; it does not consult a provider row of that type, and one is not required. The video
  itself is then fetched by its id like any other, so the run needs an enabled provider that
  serves YouTube ids for the kind the request classifies as. Disabling the general YouTube
  provider therefore stops every line-up entry keyed on a channel, whatever its `match` says,
  which is not obvious from the entry or from the provider being switched off.
- The episode number on such a request is the video's position in the listing, not an identity.
  It drifts for two reasons: a video the creator deletes shifts everything after it, and a
  change in what pitv_content counts does the same. Shorts were numbered as episodes until
  `channel_shortest_minutes` was added, so episode 1 of ten channels was a thirty second clip
  that no length rule would accept, and excluding them renumbered every one.
  This is bounded and is not designed around. What arrives becomes a catalogue row carrying the
  video's own id, so the identity of a delivered programme is the video's and never its
  position; the position only decides what is asked for next, and the check on episodes already
  held absorbs an overlap. The exposure is asking for something already there, or stepping over
  one, neither of which loses anything.

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
  catalogue entry from it. For episodes `show_title` echoes the request exactly, so the
  delivery joins the series asked for even when the fetched title differs; `season` and
  `episode` echo it too, except for a series with a confirmed match (below).
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

- A programme is taken only in the owner's language (pitv_content's `programme_language`,
  default `en`; music is exempt, a song being in whatever language it is in). An upload is
  refused when its title marks another language or a dub (a code in brackets, a language named
  in words, a title in another script; "eng sub" is no refusal, "sub ita" is), when the site
  declares another language and offers no audio format in ours, or when the file's only sound
  track declares another; where several tracks are offered ours is the one kept. Such a
  refusal reads `not found yet: no upload of this episode in en (N in another language)`.
- A request for an episode of a matched series that the source's episode list does not have
  fails with a message beginning `no such episode`, followed by the length of the run when it
  is known: `no such episode: the series has 6`. PiTV does not ask again, records the length
  on the line-up entry and asks for nothing past it. Delivering a near match under the
  number asked for is never right: PiTV airs it as that episode.
- For a series with a confirmed `match`, PiTV always asks for season 1, episode N, meaning the
  Nth regular episode of the whole run in broadcast order (specials are left out), because it
  does not know the season structure of something it does not hold. A season other than 1 in a
  request is read as written. pitv_content maps N through the match's episode
  list and files the episode under its real season, number and title; `meta.season`,
  `meta.episode` and `meta.title` are then the real ones, not an echo of the request, and the
  delivery is bound to the request by `request_id`. `meta.episodes_total` (on a delivery, and
  alone in `meta` on a "no such episode" failure) is the length of the run.

  PiTV keeps it on the line-up entry, and every later answer replaces it, so an entry's bound
  follows the run rather than fixing it. That matters for a creator's channel, which gains
  videos every week and whose length halved the day shorts stopped being numbered: the next
  delivery or over-ask carries the new total and the bound moves with it.

  What PiTV does not do for a channel is learn the length from an online lookup, which happens
  once and is never revisited. One such count sat at 1003 for a channel that really holds 503,
  and PiTV would have spent attempts asking for five hundred videos that do not exist; the same
  number would have stopped a growing channel early had it moved the other way. Series keep that
  path, because a series that ran and ended has a length worth learning once.

  Over-asking is cheap by design. "No such episode" is a miss rather than a fault: it is counted
  rather than listed, so a run of them cannot push a real fault out of the errors list.
- `search.duration_minutes` is omitted for a channel request. The window says what a programme
  of that kind runs to, which assumes a broadcaster gave it a slot; a creator posts a two minute
  clip on Tuesday and a seventy minute one on Thursday. A television episode's twenty to sixty
  rejected five of six real videos from one channel and kept only what happened to land inside
  it, and each rejection spent an attempt, so the window would have exhausted the limit on
  videos that were never wrong. With no window pitv_content falls back to what its own listing
  says the video runs to, which is the only authority on it. When the episode list cannot be read the request fails with a message
  containing `rate limit`, which PiTV retries without using up an attempt.
- For such a series pitv_content takes an upload only when its own title says it is the episode
  wanted (the episode's title, or its number), and refuses one that names another episode or
  does not say. Where the sources list another series of the same name, a number is not
  evidence, since it is true of both: the upload must carry the episode's own title or a year
  inside the series' run. A year an upload names is judged, within `year_tolerance`, against the
  episode's own year from the episode list, not the request's `year`, which is the year the
  series began. When nothing qualifies the failure begins `not found yet:`; PiTV keeps the
  request queued with no attempt used, and the readiness check covers the slot meanwhile.

### Work a run never reached, and work it held back

Two fields, because these are two facts and reporting them as one made either impossible to
judge. A slice that ends before looking at a band is starved and nothing about it will change
on its own. A request looked at and held for a later slice is a rule working: one long re-encode
can take an evening, so a slice takes one and defers the rest. A count that meant either could
only be interpreted by guessing which, and a threshold on it would have reported the rule
working as a fault several times a day.

`run.unreached_bands` lists only the bands a slice ended before looking at:
`[{"band": 4, "name": "wanted", "requests": 1653, "first_at": 793, "of": 2446}]`. An empty list
means every band holding work was reached. A band with nothing in it is not listed, so an entry
always means requests that were ready and were not looked at.

`run.passed_over` lists work deliberately held for a later slice, per band:
`[{"band": 3, "name": "transcodes", "requests": 5, "most_slices": 14, "limit": 8}]`.
`most_slices` is the longest any one request in that band has been waiting, counted as a streak
across runs and cleared when the request is worked or PiTV stops asking for it. `limit` is the
number of consecutive slices after which pitv_content takes the request regardless, so a rule
that holds work back cannot do so without bound. A band appears in one list or the other, never
both.

PiTV records both in its run log. Starvation is reported as soon as it appears, with how many
runs running it has been. A wait is reported only once `most_slices` has passed `limit` by more
than one, since that means the bound meant to end the wait has not held and the work may never
be done.

This exists because nothing else says it. Delivery is ordered in bands, scheduled work before
unscheduled, and a run that never reaches the last of them fails at nothing, logs nothing and
finishes reporting an ordinary busy night. The only symptom is a channel that stays empty, which
is how 148 requests sat untouched for a day with both applications reporting themselves healthy.

PiTV records each entry in its run log and raises it only when the same band is unreached in two
runs running: one run that ran out of time is ordinary, since the next starts from the top and
gets further, while the same band twice means nothing about it will change on its own.

## 4. Sources

pitv_content's API owns the source configuration; PiTV's admin Sources page is a view of it.

- `GET {content_tool_url}/api/sources` returns the NAS sources as in the index, plus
  `health` (mounted, readable, item count, last indexed).
- `PUT {content_tool_url}/api/sources` with `{"id", "name", "type", "category", "root",
  "remote", "enabled"}` adds or edits one; `{"id", "delete": true}` removes one. Returns the
  list, or 400 `{"errors": {...}}`.
- A source whose `remote` is an SMB share may carry a login: `username`, `password`,
  `workgroup` (all optional; none means a guest mount). The password is write-only: `GET`
  returns `credentials: {"username", "workgroup", "set": true|false}` and never the password;
  a `PUT` without `password` keeps the saved one, and `{"id", "clear_credentials": true}`
  removes the login. pitv_content keeps logins in files only its service user (and root) can
  read, never logs them, and uses them for one thing: mounting that share read-only at the
  source's `root`, through a root helper that accepts only a source id and builds the mount
  from pitv_content's own configuration. It mounts, remounts after a change and unmounts on
  removal; `health` reports the mount. PiTV relays these fields and stores none of them.
- `POST /api/sources/test` with a source's `remote`, `root` and login (or its `id`, to use the
  saved login) tries the share without saving anything: `{"ok": true|false, "message": ...}`,
  e.g. "logged in; 3 folders at the top level" or "permission denied". PiTV's admin offers it
  as Test connection.
- NAS mounts are pitv_content's: it creates them for its sources, including those seeded from
  the installer's share list. Until a pitv_content with the mount helper is installed, PiTV's
  installer keeps writing the mounts for the installer's shares with one shared login.
- A source's `location` is `nas` (a share, which must be mounted before it is indexed, since a
  share that failed to mount leaves an empty folder behind), `cache` (pitv_content's own fetched
  folders under the cache) or `local`: a read-only folder on the machine that is neither. PiTV's
  idents live in one (`<data>/idents`, type `ident`); the channel an ident belongs to is read
  from a folder in its path named `ch<n>`, `channel_<n>` or a bare number.
- Sources may nest. A folder declared as another source's root belongs to that source and is
  left out of the walk of the one it sits in; the claim holds whether or not the other source is
  enabled, since disabling a source should not turn its files into another's. PiTV's idents
  live this way on a NAS: an `ident` source, location `nas`, whose `root` is the `idents` folder
  inside the adverts share, declared with no `remote` (pitv_content mounts a `remote` at its
  `root`, which would mount the share inside itself). It is reached through the adverts
  source's mount, and the mount check still holds through the subfolder: with the share away
  the source is unreachable, never readable and empty.
- Online providers stay on `GET/PUT /api/providers` as already agreed. A provider row says what
  it `serves`, the id space it can fetch, which is not the same as its `type`: a provider that
  searches inside one channel is of its own type and serves the video site's ids. PiTV's doctor
  reads `serves`, never the type name, which is pitv_content's to rename.
- Backup. `GET {content_tool_url}/api/export` returns pitv_content's whole configuration as one
  document (settings, providers, sources), and `POST {content_tool_url}/api/import` applies one.
  This exists because every part of that configuration is typed into PiTV's admin and relayed
  straight through: the application that owns the interface holds none of the data behind it, so
  a PiTV backup carried none of it. Ownership does not move. PiTV asks for the document when
  backing up and hands it back when restoring, rather than keeping a copy that could disagree
  with the one in use.
  Keys and share passwords are masked on an ordinary read, which is right for a screen and
  useless for a backup, since restoring a mask would write asterisks over a working key. They
  come in the clear only for `POST {content_tool_url}/api/export {"secrets": true}` carrying the
  shared token as a bearer, and the document then holds `"secrets": true` so PiTV can tell the
  owner their file wants keeping like a password. A secret pitv_content does not hold is left
  out rather than sent as an empty string, so a restore cannot quietly blank a key that was set.
  `POST /api/import` takes the token too: writing this is at least as sensitive as reading it,
  since an import could point every source at another machine's shares.

  The form and the token are the shape they are because pitv_content's API has no authentication
  of its own. It binds loopback and relies on PiTV's admin login gating the proxy, which holds
  for a person at a browser and not for any other local process, so masking is the only thing
  protecting those keys today. A GET saying `secrets=1` would be written down by everything that
  records a request line and the answer would be cacheable; a body plus a token that only the
  service user can read restores roughly the protection the masking gives. PiTV's own
  `GET /api/export?secrets=0` asks for the masked document instead.
  A pitv_content without these endpoints answers 404; PiTV records the backup as not holding
  that half, with the reason, and says so. The restore of pitv_content happens after PiTV's own
  and outside its transaction, so PiTV's half stands whether or not pitv_content is running.

## 5. Shared cache rules

In normal operation pitv_content writes `.part` files and renames atomically, never
deletes, touches `running_marker` while working, and calls `POST /api/content/make-room`
before a large job. PiTV ignores `.part` files, never evicts a file in the current manifest or
younger than two hours, and does not evict while the marker is fresh.

`POST {content_tool_url}/api/cancel {"job_id"}` drops a job that is queued, leaving a running one
alone, and marks it stopped on purpose so nothing revives it. Cancelling one already gone answers
`{"ok": false}` rather than failing. PiTV withdraws a band helping this way when the band has
filled since it was asked for: a helping can wait hours for its turn, and one for a band that is
already stocked would fetch material nobody is waiting for ahead of episodes that slots are.

`GET {content_tool_url}/api/status` carries `queue_warning`, naming a job that has waited far
longer than it should, and `healed`, the last ten jobs pitv_content ran ahead of their turn
because a rule had held them past three hours, each saying which rule and how long. No rule of
pitv_content's may hold a job indefinitely: three of them could, and all three were silent.
PiTV's doctor raises both as findings, because a queue that puts itself right without saying so
leaves the rule that held the job still there to hold the next one.

`GET {content_tool_url}/api/jobs` lists the most recent jobs and, whatever the limit, everything
queued or running however old. A job PiTV was handed can therefore always be found while it is
alive, which is what lets PiTV tell "not started yet" from "long gone".

`POST {content_tool_url}/api/reset` is the explicit exception for PiTV's Fresh rebuild. It first
stops the active job (forcing an encoder down if necessary), then removes partial work,
acquired/cache files, reports, indexes, source health/status
and pipeline fingerprint databases. It preserves NAS/local source media, logs, job history,
sources, provider configuration, catalogue choices and settings. The reply is `{"ok": true}`,
or `{"ok": false, "failed": [paths]}` naming what could not be removed (a file still open, a
busy mount) after everything else was; it is never a 500 for that. PiTV clears its schedule,
history, derived wanted rows and cache references only when `ok` is true, then requests a
fresh index and rebuilds from the retained inputs; when it is false PiTV keeps its rows and
reports the paths, so the two sides never disagree about what exists.

- PiTV evicts least recently used first, but plain copies before anything pitv_content had to
  re-encode (what `pi_can_play` refuses as it is): a copy is seconds to make again, a two hour
  concert is most of an hour in which nothing else is delivered. Fetched material goes only
  when copies have not made enough room, oldest aired first, and by the same reasoning what
  was filed as found goes before what was re-encoded, told by the index's `encoded`.

- Two rules decide copy or encode, each for its own material. `pi_can_play` (PiTV) sets `copy`
  or `transcode` for NAS material in the manifest. pitv_content's own test decides whether what
  it fetches is remuxed or re-encoded, with its height cap taken from the manifest's profile.
  PiTV's `search.duration_minutes` wins over pitv_content's own length window when it is sent.
  pitv_content alone maps an episode's place in a run to its real season and number; PiTV does
  not.

## 6. Timing

01:00 pitv_content's nightly work, as two jobs: the manifest (a cache run) and a full index
of every source. A cache run works the manifest in slices, each in this order: what is about
to air (within three hours, exempt from every limit), then the fetches' share, then copies,
then one transcode. The share is half the slice and never less than fifteen minutes, with time
its only limit; requested fetches come first in it, and a series' next episodes are looked
ahead for only with what time is left. A slice takes its share even when what is about to air
has used the slice up. A run's summary says how many requested fetches it left waiting (`N to
fetch`), and while that is not none delivery takes two turns to a band helping's one. The
order is deliberate: a copy that is late leaves the player on the NAS for that programme,
which is what the fallback is for, while a fetch that is late leaves a gap. With fetches
last, behind transcodes that end a slice, no episode was reached for a week. A cache run does not index the sources itself; when it has fetched something
it republishes the fetched folders into the last index, as a catalogue run does. 04:00 PiTV
imports the index and extends the schedule. 05:00 pitv_content catch-up run. 06:00 and 07:00 PiTV readiness checks: anything
not playable from the cache (or the NAS, with fallback on) is replaced and logged as an error.

pitv_content is never idle while anything is left to fetch. A delivery run that leaves work behind
puts the rest back in its queue, and with nothing queued it starts a delivery slice itself whenever
the last run left requested fetches waiting or a rested request has come due. A run it started that
delivered nothing buys ten minutes of quiet, doubling to an hour, cleared by any delivery or by PiTV
asking for anything, so an empty manifest cannot make it spin. `GET /api/status` carries
`idle_reason`: null while anything runs or is queued, otherwise `nothing to fetch`, `resting until
HH:MM` or `waiting for room`. Band collection is PiTV's to ask for and is never self-started.

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

`GET {content_tool_url}/api/lookup?kind=show|movie|advert|music|channel&title=...&year=1988&artist=...&limit=25`
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
- `kind=channel` searches YouTube for a creator's channel, so one can be added by name instead
  of by pasting an address. A channel is added to PiTV as a series whose episodes are its
  videos, and `match` is what every later fetch request for it carries, so it is the field that
  has to be right:

  ```json
  {"candidates": [
    {"match": {"source": "youtube_channel", "id": "UCr0d0Gw5RUqSG2ljy-4PQ2g",
               "url": "https://www.youtube.com/channel/UCr0d0Gw5RUqSG2ljy-4PQ2g"},
     "kind": "channel", "title": "Some Creator", "uploader": "@SomeCreator",
     "subscribers": 412000, "summary": "The channel's description, plain text.",
     "image": "https://.../avatar.jpg"}
   ],
   "sources": ["youtube"], "errors": {}}
  ```

  `match.id` is the canonical channel id (`UC...`), never the handle, and `match.url` is that
  id's own address rather than the handle's: a handle stops resolving the day its creator
  renames it, and a series keyed on one would stop with no error anyone reads. Neither is what
  a person recognises, so the handle comes too, in `uploader`, and PiTV's admin shows that while
  keeping the canonical address as the thing it fetches from. `subscribers` stands in
  for the episode count the other kinds carry: an exact video count means listing the whole
  channel, ten to twenty seconds for each candidate in a dialog somebody is waiting in front of,
  while the subscriber count comes free with the same probe and tells two same-named creators
  apart as well. PiTV's admin shows the avatar, the description, the handle and the subscribers,
  and keeps `match` on the line-up entry. Playlists may appear among the same candidates, since a
  playlist and a channel are one thing to the fetcher and the candidate says which it is.
- `imdb=tt...`, `tmdb=N` and `tvdb=N` (any combination) ask for one programme by identifier;
  `title`, `year` and `artist` are then ignored, and `kind` stays required because a TMDb
  number names a film or a series depending on it. The reply has the same shape. A film is
  resolved by TMDb, then OMDb (IMDb identifier only); a series by TVmaze (`imdb=` or `tvdb=`;
  it knows nothing of TMDb numbers) and by TMDb when a TMDb key is set, so it may come back
  once from each, as in a title search. An unknown identifier is 200 with no candidates; a
  malformed one, or one given with kind `advert` or `music`, is 400. When no configured source
  can read the identifiers given (`tmdb=` alone with no TMDb key, for a series or a film) the
  answer is 502 naming the key to set, and PiTV asks by title instead. PiTV's
  online check asks this way whenever the index gave it an identifier, and asks by title only
  when that finds nothing or names a programme more than two years from the library's.
- A candidate's `certificate` is the United Kingdom's, else the United States', read across every
  kind of release; no other country's is offered, since mapping one to a British certificate
  would invent it. `NR`, `Not Rated`, `Unrated` and `N/A` are no certificate, and the field is
  then absent. With both film keys set, OMDb fills what TMDb left empty, the certificate
  included.
- A search that matches nothing is 200 with no candidates; one where no source could be reached
  is 502, as for the other kinds. The two are worth telling apart on screen, because only one of
  them is worth doing anything about.
- Candidates are ordered best first. Every field but `match`, `kind` and `title` may be
  missing or null. `summary` is plain text; `image` is an https URL the admin may show.
- `sources` names what was asked; `errors` maps a source that failed to its message, so
  partial answers are still usable. With no source reachable the answer is 502
  `{"error": ...}`.
- Each entry in the manifest's `wanted` list carries a `priority` and the list is sorted by it,
  lowest first, on the same scale as `items` so the two can be read together. Nothing here has an
  air time of its own, so it is judged by the gap it would fill: the soonest holding card the
  line-up entry could go into, band by band rather than channel by channel, since a channel of
  nine bands has nine different answers. A request nothing is waiting for, a gap in a series that
  already plays or an advert added by hand, is `1000`: worth doing, worth doing after everything
  a channel is short of tonight. Ranking instead by what raised a request goes stale, because "a
  line-up raised it" becomes true of every channel eventually; a card on screen at eight tomorrow
  does not. Left in insertion order, fifteen hundred series gaps sat ahead of every video a newly
  built channel needed, and that channel would have shown holding cards for days.
- PiTV keeps the chosen `match` with the line-up entry (`lineups.json` included) and sends it
  on every fetch request for that title (section 2). For an advert or music video the chosen
  video's URL is kept as the wanted request's `ref`.

## 9. Material for bands (PiTV asks, pitv_content fetches)
The request carries `genre_families` alongside `genres`: for each genre asked for, the other
names that satisfy it. A Metal band sends `{"Metal": ["hard rock", "thrash metal", ...]}`, so
something an online check calls Hard Rock counts and something it calls Folk never does. It
widens only, never narrows, and is read one way at a time, because Hard Rock satisfying Metal
does not make Metal satisfy Hard Rock. The table is PiTV's `genre_families` setting, editable in
Settings, Programming, and it exists because a genre lookup is reliable about broad genres and
unreliable about fine ones: with a family behind each name, the coarse answer is enough.


A band is a titled stretch of a channel's day filled with short items of certain kinds, genres
and decades (PiTV, PLAN section 4.5). When PiTV finds a band short of material it asks
pitv_content to go and find some, constrained to what that band wants:

```
POST {content_tool_url}/api/run
{"mode": "catalogue", "kind": "music", "count": 26, "urgent": true, "max_minutes": 15,
 "genres": ["Soul", "Reggae", "Jazz", "Blues"], "years": [1970, 2009]}
```

- `kind` is one of the catalogue kinds pitv_content offers: the `choices` of the
  `catalogue_kinds` key in its settings schema (adverts, sport, shows, cartoons, music), not
  that key's saved value, which is the operator's pick of what the scheduled runs collect.
  PiTV reads the choices and offers them per channel and per band.
- `count` is how many new items the run adds before stopping (PiTV asks for 20 to 60).
- `max_minutes` (1 to 600) is the longest item the band can use as one of several.
- `genres` (at most 12) are the band's, spelled as the index publishes them (section 1 and
  `GET /api/genres`); `years` is `[from, to]`, both 1900 to 2100, the band's decades.
- `urgent` says the run should go ahead of other queued catalogue runs; PiTV sets it when a
  band has nothing at all. It never goes ahead of cache or index work: until an index names
  what a run fetched, PiTV cannot schedule any of it, and tonight's copies come first.

The run searches for material of those genres and years: its own catalogue of well-known
titles that match, and open searches by genre and year ("disco 1979 official music video" and
the like), so a band is not limited to what the catalogue has heard of. It files what it finds
under the genre it was asked for and stops rather than drift into other genres. Results reach
PiTV through the next library index like anything else. A particular song someone wants is
not a band request: it is added in PiTV's admin and travels in the manifest's `wanted` list
(section 2), fetched by its own title and artist.

Replies: `{"ok": true, "job_id": "...", "status": "running" | "queued", "deduplicated":
false}` (`deduplicated` is true only for a cache run that matched one already queued, whose
id is returned instead): pitv_content queues
requests and runs one job at a time, lowest priority number first (cache 0, index 10, urgent
catalogue 15, catalogue 20, dry run 30), ties in order of arrival. A catalogue run started
through the API republishes the index as it collects, at most every fifteen minutes, so what
it has fetched reaches PiTV's next import rather than waiting for the run to finish.
`400 {"errors"}` for a bad body (PiTV records the refusal and moves on); `503` or no answer
when pitv_content is down, in which case the band keeps its turn. `GET /api/status` lists
`active_job` and `queued_jobs`. PiTV submits every band that is short in one pass, identical
bands as one request, and does not ask for a band again within six hours. Asking again is
harmless: a request whose kind, genres, years and `max_minutes` equal those of a catalogue run
already queued or running returns that job's id with `deduplicated: true` (the counts may
differ). `POST /api/cancel {"job_id"}` withdraws a queued request without touching the
running job.

A band run is worked in helpings: it collects about ten items, then goes back into the queue
behind the other catalogue runs with the rest of its count, until the count is met or the
search is exhausted. With several bands waiting, each has something to show within hours
instead of the first taking everything, and cache and index work never waits longer than one
helping for the job boundary.

### Genre vocabulary

`GET {content_tool_url}/api/genres` publishes the spellings the index uses: `known` (the
canonical names), `aliases` (synonyms and what they fold into), how a synonym is matched and
how a tag holding several genres is split. PiTV keeps the same table and compares the two on
every catalogue import, logging anything that differs, so a channel that allows Children never
misses a series the index calls Kids.

### Concerts

The index says whether a music item is a concert (`concert`, section 1). pitv_content decides
that from a Concerts folder, an NFO tag, or a running time of 35 minutes or more. PiTV uses
the field as sent and applies the same length rule only when the field is absent.
