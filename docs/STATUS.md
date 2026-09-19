# PiTV status: what stands between here and end-to-end testing

The open work across PiTV and pitv_content, who owns each item, and how it will be judged
done. Requirements are in [REQUIREMENTS.md](REQUIREMENTS.md), the design in [PLAN.md](PLAN.md),
and the wire between the two applications in [CONTENT_CONTRACT.md](CONTENT_CONTRACT.md); this
page only tracks what is not yet true of them. An item leaves this page when its check has been
run and passed, not when its owner reports it finished.

Last reviewed 2026-09-18.

## The test this is working towards

End-to-end testing starts when all of these hold on the development machine for a full
broadcast day, and is then repeated on a Raspberry Pi 4:

1. Every channel's week is built with no holding card, episodes in order, and no file shown
   twice in a day outside the documented repeat rules.
2. The player takes what is on air from the cache. "Playing from the NAS: not in the cache" is
   the exception the log reports, not the normal state.
3. The music channel's bands appear in the guide at their configured times and lengths under
   their own names, with material of their genres and decades, enough that a band's holding
   card is the exception.
4. Remote line-up entries scheduled more than `external_lead_hours` ahead are delivered before
   they air, and what is not delivered is replaced by the readiness check without a gap.
5. A fresh rebuild from the admin returns to this state without anyone intervening.

## Who owns what

| Area | Owner |
|---|---|
| PiTV: catalogue import, line-ups, the scheduler, the guide, playback, the admin, cache eviction, readiness | the Claude session in this repository, which also coordinates the rest |
| pitv_content: sources, the index, searching, fetching, encoding, the job queue | the Claude session in the pitv_content repository; this repository never edits it |
| The contract | both; PiTV holds the text and changes it only with pitv_content's agreement |
| Decisions about Pete's media and about stopping or starting services he controls | Pete |

Any other agent committing to PiTV keeps off `pitv/scheduler/`, `pitv/wanted.py`,
`pitv/lineup.py` and `pitv/guide.py` unless the change has been agreed here first: those hold
rules that REQUIREMENTS.md names tests for, and a change there is a change to the requirement.

## Open in pitv_content

In the order that unblocks testing. Each was sent to its owner with the evidence.

