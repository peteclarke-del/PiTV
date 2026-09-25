# PiTV status: what stands between here and end-to-end testing

The open work across PiTV and pitv_content, who owns each item, and how it will be judged
done. Requirements are in [REQUIREMENTS.md](REQUIREMENTS.md), the design in [PLAN.md](PLAN.md),
and the wire between the two applications in [CONTENT_CONTRACT.md](CONTENT_CONTRACT.md); this
page only tracks what is not yet true of them. An item leaves this page when its check has been
run and passed, not when its owner reports it finished.

Last reviewed 2026-09-25, after an outage; the work of 21 to 23 September (about sixty-five
PiTV commits and fifteen in pitv_content) is in the git log and not yet summarised here.

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
| C15 | A cache run starves band collection. Helpings let cache work pre-empt a band within one helping, but a cache run with transcodes runs for hours (on a Pi, longer than a day) and nothing collects until it ends: on 18 September no song was fetched between 18:21 and 20:08 while one run transcoded films. A cache run should work the manifest in slices and let one catalogue helping through between them Landed in pitv_content c35c10b (and 218ea68: interrupted delivery and index jobs come back too; a job stopped at an operator's request stays stopped); its check failed on the night of 18 September: no band helping started in three hours of sliced delivery, the between-slice turn going each time to an index job paired with the hourly cache runs; reworked in pitv_content b16d51d, 42d0235 and a third fix found by watching the queue: a slice's turn goes to a waiting band, a slice started while a band waits only copies, an index paired with a delivery waits for it, and the rest of a job gives way to a band unless a band has just run. A band helping started at 01:01 on 19 September, the first in four hours, and another ran between cache slices from 02:46 to 03:27, taking the fetched songs from 31 to 42. Helpings now get their turns; the check stays open until a whole night passes without pitv_content being restarted under it (five cache runs between 00:19 and 02:46 ended "the admin restarted while it ran") | 2, 3 | With a cache run and band runs both queued, new songs keep arriving while the manifest is delivered |
| C16 | A band helping interrupted by a restart, a signal or a crash is lost: the Rock band's run was killed at a deploy restart and never requeued, and PiTV, having been told the request was accepted, did not ask again, so "Rock Hour" stayed at four songs with nothing intending to fix it. An interrupted helping goes back into the queue with its remaining count Landed in pitv_content c35c10b; awaiting its check | 3 | Restarting the service during a helping leaves that band queued |
| C17 | A cache run honours `cache_max_bytes`. On 18 September a run took a cache capped at 10 GB to 13 GB, on a RAM disk, because it goes by the drive's free space; the manifest carries the cap. Ask make-room for the difference, and stop with the "drive is full" report (pitv_content c9befe5) when PiTV cannot free it Landed in pitv_content 513e9c8 (the whole cache directory is measured at the start of each run and slice, fetched material included). At 03:45 on 19 September the directory held 225 GB against the 270 GB cap, which shows nothing until the cap is pressed; awaiting its check | 2 | With the cap below the manifest's size, the cache directory never exceeds the cap |
| C18 | The listing backs off when MusicBrainz answers 503 (one helping logged 95 retries at the same pace), and a lookup that failed in transport is not remembered as "nothing known" for six weeks; chart page cell markup (`data-sort-value="Jackson"\|`) is stripped from artist names Landed in pitv_content b16d51d, with 0201074 (a chart written as a list was being skipped and the albums table read in its place, so a 1970 band was sent to find Led Zeppelin II as a single; 73 poisoned misses purged) and 6e1d3f8 (a row MusicBrainz calls an album is dropped, and a year with more than three is re-read); awaiting its check | 3 | A helping's log shows no run of 503s, and no miss recorded for a transport error |
| C20 | Lookups rotate across several sources instead of leaning on one. On 18 and 19 September MusicBrainz refused for hours after one bad night and every band helping sat in backoff making nothing. A pool of configured listing sources behind one interface (no key needed: iTunes Search, Deezer, Wikidata, TheAudioDB; with a key entered in the admin: Discogs, Last.fm), each with its own pace, breaker and remembered rest; every lookup goes to the next in turn; one cache for all, recording who said what; years reconciled towards the original release and never later than the chart year; genres through the shared vocabulary. Pete's direction. Landed in pitv_content fb13be4 and 4378645; Discogs and Last.fm wait on Pete registering keys; awaiting its check | 3 | With any one source refusing, a helping still labels and files its songs, and no source's log shows a run of refusals |
| C21 | Series-level acquisition. Every line-up episode is its own search, and demand is 55 to 72 new episodes a day against 3 delivered in a day. Find a series once (a playlist, a channel, an Internet Archive collection), record where it lives against the identity PiTV sends, fetch episodes from there in order without searching again, and take the next few in the same visit. PiTV now lists every fetch for the whole built schedule with its air time (9eb3068), so there are days in hand. Landed in pitv_content aa0a944 and dc15426; awaiting its check. Rerun Century's catalogue of public domain television on the Internet Archive has been passed on as a possible source; Pluto TV, Tubi and The Roku Channel are ruled out (protected streams, no metadata API, terms that forbid fetching) | 4 | A week's placeholders for one series are delivered from one find, ahead of their air times |
| C8 | Inside one job: searches and downloads run ahead of a single analyse-and-encode stage; one or two decode passes instead of four. The decode passes are committed in pitv_content f8e66d2 and not deployed, so that the night of 19 September stays undisturbed for C15 to C21: one decode yields the intervals, the fingerprint frames and the picture box, measured by its owner at 18.5 s against 28 + 20 + 21 s on a fetched music video under load, with identical results on three real videos. Fetching ahead is committed in pitv_content 38edfea, also undeployed: a catalogue run downloads one video while the one in hand is encoded, never two from a provider at once, and the log says "meanwhile fetching" and "fetched ahead but not needed". Both go live together once the night's checks are judged. Delivery requests (series episodes) are not covered; if series delivery is what is slow once C21 has been judged, that is the next gain | 3, 4 | Timed by PiTV against C5's figure |
| C9 | SIGTERM in a job cleans its temporary files, releases the run marker and reports what it had delivered. Landed in pitv_content 832f9f0; to be checked the next time a job is stopped mid-run | 5 | Stopping the service mid-run leaves no `encode-*` files and PiTV receives a report |
| C10 | Catalogue runs take and refresh the running marker and ask PiTV to make room before fetching | 2, 5 | A long band run is visible to PiTV's eviction as running |
| C14 | PiTV's half done on 20 September: the settings push carries the profile's values as `video_profile_values`, the same object as the manifest's `profile`, on a change of screen, after every start and whenever the values change, with the name alone for a pitv_content that refuses the key (as today's does). pitv_content's half, taking the values and dropping its table, is to follow. The screen profile is defined once. pitv_content resolves the profile name PiTV pushes against a table of its own, so the 720p ceiling had to be changed in both repositories (pitv_content d49808a) and a catalogue run would otherwise have gone on fetching 1080p. PiTV should push the profile's values, or pitv_content read them from PiTV, so the two cannot drift | none | Changing a profile value in PiTV alone changes what a catalogue run fetches |
| C23 | The duplicate fingerprint is sensitive to where a fast-cut picture is cut. With the threshold at 10 bits of 64, the same music video cut 0.1 s later moves 15 bits and 0.25 s later moves 20, against about 31 for a different song, so two uploads of one video are often not seen as the same by fingerprint and C3's name matching is what stops them. Slower pictures move far less. Found by pitv_content while reworking the decode. Landed in pitv_content 12d5262, not yet deployed: the threshold stays at 10; each hash is of the picture averaged over the preceding half second and a fingerprint holds four starting points, so a comparison slides by eighths of a second. Its owner measured the same fast-cut file begun up to 0.5 s later at 3.4 bits at worst (8 to 21 before) with a different song at 30, a suite test that fails on the old fingerprint, and the 166 stored old fingerprints still comparing (6.6 at worst, nearest other song 20.7); they are re-made forty a run. It cannot be reproduced from PiTV, since the name rule stops a second upload; closes when deployed and a band helping has run clean on it | none | Two uploads of one fast-cut video, offset by up to half a second, are recognised as duplicates, and two different songs are not |
| C19 | `pitv-content doctor` and `GET /api/doctor` on the loopback API: a read-only report, findings first in sentences (a job failing repeatedly, a provider refusing, sources unreadable, the cache at its cap, stale part-files, how long since a band helping last ran and last made anything, a chart or listing year being distrusted), then the sections. Sources by id and state, never by path; no credentials, token paths or manifest URLs, since it is meant to be pasted. PiTV's `pitv doctor` embeds it. Agreed, after C8. Gap found after the outage of 23 September, when systemd-oomd killed the whole desktop session at 17:32 (another project's editor at 17.4 GB) and pitv_content with it: a SIGKILL leaves a part-encode (447 MB that time) and a download folder behind, and the only sweep removes an `encode-*` file once it is 24 hours old, so for up to a day nothing says it is there. A download folder whose video is no longer wanted is never swept (one, empty, since 18 September). With no job running, both belong to nobody and are to be reported at once with a clean-up action. Done in pitv_content e502a5a (`leftovers` in status, `POST /api/cleanup`, the sweep at one hour for a part-encode and six for a download folder; its test plants both) and on PiTV's side (the doctor's finding, Clean up on the Doctor page, `cleanup` through the proxy; contract section on job status). Live once pitv-dev-content restarts between jobs | none | PiTV's report carries pitv_content's findings, and a part-encode or unwanted download folder left by a killed run is reported on the next start |
| C24 | A band whose searches find nothing is asked for without end. On 25 September the Metal band's helping ran 35 minutes of searches for 1 song of 10, "0 worth a look" on every query at the end; a helping that files anything queues the band's remainder (52 wanted), and PiTV asks for any short band hourly, so the pair can spend a day's queue on one song a helping. A rest needs both sides: a helping that exhausts its queries having filed under some fraction of its count reports so, PiTV stops asking for that band for a configured time, and the doctor names it as searched and not found, with its genres and years and the means to widen them or ask again now. Proposed to Pete by both sessions; needs a contract change | 3 | A band whose searches are exhausted is rested on both sides and shown in the doctor with its remedy |
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
written since 217ba21, against three in the last log before it. A gap found since and closed in
pitv_content 38edfea: the "already filed" rule was asked only when a search was scored, so two
uploads of one song from the same search both passed; it is now asked again before each
download. The check is unchanged.

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

