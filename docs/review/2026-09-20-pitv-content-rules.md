# pitv_content's rules, and where the two applications hold the same one (review of 20 September 2026)

Part of the review Pete asked for on 20 September. pitv_content's session wrote this inventory
of its own side at PiTV's request; it describes pitv_content ffdaf19, which is what was pushed
and running that day. It is condensed here; the decisions taken on it are at the end and in
[the review summary](2026-09-20-review.md).

## The rules, with where they live

Order and pacing of delivery (`cache.py`, `api.py`):

- What is worked first: `requests_from_manifest` and `_delivery_order`. Five classes in order:
  anything scheduled whose deadline is within `deliver_soon_hours` (3), soonest first and exempt
  from every limit below; fetches, by PiTV's priority then deadline; copies by deadline;
  transcodes by deadline; unscheduled fetches last.
- A slice runs `cache_slice_minutes` (30; 0 works the whole manifest). The fetches' share is
  `FETCH_SHARE` (a half) and never under `FETCH_SHARE_MIN_S` (900 s), with time its only limit; a
  slice that has run over still takes its share, passing over what lies between.
- One transcode a slice (`TRANSCODES_PER_SLICE`), none when a band helping is waiting.
- Turns: `api._launch_next`, `SLICES_PER_HELPING` 2 while the run's summary says "(N to fetch)".
- Unfinished work comes back (`_requeue_slice`, five interruptions, the count reset by a slice
  that ends normally; `_requeue_remainder` for band helpings).
- Room: `_Budget`, `dir_bytes`, `_room_for`, `CacheFull`. The cap is the manifest's
  `cache_max_bytes`, measured over the whole cache folder at the start of each run and slice;
  `Pitv.make_room` asks PiTV for the difference.

Which upload is accepted for an episode (`cache.py`, `episodes.py`, `lookup.py`, `catalogue.py`):

- The gate is the `accept` function built by `Fetcher._plan` and applied in `_search`.
- Is it the series: `catalogue.Show.matches`; for a series PiTV confirmed against a source, the
  request's own show and never one of pitv_content's catalogue entries.
- Is it the episode: `episodes.says` answers for, unsure or against. The episode's title wins;
  failing that a number, against either the real number or the one PiTV asked with. Naming
  another episode is against; saying nothing is unsure, and refused.
- Namesakes: `lookup.namesakes` and `episodes.names_it`. Where another series has the same name a
  number is no evidence; the upload must carry the episode's title or a year inside the run
  (`lookup.series_years`, from the matched source).
- Which episode N is: `lookup.matched_episode`. Season 1 or none counts through the whole run in
  broadcast order, specials left out; another season is read as written.
- Past the end: "no such episode: the series has N". Nothing qualified: "not found yet: ...".
- Search order: the channel the last episode came from, the episode's real title, PiTV's
  `search.phrase`, then its hints, moving on only when the one before accepted nothing.
- Length window 0.6 to 1.6 times the episode's own running time when PiTV sent none; year window
  PiTV's `year_tolerance` around the episode's year, not the series' first.
- Ranking what passed: `search.score_programme`, then quality among near-equal scores. Quality
  never outranks relevance.

The not-found memory (`recent_miss`, `remember_miss`, twelve hours) is keyed on the confirmed
identity with season, episode and title, so a corrected match is a new question at once.
Looking ahead (`_same_series_next`, four a visit; `fetch_ahead`, two beyond the schedule) runs
only on what is left of the share.

Duplicates: `fingerprint.py` (four phases, each hash of the half second before it, compared
across eighths of a second), `pipeline._duplicate_of` against `dup_threshold` (10 bits of 64),
`pipeline._already_filed` by artist and title before anything is downloaded.

Bands and music: `pipeline.run`, `tracklist.tracks_for`; listing sources in `charts.sources()`
(UK best-selling singles by year, Billboard year-end) above MusicBrainz; `metadata.Pool` rotates
the naming sources; `music.is_the_song` decides whether an upload is the song.

Copy or encode, and the screen: `encode.can_stream_copy` (H.264, progressive, at most one and a
half times the profile's height, not an advert); `profiles.py` and `cache.with_manifest_profile`.

Year, certificate, genre: three year readers (`naming.extract_year` 1960 to 1999,
`episodes.years_named` 1930 to 2039, `music.open_year`); `library.raw_certificate` passes the
NFO's text through; `genres.py` is the vocabulary.

## Where both applications hold the same rule, and who decides

| Rule | PiTV | pitv_content | Authority |
|---|---|---|---|
| Genre vocabulary | `pitv/genres.py` | `genres.py`, served at `GET /api/genres` | pitv_content for the names and synonyms. PiTV keeps its copy, because it must schedule with the tool down and its module also holds scheduling notions the tool has no use for, and `pitv doctor` reports any drift between the two |
| Display profiles | `pitv/display.py` | `profiles.py` | PiTV, which owns the screen. It is to push the profile's values, not its name, and pitv_content to keep no table (STATUS C14) |
| Copy or encode | `hwdec.pi_can_play`, for NAS material in the manifest | `encode.can_stream_copy`, for what it fetches | Each for its own material, now stated in the contract; pitv_content's height cap to come from the manifest profile so there is one number |
| Certificates | `rules.normalise_cert` | raw text only | PiTV. Settled |
| Episode numbering | asks for the Nth | maps N to the real season and number | pitv_content maps; PiTV does not |
| Length window | `WANTED_MINUTES` sent as `search.duration_minutes` | the episode's own running time | PiTV's when given |

## Where pitv_content's code was stricter than the contract's text

1. The namesake rule was not in section 3. Added.
2. An upload's year is judged against the episode's year, not the request's. Added.
3. PiTV's eviction order tells an encoded file by what `pi_can_play` refuses, which works for NAS
   material only: a fetched file re-encoded to the screen passes that test and would be evicted
   as though it were a cheap copy. pitv_content is to publish a per-item flag in the index.

Consistent, checked: a fetched 720p H.264 source is filed without re-encoding for a standard
definition screen, as section 2 says; `meta.title` for a matched series is the episode list's.

## Decisions taken

- The vocabulary, profile, copy-rule, numbering and length-window authorities above.
- pitv_content publishes `encoded` per item; PiTV's eviction reads it for fetched material. Done on
  both sides: PiTV keeps the flag on import and `lineup.evict_fetched` takes what was filed as found
  before what was re-encoded, oldest aired first within each.
- pitv_content folds its three year readers and two placeholder-title patterns into
  `episodes.py`, and its ordering constants into the head of `cache.py`, as a change of its own
  after this review.