| # | Item | Blocks | Done when |
|---|---|---|---|
| C1 | A listing source for named, dated, genre-labelled tracks. MusicBrainz landed in pitv_content 0bec644: listings by tag and first release date, tags mapped through the shared genre vocabulary, spread over the band's years, two songs an artist a year, misses remembered for six weeks. It finds the period but not reliably the hits, since MusicBrainz records nothing about popularity | 3 | A helping's log shows songs made against names listed, the misses are counted, and PiTV's import shows the rows live under the canonical genre with the listing's years; the ratio of made to listed is recorded here |
| C3 | Stop downloading further uploads of a named track once one is filed (about half of a run's time). Landed in 217ba21; to be checked on the next band run's log | 3, 4 | A run's log shows no "duplicate of" for a track it made earlier in the same run |
| C4 | A mid-run index publish rescans only the fetched sources and merges the rest (seven minutes before, every fifteen). Landed in 217ba21; to be checked on the next band run's log | 3, 4 | A mid-run publish takes under thirty seconds in the log |
| C5 | Copy rather than re-encode fetched video that already fits the copy rule. Landed in pitv_content 63b742b: H.264, not interlaced, at most one and a half times the frame is remuxed with the sound levelled, and crop detection is skipped; adverts are always re-encoded because they are cut at exact boundaries. Its owner measured 6.4 s against 24.4 s for a fifty second 720p clip. The cost is disk: about three times the size (40 MB rather than 12 for a four minute video), so the cache drive's size matters more (see P6). A fetched file smaller than the frame now keeps its own size | 3, 4 | PiTV times a helping: a typical music video filed in under a minute on the development machine |
| C15 | A cache run starves band collection. Helpings let cache work pre-empt a band within one helping, but a cache run with transcodes runs for hours (on a Pi, longer than a day) and nothing collects until it ends: on 18 September no song was fetched between 18:21 and 20:08 while one run transcoded films. A cache run should work the manifest in slices and let one catalogue helping through between them Landed in pitv_content c35c10b (and 218ea68: interrupted delivery and index jobs come back too; a job stopped at an operator's request stays stopped); its check failed on the night of 18 September: no band helping started in three hours of sliced delivery, the between-slice turn going each time to an index job paired with the hourly cache runs; reworked in pitv_content b16d51d, 42d0235 and a third fix found by watching the queue: a slice's turn goes to a waiting band, a slice started while a band waits only copies, an index paired with a delivery waits for it, and the rest of a job gives way to a band unless a band has just run. A band helping started at 01:01 on 19 September, the first in four hours; check pending on the song count | 2, 3 | With a cache run and band runs both queued, new songs keep arriving while the manifest is delivered |
| C16 | A band helping interrupted by a restart, a signal or a crash is lost: the Rock band's run was killed at a deploy restart and never requeued, and PiTV, having been told the request was accepted, did not ask again, so "Rock Hour" stayed at four songs with nothing intending to fix it. An interrupted helping goes back into the queue with its remaining count Landed in pitv_content c35c10b; awaiting its check | 3 | Restarting the service during a helping leaves that band queued |
| C17 | A cache run honours `cache_max_bytes`. On 18 September a run took a cache capped at 10 GB to 13 GB, on a RAM disk, because it goes by the drive's free space; the manifest carries the cap. Ask make-room for the difference, and stop with the "drive is full" report (pitv_content c9befe5) when PiTV cannot free it Landed in pitv_content 513e9c8 (the whole cache directory is measured at the start of each run and slice, fetched material included); awaiting its check | 2 | With the cap below the manifest's size, the cache directory never exceeds the cap |
| C18 | The listing backs off when MusicBrainz answers 503 (one helping logged 95 retries at the same pace), and a lookup that failed in transport is not remembered as "nothing known" for six weeks; chart page cell markup (`data-sort-value="Jackson"\|`) is stripped from artist names Landed in pitv_content b16d51d, with 0201074 (a chart written as a list was being skipped and the albums table read in its place, so a 1970 band was sent to find Led Zeppelin II as a single; 73 poisoned misses purged) and 6e1d3f8 (a row MusicBrainz calls an album is dropped, and a year with more than three is re-read); awaiting its check | 3 | A helping's log shows no run of 503s, and no miss recorded for a transport error |
| C20 | Lookups rotate across several sources instead of leaning on one. On 18 and 19 September MusicBrainz refused for hours after one bad night and every band helping sat in backoff making nothing. A pool of configured listing sources behind one interface (no key needed: iTunes Search, Deezer, Wikidata, TheAudioDB; with a key entered in the admin: Discogs, Last.fm), each with its own pace, breaker and remembered rest; every lookup goes to the next in turn; one cache for all, recording who said what; years reconciled towards the original release and never later than the chart year; genres through the shared vocabulary. Pete's direction | 3 | With any one source refusing, a helping still labels and files its songs, and no source's log shows a run of refusals |
| C21 | Series-level acquisition. Every line-up episode is its own search, and demand is 55 to 72 new episodes a day against 3 delivered in a day. Find a series once (a playlist, a channel, an Internet Archive collection), record where it lives against the identity PiTV sends, fetch episodes from there in order without searching again, and take the next few in the same visit. PiTV now lists every fetch for the whole built schedule with its air time (9eb3068), so there are days in hand | 4 | A week's placeholders for one series are delivered from one find, ahead of their air times |
| C8 | Inside one job: searches and downloads run ahead of a single analyse-and-encode stage; one or two decode passes instead of four | 3, 4 | Timed by PiTV against C5's figure |
| C9 | SIGTERM in a job cleans its temporary files, releases the run marker and reports what it had delivered. Landed in pitv_content 832f9f0; to be checked the next time a job is stopped mid-run | 5 | Stopping the service mid-run leaves no `encode-*` files and PiTV receives a report |
| C10 | Catalogue runs take and refresh the running marker and ask PiTV to make room before fetching | 2, 5 | A long band run is visible to PiTV's eviction as running |
| C14 | The screen profile is defined once. pitv_content resolves the profile name PiTV pushes against a table of its own, so the 720p ceiling had to be changed in both repositories (pitv_content d49808a) and a catalogue run would otherwise have gone on fetching 1080p. PiTV should push the profile's values, or pitv_content read them from PiTV, so the two cannot drift | none | Changing a profile value in PiTV alone changes what a catalogue run fetches |
| C19 | `pitv-content doctor` and `GET /api/doctor` on the loopback API: a read-only report, findings first in sentences (a job failing repeatedly, a provider refusing, sources unreadable, the cache at its cap, stale part-files, how long since a band helping last ran and last made anything, a chart or listing year being distrusted), then the sections. Sources by id and state, never by path; no credentials, token paths or manifest URLs, since it is meant to be pasted. PiTV's `pitv doctor` embeds it. Agreed, after C8 | none | PiTV's report carries pitv_content's findings |
| C11 | The remaining review items: NFO written before the media is renamed into place, `Jobs.cancel` verifies the pid, blocking work out of request handlers, one year parser, rename debris, track and catalogue data out of code | none | Reviewed against the original list when the rest is done |

Closed and verified from PiTV's side: the index lists fetched folders and is complete; urgent
band runs queue behind cache and index work and survive a restart in that order; a run
republishes the index as it collects; years come from the upload or a named track, never the
request; no invented "Music" genre; fetched concerts are flagged; Kodi extras are not indexed;
`work_dir` is confined, yt-dlp options are checked on load, searches ignore user config, the
API refuses a non-loopback bind; a reset names what it could not remove.

Closed since: C6, identical band requests are one job (cbd9644, ee003aa), seen in
`GET /api/status` as eight distinct band runs with the true duplicate cancelled.
C7, band runs in helpings (6591751, and 265cbda for runs queued before helpings existed):
seen live as one active helping of ten and six queued at ten each of their counts; a helping
that files nothing ends its band. The helping size is pitv_content's `catalogue_helping`.
C3, no further uploads of a filed song are downloaded: no "duplicate of" in either band log
written since 217ba21, against three in the last log before it.

C12, a cache run delivers from its first line (207fc3b): run 6a1e opened with copies thirty
seconds after it started, no walk of the shares, and its index job queued behind it unasked.
C13's listing is in (074b37f): UK best-selling singles by year, then Billboard year-end, above
MusicBrainz; genre from the recording, then the artist, then pitv_content's artist map. Its
check (recognisable singles in a band's first helpings) is met by the first Nineties helping
(World in Motion, The Power, Killer, Nothing Compares 2 U); one case is open with its owner:
a reissue that charted years after its record (The Joker, 1973, filed as 1990) should take
the recording's first release year.

C2 and the dev cache (19 September, 00:11): with the cache on `/media/pclarke/18TB/cache`, one
night's sliced run took the next 24 hours from 12% to 95% cached in two hours (426 of 446 files,
191 GB against a 270 GB cap), with no failures once the slices re-read the manifest, and the
player has reported no NAS fallback since. Condition 2 holds on this machine.

pitv_content 265cbda is committed and running on this machine but not pushed: the GitHub token
here has expired and Pete has to sign in again.

## Open in PiTV

| # | Item | Done when |
|---|---|---|
| P1 | Time a band run and a cache run as each of C1 to C8 lands, import, rebuild, and record the result here | Figures recorded against each item above |
| P3 | Hardware verification on a Raspberry Pi 4: hardware decode of copied files, the cache drive, encode times with the Pi's presets, the player keeper under systemd | REQUIREMENTS.md rows marked "by hand" checked on the device |

## How a change is accepted

A change to either application is accepted when the full PiTV suite passes (about ninety
seconds: `.venv/bin/python -m pytest -q`), the documents say what the code now does, and the
behaviour has been seen on the development machine's real library, not only in the fixture.
The suite must be run to completion before a commit; a day of scheduler defects went unseen
when it took an hour and nobody waited for it.