The "needs attention" list (19 September): 653 items to 93. 560 were episodes of dated series
flagged "No year found" at every import, because the note was computed from the episode's own
row; an episode now takes its series' year, as it does when scheduled (7cd8a1d). What remains
was C22 and two films the library has no duration or year for.

Line-ups by type (19 September, PiTV 34c52a0, 674a5d0, 8d5b85d): what a programme is decides its
channel and its genres only describe it. After a rebalance on the development library every
unpinned title sits on a channel of its own type (Toons 197 cartoons; Docs 59 documentaries; the
general channels films, series and, with no sports channel, sport); the ten exceptions are all
pins. Nothing built from tomorrow airs off its own channel. The cartoon channel runs its series
daily (01bed32) and shows 23 hours a day from disk; a delivered file's slot takes its real
length (65ae0b6); the last resort no longer books one remote episode all day (52d4888).

The feel of a day (20 September, PiTV 10d1d6d, 3508b96, 0b923fc, e342c62). Measured on the
general channels, weekday prime time (19:00 to 21:00) was 88 to 100 per cent film, because a day
filled from the morning on spends its few due series by lunchtime. The day's series are now kept
for the peak hours, a film does not run into them, the peak takes series first, and a remote
title is held to its match's certificate (A Bit of Fry and Laurie had been at breakfast). Film
share of weekday prime time is now 49 per cent on One and 37 on Three, with no holding card;
Two (74) and Four (95) are short of material, not of rules: Two has 19 series that may air before
nine and Four 14, against 52 and 47 on One and Three. More titles on those two is the cure.

