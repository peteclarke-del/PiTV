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
3. The music channel's bands appear in the guide under their own names with material of their
   genres and decades, enough that a band fills most of its stretch.
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
| C13 | A chart source ahead of MusicBrainz: UK top-ten and best-selling singles by year first (the project is British television of the period, and a music slot felt right because it was the singles people knew), Billboard year-end second; genre from MusicBrainz for the recording; charted singles rank first in a band's listing. Agreed to follow C9 | 3 | A band's first helpings are recognisable singles of its years; a spot check of one band's listing against the chart pages |
| C2 | Cache delivery actually delivers. The cause was order, not speed: requests were worked by priority and deadline alone, so an hour-long remote episode to be searched for, downloaded and encoded sat ahead of 519 plain copies, and every interruption restarted the manifest. Fixed in pitv_content 3a0dd22 (copies first); awaiting a cache run, which is queued behind a band run that will not yield until it ends (see C7) | 2 | After one cache run, the manifest reports at least 95% `already_cached` for the next 24 hours and the player's status no longer reports NAS playback |
| C3 | Stop downloading further uploads of a named track once one is filed (about half of a run's time). Landed in 217ba21; to be checked on the next band run's log | 3, 4 | A run's log shows no "duplicate of" for a track it made earlier in the same run |
| C4 | A mid-run index publish rescans only the fetched sources and merges the rest (seven minutes before, every fifteen). Landed in 217ba21; to be checked on the next band run's log | 3, 4 | A mid-run publish takes under thirty seconds in the log |
| C5 | Copy rather than re-encode fetched video that already fits the profile's copy rule (H.264, at most 864 lines). Agreed. How much it saves depends on the profile's `max_source_height`, which PiTV owns: at the present 1080 for a 576 line screen almost nothing fetched qualifies for the copy. Pete's decision, see P4 | 3, 4 | A typical music video is filed in under a minute on the development machine; PiTV times a run |
| C12 | A cache run stops opening with a full walk of every share. Landed in pitv_content 207fc3b, with an index job paired to every cache run by the API (seen: the reply carries `index_job_id`); awaiting a cache run's log. Three cache runs today were ended by deploy restarts before they delivered, which is the practical case for C9 | 2 | A cache run's log begins with deliveries, and an index job follows it unasked |
| C8 | Inside one job: searches and downloads run ahead of a single analyse-and-encode stage; one or two decode passes instead of four | 3, 4 | Timed by PiTV against C5's figure |
| C9 | SIGTERM in a job cleans its temporary files, releases the run marker and reports what it had delivered | 5 | Stopping the service mid-run leaves no `encode-*` files and PiTV receives a report |
| C10 | Catalogue runs take and refresh the running marker and ask PiTV to make room before fetching | 2, 5 | A long band run is visible to PiTV's eviction as running |
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

pitv_content 265cbda is committed and running on this machine but not pushed: the GitHub token
here has expired and Pete has to sign in again.

## Open in PiTV

| # | Item | Done when |
|---|---|---|
| P1 | Time a band run and a cache run as each of C1 to C8 lands, import, rebuild, and record the result here | Figures recorded against each item above |
| P2 | The two bands of one decade play the same few songs at 08:00 and 15:30 while the library is thin. Acceptable for now; revisit when C1 has delivered, and decide with Pete whether a band should rather stay dark than repeat what aired earlier that day | Decision recorded |
| P4 | `max_source_height` per display profile. pitv_content fetches the best source up to that ceiling because Pete asked it to; at 1080 for the 576 line CRT profile every fetch is a large download and a full re-encode. Lowering it to 720 lets most fetched video be copied. Pete's choice between source quality and speed | Decision recorded and the profile set |
| P3 | Hardware verification on a Raspberry Pi 4: hardware decode of copied files, the cache drive, encode times with the Pi's presets, the player keeper under systemd | REQUIREMENTS.md rows marked "by hand" checked on the device |

## How a change is accepted

A change to either application is accepted when the full PiTV suite passes (about ninety
seconds: `.venv/bin/python -m pytest -q`), the documents say what the code now does, and the
behaviour has been seen on the development machine's real library, not only in the fixture.
The suite must be run to completion before a commit; a day of scheduler defects went unseen
when it took an hour and nobody waited for it.
