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
| C1 | A listing source for named, dated, genre-labelled tracks (MusicBrainz first, then chart pages or Discogs), searched for by artist and title. Today a band gets only what the built-in track list holds: the first run asked for 60 and found 8, all from that list, and open searches add nothing because uploads rarely state their year | 3 | A band run for one genre and decade delivers at least 30 correctly labelled songs, and PiTV's import shows them live with that genre and years from the listing |
| C2 | Cache delivery actually delivers. 5 of 519 files wanted for the next day were cached at the last look; each cache run spends its first seven minutes re-walking the NAS index | 2 | After one cache run, the manifest reports at least 95% `already_cached` for the next 24 hours and the player's status no longer reports NAS playback |
| C3 | Stop downloading further uploads of a named track once one is filed (about half of a run's time) | 3, 4 | A run's log shows no "duplicate of" for a track it made earlier in the same run |
| C4 | A mid-run index publish rescans only the fetched sources and merges the rest (seven minutes today, every fifteen) | 3, 4 | A mid-run publish takes under thirty seconds in the log |
| C5 | Copy rather than re-encode fetched video that already fits the profile's copy rule (H.264, at most 864 lines); request at most 720 lines from the provider | 3, 4 | A typical music video is filed in under a minute on the development machine; PiTV times a run |
| C6 | Identical band requests are one job: a request equal to a queued or running catalogue run returns that job with `deduplicated: true`; the stale duplicates are cancelled | 3 | Asking twice for the same band leaves one job in `GET /api/status`; contract section 9 updated |
| C7 | Band runs worked in helpings of about ten items, the remainder requeued behind the other bands, so every band has something early | 3 | With several bands queued, each has new files before any has all of its count |
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

## Open in PiTV

| # | Item | Done when |
|---|---|---|
| P1 | Time a band run and a cache run as each of C1 to C8 lands, import, rebuild, and record the result here | Figures recorded against each item above |
| P2 | The two bands of one decade play the same few songs at 08:00 and 15:30 while the library is thin. Acceptable for now; revisit when C1 has delivered, and decide with Pete whether a band should rather stay dark than repeat what aired earlier that day | Decision recorded |
| P3 | Hardware verification on a Raspberry Pi 4: hardware decode of copied files, the cache drive, encode times with the Pi's presets, the player keeper under systemd | REQUIREMENTS.md rows marked "by hand" checked on the device |

## How a change is accepted

A change to either application is accepted when the full PiTV suite passes (about ninety
seconds: `.venv/bin/python -m pytest -q`), the documents say what the code now does, and the
behaviour has been seen on the development machine's real library, not only in the fixture.
The suite must be run to completion before a commit; a day of scheduler defects went unseen
when it took an hour and nobody waited for it.