The cache deadlock (20 September, PiTV de88eab): PiTV counted only its copies against the cap and
pitv_content the whole folder, so with 8 GB fetched PiTV judged that a film fitted and evicted
nothing while pitv_content refused every delivery for room, with 184 GB evictable. make-room
now measures the folder as the cap is defined. Deliveries had been blocked since about 15:40 on
the 19th, so C21's delivery rate has still to be measured.

First reading after the fix, 01:19 on the 20th: pitv_content's cache run delivering with no
errors (3 of 531 items, folder 284.5 GB), 73 episode requests open, none delivered yet, 116 songs.
The measurement for C21 is due around 08:00.

Why episodes did not arrive all week (found by pitv_content at about 02:00 on the 20th): it
sorted the manifest itself, fetches last, behind the transcodes, and since C15 a slice ends at
its first transcode whenever a band helping is waiting and starts again from the top. While one
transcode not about to air was listed, no fetch was ever reached: of tonight's 73 only the 2 due
within three hours came before eight transcodes of thirteen hours. The 7 episodes in 12 hours
were those near-term ones; the searching was never the limit. Fixed in pitv_content ea753d2
(soon, copies, fetches, transcodes; four fetches a slice; "not found yet" remembered for twelve
hours instead of searched again every slice), restarted onto with 12d5262 on PiTV's word that
night. The band helping against C5's figure is to be timed on a later helping, since the first
few re-make forty old fingerprints each.

It was not enough: the first slice on ea753d2 opened with a concert about to air, which is
rightly exempt from every limit, and that one transcode took 76 minutes of a 30 minute slice.
Behind it stood 158 copies not yet in the cache (153 GB, each waiting on make-room after a day
of rebuilds had filled it), so the fetches would still not have been reached. pitv_content
2d6d0b9, restarted onto at 03:28 on the 20th: soon, then the fetches' share (four things looked
for, within a third of the slice and never less than ten minutes, taken even when what is about
to air has used the slice up), then copies, then the transcode. The trade is deliberate and
agreed: a late copy leaves the player on the NAS for that programme, a late fetch leaves a gap.
A third restart followed at 04:38 (pitv_content 56c14a1): the share was kept for an over-long
slice only when a fetch was the very next request, and in a real manifest the rest of what is
about to air lies between; the 03:48 slice transcoded a concert for 35 minutes and ended without
looking for anything. A slice out of time with its share untaken now passes over what lies
between, takes its four fetches within the time box, and ends. Episode delivery is to be read
from 04:38; contract section 6 describes the order.

First result on it (05:41 to 05:51): "Up the Elephant and Round the Castle" episodes 1 and 2,
each under its real title, about four minutes from search to filed. The ten minute share went
on one series because the look-ahead took the next two episodes of it. Decided on PiTV's side:
a look-ahead is for a slice with time in hand, since about ninety series each need their first
episode this week and a second is not needed for a week after; the share's size stays until the
reading. PiTV 61c7a0e files a matched series' delivery under the report's real season, number
and title, as the contract says; it had catalogued that first one as "Episode 1".
pitv_content 19440e8 (restarted onto at 06:04 on the 20th) looks ahead only with what is left of
a slice's share once the requested fetches have had their turn. Songs went from 116 at 01:20 to
167 at 06:00, so band helpings are collecting between slices. Episode delivery under the final
behaviour is to be read from 06:04.

The reading, 07:50 on the 20th: since 04:38 three episodes filed (two requests delivered, 72
still queued, none "not found yet", none failed) against 73 songs since 01:20 (116 to 189). The
searching works and what it takes is right; the limit is turns and share: two cache slices in
three and a quarter hours, ten minutes of each for fetches, a band helping of most of an hour
between. Asked of pitv_content: the share to half the slice and never under fifteen minutes with
the time box as its only limit, and two slice turns to each helping's one while requested
fetches wait. The target is about four first episodes an hour. C5's helping timing is still owed.

Both are live in pitv_content 751ffcc (restarted onto at 08:04 on the 20th). Its sampler for
the night shows the shape plainly: four of five slices opened with a concert being re-encoded
for that evening, 33 to 76 minutes each. That was PiTV's eviction, which went by least recently
used alone and threw an encoded concert out ahead of films that take seconds to copy again;
PiTV 893abbf evicts plain copies first. The one slow fetch of the night was a 44 minute 720p
upload that needed ten minutes to re-encode; SD uploads are copied in about four.

