# PiTV status: what stands between here and end-to-end testing

The open work across PiTV and pitv_content, who owns each item, and how it will be judged
done. Requirements are in [REQUIREMENTS.md](REQUIREMENTS.md), the design in [PLAN.md](PLAN.md),
and the wire between the two applications in [CONTENT_CONTRACT.md](CONTENT_CONTRACT.md); this
page only tracks what is not yet true of them. An item leaves this page when its check has been
run and passed, not when its owner reports it finished.

Last reviewed 2026-09-20.

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
| C14 | The screen profile is defined once. pitv_content resolves the profile name PiTV pushes against a table of its own, so the 720p ceiling had to be changed in both repositories (pitv_content d49808a) and a catalogue run would otherwise have gone on fetching 1080p. PiTV should push the profile's values, or pitv_content read them from PiTV, so the two cannot drift | none | Changing a profile value in PiTV alone changes what a catalogue run fetches |
| C23 | The duplicate fingerprint is sensitive to where a fast-cut picture is cut. With the threshold at 10 bits of 64, the same music video cut 0.1 s later moves 15 bits and 0.25 s later moves 20, against about 31 for a different song, so two uploads of one video are often not seen as the same by fingerprint and C3's name matching is what stops them. Slower pictures move far less. Found by pitv_content while reworking the decode. Landed in pitv_content 12d5262, not yet deployed: the threshold stays at 10; each hash is of the picture averaged over the preceding half second and a fingerprint holds four starting points, so a comparison slides by eighths of a second. Its owner measured the same fast-cut file begun up to 0.5 s later at 3.4 bits at worst (8 to 21 before) with a different song at 30, a suite test that fails on the old fingerprint, and the 166 stored old fingerprints still comparing (6.6 at worst, nearest other song 20.7); they are re-made forty a run. It cannot be reproduced from PiTV, since the name rule stops a second upload; closes when deployed and a band helping has run clean on it | none | Two uploads of one fast-cut video, offset by up to half a second, are recognised as duplicates, and two different songs are not |
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

## Open in PiTV

| # | Item | Done when |
|---|---|---|
| P4 | The music channel's bands are short: 42 fetched songs on 19 September, 18 bands short over the next two days and 1106 minutes of holding card, by `pitv doctor`. The bands hold their configured times and show their own card, as configured; the cure is supply (C15, C18, C20) | The doctor reports no band short over two days |
| P5 | The documentary channel leans on material that has not arrived: after the line-ups were put right it shows about ten hours a day from disk and waits on remote episodes for the other thirteen. Until they arrive the readiness check substitutes from a small shelf. The cure is supply (C21) and more documentary titles, not scheduling | The documentary channel's day is mostly material on disk |
| P8 | A way in for series the search cannot settle. Old series with modern namesakes (Crackerjack) will now mostly end "not found yet", which is honest but leaves them unfetched. The owner should be able to give a line-up entry a source of its own (a playlist, a channel, an Internet Archive collection) that pitv_content takes episodes from in order; today only a single advert or music video can carry a `ref`. Needs a contract change agreed with pitv_content | A series given a playlist in the admin is fetched from it, episode by episode |
| P9 | A split system (Pete, 20 September): stations, content workers and thin receivers that only play a channel's stream. Planned in [SPLIT_PLAN.md](SPLIT_PLAN.md) before any code; the first step changes none: mpv on a Pi Zero 2 W against the existing `/channel/<n>.m3u8`, measuring playback over the house Wi-Fi and the time a channel change takes | Pete's decisions at the end of the plan are made on the measurements from its steps 0 and 1 |
| P7 | Films crossing channels. Series are now borrowed by type (below); films are not, and need not be until a film channel exists to claim them from the general channels | With a film channel, a general channel that lists films under "also carries" shows them in its film dayparts |
| P1 | Time a band run and a cache run as each of C1 to C8 lands, import, rebuild, and record the result here | Figures recorded against each item above |
| P3 | Hardware verification on a Raspberry Pi 4: hardware decode of copied files, the cache drive, encode times with the Pi's presets, the player keeper under systemd | REQUIREMENTS.md rows marked "by hand" checked on the device |

## How a change is accepted

A change to either application is accepted when the full PiTV suite passes (about ninety
seconds: `.venv/bin/python -m pytest -q`), the documents say what the code now does, and the
behaviour has been seen on the development machine's real library, not only in the fixture.
The suite must be run to completion before a commit; a day of scheduler defects went unseen
when it took an hour and nobody waited for it.