A sixth wrong delivery, excluded in PiTV and left on disk for Pete: "Crackerjack (1955) S01E01"
is almost certainly the 2020 CBBC revival (44 minutes, 720p, titled "Crackerjack!"). An episode
number cannot tell namesakes apart; pitv_content is making a title or a year inside the run
the evidence when a namesake exists, and was asked to take the longer of two sources' runs
(PiTV's match says 1955 with 453 episodes; TVmaze stops at 1958).
That is live in pitv_content ffdaf19 (restarted onto at 08:20 on the 20th): with a namesake, the
upload must carry the episode's title or a year from the matched source's own run; its year
reader had known only 1960 to 1999. The midday reading is on this code from 08:20.

From pitv_content's own reports, 08:20 to 11:40 on the 20th: 8 episodes delivered, every one
under its real title (How It's Made 1, Classic Albums 1, Forbidden History 2, Dinosaur
Apocalypse 2, Ancient Aliens 2, How It Works 1 and 3 with 2 downloading), 5 where no upload was
found at all and 3 where none said which episode it was; 16 episodes on disk against 6
overnight, and 216 songs. About two and a half an hour, with the cache full and every copy
waiting on make-room. Agreed the same day: a search that finds nothing is also "not found
yet" and costs no attempt, and the not-found memory backs off (twelve hours, doubling to a
week) so that a title that never appears is not searched for twice a day for ever.

PiTV's own reading for the same stretch, 08:20 to 12:20: 8 episodes filed, 7 requests delivered, 2
"not found yet" (Planet Earth 1, How It's Made 2), 1 with no upload found, 77 queued; 17 episodes
and 226 songs on disk. Two an hour against a target of about four, with nothing filed after
11:42; pitv_content has been asked what those slices did and how long make-room takes it, since
the cache is at its cap and every copy waits on an eviction. It restarts onto 8173832 (the
back-off, and `encoded` in the index) after this reading.

What the reading came to (pitv_content's account, 20 September): the quiet from 11:42 was a full
index of every source, twenty-six minutes in which nothing is delivered, requeued by a restart;
and the real ceiling is that each fetched episode is decoded whole for the duplicate fingerprint,
about a minute for every four or five of programme (ten minutes to file a 44 minute episode that
downloaded in 37 seconds). Agreed: the fingerprint of a long programme is taken over a window
after the trim, proven against the whole-file fingerprints already stored and against two
different episodes of one series, with the threshold untouched and music left alone; and a full
index nobody asked for runs only between 01:00 and 06:00. PiTV's make-room was cleared of
suspicion: the waits scale with the copy that follows it.

The windowed fingerprint went live at 13:33 on the 20th (pitv_content dea42a2; on the twenty
episodes then on disk each window matched its own file at 0.0 and the nearest other episode,
same series included, was 25.6 bits against a threshold of 10). PiTV's reading, 13:34 to 16:41: 15
episodes filed, about five an hour against the target of four and two that morning; 38 on disk
against 6 the night before; 74 requests still queued, of which 6 found no upload that said which
episode it was and 4 found nothing. With time in hand the look-ahead is running again (Forbidden
History 1 to 5, Planet Dinosaur 1 to 3). What arrives is mostly documentary; the 1980s British
series on One and Three are the ones the search finds least. Songs stood at 226 from 12:20 to
16:41, which wants asking about. C21 is met on this machine.

Idents (PiTV 84b8a0f): seven made in the development data folder, and an idents source added to
pitv_content through its API (type ident, the new location `local`, pitv_content ad1879e). They
appear in the schedule once that source is indexed.

They were indexed at 16:48 on the 20th, once pitv_content stopped a named-source index being
starved by delivery (its 8a2847a); all seven arrived tied to their channels. On the development
machine every programme channel's pattern now asks for one (before each programme on the two
without adverts, after the break on the rest), and the next day carries 11 to 54 a channel. A
rebuild also now keeps remote programmes already promised inside the lead window (PiTV c337e7d),
which is what had left each today and tomorrow to fill from disk alone.

Moved to the NAS on the evening of the 20th, at Pete's word: the seven films sit flat in the
`idents` folder of the adverts share, each checked against its original by SHA-256, and the
development copies are gone. pitv_content's `idents` source is now `nas` with that folder as its
root, nested inside the adverts source, which leaves it out of its own scan (its f273648). One
index job over both sources gave 608 adverts and 7 idents, none counted twice. On import PiTV
tied each film to its channel by the name its title begins with (cc03656); the rows for the old
`ch<number>/` paths are marked missing and the editor no longer lists them. The rebuilt week has
97 to 203 ident slots on each of the six channels whose pattern asks for one, every one the
channel's own film, and the manifest lists them for copying. `pitv idents --flat --out <folder>`
writes this layout for the next regeneration (19732b7).

Two readings from the move. The index job waited 21 minutes behind a band helping of ten songs
and then ran in 12 seconds: pitv_content's log puts a song at 46 to 100 seconds end to end
(search, download, one analysis pass, encode, with the next download running meanwhile), so a
helping is fifteen to twenty minutes, which is the figure to hold against C5's. And
`/api/status` said `idle` throughout that helping, because the status file was written by
delivery runs alone; pitv_content now takes `state` from its queue (its c3c52b6, deployed once
this move was checked). `pitv doctor` was not misled, since its idle finding reads `active_job`
and `queued_by_mode`, but it prints `state`.

The web streams re-encoded every programme to the screen's size, which cost 8.5 seconds of
processor for fifteen seconds of video where copying costs 0.2, and on a machine at load 28 the
encoder could not hold real time: a 921 second packaging run took over 993, so the stream fell
behind its own schedule. A source a browser already accepts now goes through untouched, decided
by asking the file, since the catalogue records neither pixel format nor field order and ten bit
HEVC sits in the cache beside plain H.264. Of forty cache files probed, 31 were H.264 in eight
bit 4:2:0 and copyable; the nine HEVC files, five of them ten bit, are still re-encoded. The
picture keeps the source's own size, because a browser scales it to the window and the
television has its own player. pitv_content was the other half of the contention and now runs
its jobs five points of niceness below the player (its d4c96a3).

Transcoding is not what holds delivery back, which is worth recording because it looks as though
it might be. Of 602 manifest items, 513 are plain copies, 80 are remote fetches and 9 are
transcodes. The NAS fallback and the thin variety come from the copies and the fetches, not from
encoding.

What does hold it back is the cache cap, found on the night of the 20th after pitv_content had
ruled out its own half with evidence: delivery is not behind (of 128 undelivered items exactly
one was due within three hours) and the two sides' books agree exactly (392 files claimed
cached, 392 present). The cache folder is 267 GB against a cap of 270, and the 17 TB drive
holding it is 100 per cent used with 13 GB free, almost all of it material that is not PiTV's.
A day of schedule is 501 distinct files and 222 GB; two days is 343 GB. So the cap holds about
one day and the rest is evicted before its slot comes round, which is why those programmes play
from the NAS. pitv_content priced it from its own reports: of 434 files delivered in a day, 165
were evicted again, 166.5 GiB copied onto the drive and thrown away, and it will repeat every
day the cap stands. That figure is nearly all NAS copies, which are cheap to make again and are
meant to come and go; it is the size of the churn, not of anything lost.

Settled in the early hours of the 21st. The drive had 14 GB free when this was diagnosed and
2.0 TB by 01:50; by 04:00 Pete had freed over ten terabytes more, and it stands at 31 per cent
used with 11 TB available. He set the cap to 900 GiB, sized for the 1 TB SSD the target Pi will
have. Both findings have cleared: the cap holds 3.9 days of schedule where it held 1.2, and 668
GiB is left for fetched episodes against the 25 GiB they occupy. pitv_content expects its churn
figure to fall to near nothing and will say so if it does not, which would mean something else
is evicting. What is left is transient: a little over half of the next day is in the cache and
the rest is being copied into the room that has just appeared.

Nothing is at fault. `POST /api/content/make-room` protects the current manifest before evicting
and `evict_fetched` spares anything still scheduled ahead; what goes is NAS copies for airings
beyond the one day the manifest covers, which the rules permit because a NAS copy is cheap to
make again. The cap is what is wrong, and raising it is Pete's decision because it needs about
180 GB freed on a drive holding his own material.

`pitv doctor` now reports this by itself rather than needing an evening of measurement: it
totals the distinct files the next 24 hours needs and states the cap as days of schedule, with
what it would want and what the drive has spare. The finding reads "The cache holds only 1.2
days of the schedule (270 GiB against 223 GiB a day), so copies are evicted before their slot
comes round and those programmes play from the NAS. It wants about 335 GiB; the drive has 12 GiB
spare." A delivery report could never have shown it, because a run only ever sees what is
missing now.

Sizes are GiB throughout, the unit `cache_max_gb` is set in and `df -h` prints, and pitv_content
prints GiB too from its ddfcaf3 onwards. This was worth stating because the two applications
appeared to disagree about the cap: PiTV read 270 and pitv_content 289.9, and they are the same
289,910,292,480 bytes in the two conventions. Nothing was misconfigured, but Pete would have been
freeing disk against whichever figure he read last. The mix caught three separate figures in one
evening, including one quoted to him, which is the argument for one unit rather than for care.

The doctor also watches what the cache is meant to grow by. Fetched episodes are kept so a later
airing costs nothing, and a cap that holds the schedule while leaving less room than those
episodes already occupy is about to start evicting them. Tonight it leaves 47 GiB against 23 GiB
kept, so the finding is quiet; at the present rate of fetching pitv_content puts it at days
rather than weeks before it is not.

Where an ident belongs, from Pete on the 20th: it always comes after a show, and a break carries
only one. The ident closes the programme that has ended and hands over to the break, so it heads
the break and the adverts follow it. Three things had to change. Each channel's pattern now reads
`ident, show` or `ident, ad, ad, show`, which puts the break before the programme it leads into
so that a walk resuming after a programme already on air still places it; the older `show, ident`
lost the ident at exactly that junction, which is what Pete saw on PiTV One. Padding a gap now
offers the ident first rather than after the adverts it could fit. And `Walk.ident_due` replaces
the bare one-per-break test everywhere an ident can be placed: an ident is placed only where the
slot just emitted is a programme, which is what stopped idents appearing between adverts, after
filler and beside another ident. The first fully rebuilt day carries 90 idents, every one
directly after a programme.

PiTV Toons ran two programmes with nothing between them, which was its configured pattern
(`show, show, ad, ad, ident`) doing as it was told. It now breaks after every programme like the
other advert channels.

Band helpings are now taken back when they are no longer wanted. PiTV asked hourly, pitv_content
served a band about hourly, and the two sat exactly at equilibrium: any hour delivery took more
of put a helping behind and it never caught up. Eight were found queued in the early hours of the
21st, the oldest waiting five hours, for bands that `band_needs` by then reported as fully
stocked, and they would have fetched music nobody was waiting for ahead of episodes that slots
were waiting for. pitv_content read the depth as its queue being too slow and offered to give
bands more turns, which would have served the stale requests sooner. A queue depth says how much
is waiting and nothing about whether it is still wanted, and only the side that raised the work
can say. PiTV now records the job it is handed (`band.fetch_job_id`), does not ask again while
that job is alive, and cancels it when the band has filled (`POST /api/cancel`, pitv_content
39cbc0f lists queued jobs however old so it can be found). The ratio was left alone.

The suite has only ever been run in one order, so a test that passes because an earlier one left
state behind would never have been caught. Two were found by accident on the night of the 20th:
a readiness test that needed the test before it to have scheduled remote titles, and a test of
the public player state that asserted nothing at all unless an earlier test had set an admin
password. Running each file with its tests reversed is a cheap check that would have caught both
and needs no new dependency:

    ids=$(.venv/bin/python -m pytest -q -p no:cacheprovider --collect-only tests/test_x.py | grep '^tests/' | tac)
    .venv/bin/python -m pytest -q -p no:cacheprovider $ids

Settled on 21 September, after pitv_content's own shuffler found a real fault in its `/api/system`
on its second run. Pete chose to add one here too. `pytest-randomly` is a dev dependency and every
run is in a different order, with the seed printed at the top so a failure is reproducible.

Shuffling found eleven order-dependent tests where the reverse-order sweep had found six, and
fixing them properly took a dozen more runs, because each fix exposed the next. They were three
faults wearing eleven faces. Most of `test_api` failed on 401s that had nothing to do with what
they were testing: the module shares one client, one test sets a password and another rotates the
signing secret, so whoever ran after them lost their session. The client now has a password from
the start, which is what makes "a stranger is refused" mean anything, and an autouse fixture logs
back in when a neighbour has invalidated the session; the two tests about an installation nobody
has set up get a service of their own. In `test_lineup` a helper left `nas_only` off for everyone
and several tests counted the whole line-up or the whole week, so they were measuring their
neighbours' work: each now states what it needs, scopes its query to its own entry, or takes a
library of its own where the thing it asserts is an invariant over the whole catalogue. And
`test_episodes_in_order_per_show` read a week that other tests rebuild, so it rebuilds the
canonical week first.

The suite is green in its declaration order and under seeds 42, 55, 314, 777, 1618, 2026, 2718,
4242, 8080, 9001, 9999 and 31337.

From Pete watching over the web interface, late on the 20th: the end of a programme came round
again for about ten seconds, then the ident arrived in pieces, its end first and sometimes its
start, jerky and never whole. The cause was in the streams, not the television. A packaging run
writes its slot faster than the slot plays, because the opening seconds are read flat out and
`-t` stops ffmpeg once it has written the media rather than when the clock reaches the end of
it. The supervisor started the next item the moment the process exited, so it packaged the same
slot again from a few seconds earlier, and again, halving what was left each time: the log shows
a fifteen second ident packaged six times. Each pass appended to the live playlist while
`delete_segments` took segments the viewer had not fetched yet, which is what made it jerk.

The next item now waits for the media already written to be played out, a run is never asked for
more than the file has left to give (a file that ends before its slot leaves the continuity card
for the rest, as the television does), and the opening burst is kept for the cold start it was
written for, since reading ahead mid-stream only spends the margin the viewer has.

From Pete watching the documentary channel on the evening of the 20th. Two idents ran in one
break: padding a gap could add two of its own beside the pattern's, and the small-hours replay
closed two breaks up wherever it left a programme out (six idents once). A break now carries one
ident at most, day and night (PiTV f4b2e4e, 8d9ef13); the rebuilt week has none with two. And
"Classic Albums" episode 1 was a Russian dub (source title "... [RUS]", dm:x1rpi08): excluded in
PiTV, its request re-opened, and pitv_content asked for a rule that a programme is taken only in
the owner's language (titles that mark a dub or another language refused, the provider's
language field and English audio format preferred, a non-English only stream refused; music
exempt).

Pete's standing rule the same evening: pitv_content is never idle unless the cache is full and
the foreseeable schedule's needs are met. Its job history showed no gap between jobs, but PiTV
itself had stopped music collection: band material was asked for only until a band could get
through its repeat gap, only from 01:00 to 05:00, six hours apart, so no song was filed from
midday with five bands wanting. It is now asked for at any hour, an hour apart, until a band
could run seven days without a repeat, and `pitv doctor` reports pitv_content idle with work
outstanding (PiTV f4b2e4e).

pitv_content's language rule is live (cd8efce, 19:27 on the 20th) and in contract section 3; the
dub is set aside and its source rejected. On never-idle it has no timer of its own, but an empty
queue does not start work by itself: with the manifest delivered no look-ahead is taken, since
one only happens inside a delivery slice. Asked for next: a queue that starts a slice whenever
anything is left to fetch, with a floor so an empty manifest cannot make it spin, and an
`idle_reason` in its status for `pitv doctor` to read.

The spread of a week (19 September, PiTV dcb86e5, ab299e9, 5def1e0), from Pete finding the same
remote sport episode twice running on a Wednesday morning on PiTV One. Three causes: the last
resort waived "never the same series back to back" for remote titles; placement ignored a
channel's decades, so One (1960s to 1990s) held 23 series from 2000 on that it could never air
and ran short; and with one episode a week every series came due on the same days. Placement
now applies the decades, each series keeps to its own day of the week, and after a rebalance and
rebuild there is no back-to-back repeat on any channel and no sport on a weekday morning on the
general channels. One shows 0, 4, 1, 3, 5 and 7 library series a day over the coming week (it
was 0, 0, 3, 0, 1, 15) and should level further as series settle onto their days. The library
itself is mostly from 2000 on (about 700 of 1100 films, 115 of 195 series), which the era
weights hold at 0.05: the period feel of the general channels rests on the titles Pete adds.

Borrowing by type (19 September, PiTV 57081ce): on the development library the general channels
carry cartoons from the cartoon channel's shelf on Sunday morning and in the weekday children's
hour (One 12 slots, Two 15, Three 21 on 20 September; none outside those dayparts), and the
cartoon channel's own day is unchanged. Rick and Morty arrived from its NFO as PG and, being
animated, flagged for children; it is overridden to 15 and not children's, which is the kind
of thing to look for when a borrowed cartoon turns up somewhere it should not.

Still to measure after pitv_content's restarts of 19 September (20319c7 at 15:18, then 47abe13 at
15:38, which takes an upload for a matched series only when its title says it is the episode wanted):
a band helping against C5's under-a-minute figure, and series delivery over an evening with
every request now asking from episode 1 (C21). Nothing had been delivered between 15:18 and
15:37.

P6, remote series asked for in order and never past their end (19 September, PiTV 45f895c,
c672623, 5949fd9, 99150cb; pitv_content 20319c7, deployed 15:18): open requests went from a highest
episode of 21 to 8 with every series asking from episode 1, the run length is known for 53 of 67
remote series, and pitv_content refuses an episode its source does not list. The five files fetched
before these fixes are dealt with (20 September): Pete approved it in the pitv_content session,
which renamed three to their real numbers (Science Britannica is now complete, 3 of 3) and set
the Wales touring video aside in `pitv-set-aside` beside the cache; the How It Works news clip
had already been evicted. PiTV imported the renamed index, its request records were corrected
on the development database (backup `.dev/pitv-before-request-repair-*.db`), and first-season
episodes on disk now count as asked for (20f781d), so a rename needs no repair again. The first
two deliveries under pitv_content's episode-evidence rule (Space 1, Making the Most of the Micro
1) were checked against their NFO source titles and are the episodes they claim to be.

C22, the index passes on what the NFO knows (pitv_content 5bdcee5, PiTV fe225c9): `certificate` is
the NFO's raw text, shows and items carry `ids`, and the lookup resolves by identifier. Verified
at 04:38 on 19 September: the full index published with ids on 1291 of 1294 films and 187 of 197
shows, PiTV imported the same counts, the raw text alone rated 5 films, and one forced online
check identified 90 of 97 titles by identifier. The list went from 93 to 43. The 42 films
still unrated were identified, but TMDb holds no UK or US certificate for them (television
films, documentaries, live shows; `NR` for some). They are scheduled as 15, after the
watershed, until the owner sets one in the admin, which is what the note is for. No OMDb key is
set on the development machine, so only TMDb was asked; with one, OMDb fills a certificate TMDb
lacks (pitv_content 5b9a137 stops TMDb's `NR` blocking that). Pete entered an OMDb key on 19
September; a forced check then rated five more films (43 to 38 on the list). OMDb has no
rating for most of the rest either, so the 37 still unrated are for the owner's hand, less any
that 5b9a137 frees when it is deployed.

pitv_content 265cbda is committed and running on this machine but not pushed: the GitHub token
here has expired and Pete has to sign in again.

The outage of 23 to 25 September. At 17:32 on the 23rd systemd-oomd killed Pete's whole desktop
session because another project's editor had reached 17.4 GB; PiTV and pitv_content held 180 MB
between them and went with it, as did the gvfs mounts of every NAS share. Nothing ran for a day
and a half. Restarted at 02:29 on the 25th as `systemd-run --user` units outside the editor's
cgroup. What the restart showed:

- pitv_content recovered as C16 says: the killed slice was marked failed and continued with
  `tries=1`, and its first slice swept the part-encode and both download folders.
- With the shares gone, two index jobs published incomplete (887 items against about 17,400)
  and PiTV imported one as additions only, retiring nothing, as the contract requires. Nothing
  on PiTV's side said the NAS was away: `pitv doctor` now names every enabled source
  pitv_content cannot read, with the remedy, from `GET /api/sources`, whose readability is a live
  check. Pete reconnected the shares at about 02:33.
- Readiness at 02:41 replaced eight remote episodes due that morning that had not been fetched
  during the outage, which is the designed behaviour.
- A catalogue run from 02:32 stayed "running" for good: `import_and_place` guarded the import
  but not the mirror and refill after it, so an exception there, most likely a locked database,
  left no trace. Every step is now inside the guard.
- The full index waited from 02:29 to 04:52 behind one delivery slice that overran its 22 minute
  share by more than an hour: a series' further episodes ride the visit that found the first,
  and a visit is exempt from the share's clock, so one ARTE Concert visit ran from 03:52 until it
  was cancelled at 04:52, by then fetching an 11 h 55 min restream as an episode. Cancelled
  through `POST /api/cancel`, which SIGTERMs the job alone; it tidied up and reported (C9 seen
  working). The index then published complete, 17,424 items, and PiTV's import at 05:02 found
  none new and none missing. Three faults are with pitv_content and Pete: a visit has no bound,
  an episode has no length ceiling relative to its series, and splitting a long recording
  numbers its parts without regard to the series, so ARTE Concert has two S01E02 in PiTV
  (media 17624 and 17625) and their order is arbitrary until it is fixed.
- PiTV withdrew every band helping it asked for, about ten minutes after asking, from 21
  September 02:24 (bfffc0e) until b100bc9 on the 25th: `band_needs` leaves out a band asked for
  within `band_fetch_gap_hours`, and "is this helping still wanted" was put to that list. 125
  withdrawals are in the logs from 22 September 23:00, six to eight an hour. No helping PiTV
  asked for in those four days can have run, which bears directly on P4 and on every reading of
  C15 since. With it fixed, the first real reading of C15 (pitv_content, 06:30 on the 25th) has
  band helpings and delivery slices alternating, each helping waiting one slice. A second fault
  showed at once: when the neediest band already had a helping queued the pass asked for nothing
  and stopped, so the others went unasked (06:16, "0 bands; 5 will retry"). Fixed in d9147cd.
- The doctor's "idle with work outstanding" finding never counted short bands (it iterated a
  dict as a list); fixed in 00ca9b6.
- The test suite's web app ran its keeper, which started a real `pitv play` on the desktop
  against the default data folder; one outlived a suite run. Fixed in a30a1ca.
- pitv-dev-content now runs with `KillMode=process`, as on the Pi, so restarting it adopts a
  running job. The first restart onto it could not, and ended slice 41db six minutes in.
- P11 closed the same evening: a rebuild kept a gap walled in by kept slots and carded it
  (Toons, 21:33); a rebuild now starts at the first gap in its day. Its test fails on the old
  code. Also that evening: the programme after a held last frame loaded paused (PiTV Tube,
  jerky, no sound until a channel change), because keep-open's pause outlived the file; every
  load now sets the pause. And remote repeats had ignored the lead window (1e9bc55).
- The player's mpv closed at 02:34:50, two seconds after a click in its window and fifteen after
  a burst of eleven channel loads in one second; the player exited as designed and the web
  service's keeper started another twenty seconds later.

## Open in PiTV

| # | Item | Done when |
|---|---|---|
| P4 | The music channel's bands are short: 42 fetched songs on 19 September, 18 bands short over the next two days and 1106 minutes of holding card, by `pitv doctor`. The bands hold their configured times and show their own card, as configured; the cure is supply (C15, C18, C20) | The doctor reports no band short over two days |
| P5 | The documentary channel leans on material that has not arrived: after the line-ups were put right it shows about ten hours a day from disk and waits on remote episodes for the other thirteen. Until they arrive the readiness check substitutes from a small shelf. The cure is supply (C21) and more documentary titles, not scheduling | The documentary channel's day is mostly material on disk |
| P8 | A way in for series the search cannot settle. Old series with modern namesakes (Crackerjack) will now mostly end "not found yet", which is honest but leaves them unfetched. The owner should be able to give a line-up entry a source of its own (a playlist, a channel, an Internet Archive collection) that pitv_content takes episodes from in order; today only a single advert or music video can carry a `ref`. Needs a contract change agreed with pitv_content | A series given a playlist in the admin is fetched from it, episode by episode |
| P9 | PARKED by Pete on 20 September until the application as it stands is working to his satisfaction. A split system (Pete, 20 September): stations, content workers and thin receivers that only play a channel's stream. Planned in [SPLIT_PLAN.md](SPLIT_PLAN.md) before any code; the first step changes none: mpv on a Pi Zero 2 W against the existing `/channel/<n>.m3u8`, measuring playback over the house Wi-Fi and the time a channel change takes | Pete's decisions at the end of the plan are made on the measurements from its steps 0 and 1 |
| P7 | Films crossing channels. Series are now borrowed by type (below); films are not, and need not be until a film channel exists to claim them from the general channels | With a film channel, a general channel that lists films under "also carries" shows them in its film dayparts |
| P1 | Time a band run and a cache run as each of C1 to C8 lands, import, rebuild, and record the result here | Figures recorded against each item above |
| P10 | SQLite write contention. A catalogue import holds the write lock long enough (imports ran 90 to 250 seconds on 23 September) that others exceed the 30 second busy timeout: a schedule run failed at 14:43 on the 23rd "after 8 channel-days: database is locked", the player could not close history entries, and a `pitv catalogue` started from the command line at 02:32 on the 25th failed while opening the database | No run of any kind ends "database is locked" over a day with imports, schedule runs and the player all active |
| P3 | Hardware verification on a Raspberry Pi 4: hardware decode of copied files, the cache drive, encode times with the Pi's presets, the player keeper under systemd | REQUIREMENTS.md rows marked "by hand" checked on the device |

## How a change is accepted

A change to either application is accepted when the full PiTV suite passes (about ninety
seconds: `.venv/bin/python -m pytest -q`), the documents say what the code now does, and the
behaviour has been seen on the development machine's real library, not only in the fixture.
The suite must be run to completion before a commit; a day of scheduler defects went unseen
when it took an hour and nobody waited for it.
