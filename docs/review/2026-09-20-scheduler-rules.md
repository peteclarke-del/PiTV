# Scheduler rules: where each is decided (review of 20 September 2026)

Part of the review Pete asked for on 20 September: the scheduler against its rules, with the aim
of bringing the rules into as few files as is sensible. This part is the evidence: every rule,
where its decision is made, what is duplicated or hardcoded, what is dead, and the order in
which the consolidation is being carried out. Line numbers are as of commit 5001941 and will
drift as the steps land; the findings in section 4 are tracked to closure in
[the review summary](2026-09-20-review.md).

Read-only audit. Nothing was edited and no git command that changes state was run. Paths are relative to `/home/pclarke/ownCloud/Projects/Personal Projects/Raspberry Pi/PiTV`. Line numbers are as of commit 5001941.

Files read in full:
- `pitv/scheduler/{policy,rules,select,bands,runs,overnight,build,library,horizon,slots,__init__,listing}.py`
- `pitv/channel_profiles.py`, `pitv/genres.py`, `pitv/lineup.py`, `pitv/settings_schema.py`
- `pitv/db.py`: the channels schema, lines 280 to 490, and the seeding helpers
- `pitv/wanted.py`: the band top-up section, lines 45 to 185
- `docs/PLAN.md` sections 4 to 4.10, and `docs/REQUIREMENTS.md`
- `tests/test_scheduler_policy.py` in full; the other two test files were checked by grep for which internals they touch

The most useful findings first:
- The relax ladder has no single definition. Its meaning is spread over about 20 branch conditions in `select.py`, `policy.py:136` and `build.py:384`. The docstring at `select.py:200-201` describes two of them.
- Bands, free time on band channels and anchors never call `allowed_at`, so they apply no watershed or kids cutoff (`allowed_at` is called only at `select.py:116,126,252`). PLAN 4.7 says certificates override everything.
- `_series_kept_for_peak` (`select.py:89-131`) re-implements candidate eligibility and has drifted. It counts remote titles on a NAS-only channel and resting series that can never be placed. That makes a channel look well supplied, which is the failure its own docstring describes.
- `weekend_kids_breakfast` cannot fire with any shipped configuration. It keys on a daypart named "Breakfast" (`select.py:225`), and no weekend table has one.
- `external_weight` below 1.0 is silently clamped to 1.0 (`policy.py:102`), while the admin offers 0 to 2 (`settings_schema.py:229-230`).
- `wanted.band_needs` re-implements `SchedulerPolicy.band_limits` inheritance by hand (`wanted.py:99,103-105,111-113`). `guide.py:63` reads only the global band item length.
- Stale comments still describe the old behaviour where a short band gives its time back to the channel (`build.py:308-310`, `bands.py:250-252`). That contradicts `_fill_band` and the configuration-is-authority rule.

---

## 1. RULE INVENTORY

"Decision" is where the yes/no or the weight is computed. "Also at" lists copies, or partial re-implementations, of the same logic.

### 1a. Eligibility

| # | Rule | Decision | Parameters | Also at |
|---|---|---|---|---|
| E1 | A certificate may not start before its watershed. Films and TV have separate tables, and a 00:00 entry means unrestricted. | `rules.py:151 allowed_at`, `rules.py:136 cert_earliest_minutes` | settings `watershed`, `tv_watershed`, `day_start` | Called only from `select.py:116,126,252`. Not applied to bands (`bands.py`), free time (`bands.py:374`), anchors (`build.py:246-259`) or manual inserts. |
| E2 | A missing certificate is assumed to be 15 for a film and PG for TV. | `rules.py:127 effective_cert` | settings `unknown_movie_certificate`, `unknown_tv_certificate`; literal fallbacks at `rules.py:132-133` | none |
| E3 | Certificate spellings (US ratings, `UK:PG`, lists) normalise to BBFC, with a British entry preferred. | `rules.py:110 normalise_cert`, table at `rules.py:16-21` | module constant | Used by `lineup.py:201,366`, `catalogue.py`, `content.py`, `admin.py`. Single implementation. |
| E4 | Removed. A remote title tagged Adult was forced to 18 when unrated, or to 15 when its match rated it lower. The rating alone now decides when a title may air; the genre only describes it. | `library.py` | n/a | n/a |
| E5 | Children's TV may not start at or after the kids cutoff. Films are exempt, and a channel flag turns the rule off. | `rules.py:166-167` | setting `kids_cutoff` (literal "21:00" at `rules.py:166`), channel `kids_any_time` | `kids_rule = not channel.get("kids_any_time")` is computed twice: `select.py:109` and `select.py:224`. |
| E6 | An item is children's when it is flagged so or carries a children's genre. | `rules.py:145 is_kids` using `genres.py:107 is_childrens`, list at `genres.py:53` | `genres.CHILDRENS` | `library.py:102,121,217`, `catalogue.py:249,508,540`, `lineup.py:310`, `admin.py:1208`. `allowed_at` recomputes it (`rules.py:167`) while the selector reads the cached `item["kids"]` (`select.py:116,125,256,321,385`). `lineup.generate` hardcodes films as `kids: 0` (`lineup.py:238`). |
| E7 | A channel plays only its decades. A series that ran into a decade counts, and an unknown year is allowed unless the channel is strict. | `rules.py:179 in_decades`, called at `select.py:239` | channel `decades` (JSON) | `build.py:474` (band pool), `lineup.py:106-107` (placement, without the strict flag), `bands.py:62-72 Band.dated` (its own decade arithmetic), `lineup.py:471,482` (facets). The decades column is parsed twice: `select.py:183-190` and `lineup.py:106`. |
| E8 | A strict channel takes only items with both a genre and a year. | `select.py:218,237-238` | channel `strict_matching` | The same expression is at `build.py:252` (anchors) and `bands.py:222` (band items). Band override at `bands.py:263`. Not applied in `lineup.channel_fit`. |
| E9 | A title the owner pinned to a channel is exempt from strict, decade and era rejection, with a floor weight. | `select.py:236,239,242-243` | `lineup.pinned` via `library.py:77-80,105,115,122`; floor `max(unknown_year_weight, 0.2)` | `build.py:252` uses `show.pinned` for anchors. |
| E10 | An item whose era weight is 0 never airs. An unknown year airs at `unknown_year_weight`. | `select.py:241-245` via `rules.py:96 era_weight_spans` | settings `era_weights`, `unknown_year_weight`; channel `era_weights` | Spans are parsed twice: `select.py:56` and `library.py:49`. Channel fallback at `select.py:154-161`. |
| E11 | A programme must fit the gap plus slack. Slack is zero before a fixed slot, and the tolerance or the time to next day start at closedown. | `select.py:247`; slack at `build.py:374-378` | setting `duration_tolerance_minutes` | `build.py:499-505 _overrun` is the band equivalent. |
| E12 | A short episode is not placed where its run could not reach run length before a fixed boundary. | `select.py:249-251` | `short_episode_minutes`, `short_episode_run_minutes` (channel, then setting) via `policy.py:83` | none |
| E13 | Season 0 specials, missing and excluded rows, and rows with no duration are never candidates. | `library.py:33 USABLE`, `db.py:335 LIVE` | module constants | Reused by `wanted.py:20,169`. `lineup.generate` writes its own variant: `lineup.py:225` (`excluded = 0 AND missing = 0`) and `lineup.py:234`. |
| E14 | Series owned by a transient external entry air only through their placeholders. | `library.py:89-93` | `lineup.transient` | none |
| E15 | A kind whose channel weight is 0 is prohibited. | `select.py:426-428` | channel or setting `kind_weights` | none |
| E16 | Only the channel's own line-up is eligible. Remote entries are eligible only when NAS-only is off for the channel. | `library.py:132-146` (indexes), `select.py:361` | setting `nas_only`, channel `nas_only` | The decision function is `lineup.py:136 nas_only_for`, which is why `select.py:20` imports `lineup`. Not checked in `_series_kept_for_peak` (`select.py:122`). |
| E17 | The readiness and editor constraints (`only_media_ids`, `exclude_media_ids`, `allow_external`) restrict the shelf. | `library.py:65-72,189` | Builder arguments from `horizon.py:204-220` | none |

### 1b. Cadence

| # | Rule | Decision | Parameters | Also at |
|---|---|---|---|---|
| C1 | The cadence is the channel's days, else the setting, with a minimum of one day. | `policy.py:105 cadence_seconds` | channel or setting `series_cadence_days` | none |
| C2 | Plain interval: the next episode is due one cadence after the last, less a grace of min(12 h, cadence/2). | `policy.py:111 next_episode_due` | literal `12 * HOUR` at `policy.py:116` | none |
| C3 | Own day: a series belongs to day `id % cadence_days`. | `policy.py:118 own_day` | none | The weight floor at `select.py:335` and `select.py:404`. |
| C4 | Series due. A first airing happens only on its own day. After that it airs on its own day once at least 2/5 of a cadence has passed, or on any day once 1.5 cadences have passed. With a cadence of a day or less, or any relax, the plain interval applies. | `policy.py:124 series_due` | literals `(cadence*2)//5` at `policy.py:143` and `cadence + cadence//2` at `policy.py:141` | Callers: `select.py:118,127-128,318,379`. |
| C5 | Cadence applies only to "weekly" series, meaning `mode == "auto"` and not sport. Strips, anchors and sport are exempt. | `select.py:316` | show `mode`, `category` | The same test is written separately at `select.py:113`. Remote titles are always treated as weekly (`select.py:379`). |
| C6 | Which last airing counts: any airing on the home channel, or the borrower's own airings on a borrowing channel. | `library.py:165 show_last`, `library.py:174 note_show_placed` | none | Anchors bypass it and write `show_last_placed` directly (`build.py:321`). |
| C7 | Cadence weight: full bonus within 12 h of target, `max(1.5, 0.6 x bonus)` within 36 h, 0.05 (0.3 when relaxed) when more than 36 h early, and a lateness ramp capped at 3.0. | `policy.py:145 cadence_factor` | setting `series_cadence_bonus`; literals at `policy.py:151-158` | The weight is composed with an own-day floor of 1.0 in two copies: `select.py:334-335` and `select.py:403-404`. |
| C8 | The day's series are held for the peak while supply is no more than the peak time still to come. Children's series and sport are exempt. | `select.py:89 _series_kept_for_peak`, applied at `select.py:290-291,321,385` | settings `peak_from`, `peak_until`; daypart `peak` (`select.py:76-82`) | The supply count re-implements candidate eligibility (section 3.2). |
| C9 | While series are held, a film may overrun the peak opening by at most 20 min. | `select.py:293,346` | `PEAK_OVERRUN_MINUTES` at `select.py:46` | none |
| C10 | In a peak daypart a film is offered only when no series candidate exists. | `select.py:420-423` | none | none |
| C11 | Daily cap: a series airs at most `show_daily_limit` times a day per channel, and each earlier airing multiplies the weight by `show_repeat_penalty`. | `select.py:306-311,326-327` | settings only, no channel override | Remote copy at `select.py:377,387,393`, where a film's cap is hardcoded to 1. `placed_today` is kept at `build.py:241-243,259,408,426`. A short run counts once (`build.py:426`, `runs.py:113-114`). |
| C12 | Rest: after its last episode a series rests `rest_weeks` and restarts from the first episode. | `slots.py:75-83 Show.advance` | show `rest_weeks`, else setting `series_rest_weeks` (`library.py:113`) | Gate at `select.py:303-305` (weight x0.3 at `select.py:328-329`). Anchors skip at `build.py:177`. An admin cursor clears the rest at `library.py:349`. |
| C13 | Never the same series back to back, including across kept slots, anchors and the day boundary. | barred set at `build.py:380-383`, applied at `select.py:298-302,383`; boundary lookup `build.py:209 _adjacent_show` | literal 12 h look-back at `build.py:215,217` | Overnight at `overnight.py:50-56,69-77`. Bands use an item-level version at `bands.py:294-295`. |
| C14 | Sport may follow itself at weekends. | `select.py:299-300` | setting `sport_back_to_back_weekends` | Not available to remote sport (`select.py:383`). |
| C15 | Episodes only advance. The cursor is the episode after the latest placed, or the admin cursor. Placements inside a rebuild window do not count. | `library.py:275-350`, `slots.py:68-83` | `show_cursor` table | none |
| C16 | Films: not within `movie_repeat_days` of any airing in either direction on any channel. Otherwise weighted by time since shown, and a never-shown film gets x2. | `select.py:339-345,351-357` | setting `movie_repeat_days` via `policy.py:161` | Ledger at `library.py:291,305`, `build.py:430`. |

### 1c. Relaxation ladder (precise)

Order of attempts, at `build.py:384`:
1. `(token, 0)`
2. `("show", 0)`, only when the token was `tv` or `movie`
3. `("show", 1)`
4. `("show", 2)`
5. If all fail: a holding card up to the next daypart boundary, of at least 60 min, then retry (`build.py:391-403`).

The token widening at step 2 is an undocumented relaxation step.

| Level | What changes | Where |
|---|---|---|
| relax >= 1 | The daypart kind weight and the daypart genre weight are not applied to items. A daypart genre weight of 0 stops being a bar. | `select.py:254-255` |
| relax >= 1 | The daypart kind weight is not applied to the choice of kind. | `select.py:429` |
| relax >= 1 | The kids daypart weight gets a floor of 0.2, so a `kids: 0.0` daypart no longer excludes. The kids cutoff still does. | `select.py:260` |
| relax >= 1 | The daypart `max_minutes` penalty is dropped. | `select.py:271` |
| relax >= 1 | The daypart overrun penalty is dropped. | `select.py:276` |
| relax >= 1 | The daily cap is lifted for library series. It stays in force for remote titles. | `select.py:310`; `select.py:387,393` |
| relax >= 1 | `series_due` falls back to the plain interval, for library and remote titles alike. | `policy.py:136`; `select.py:318,379` |
| relax >= 1 | The peak hold is lifted, and films may take peak slots. | `select.py:290,420` |
| relax >= 1 | Cadence weight: the early factor becomes 0.3 instead of 0.05, and the own-day floor is dropped. | `policy.py:152`; `select.py:335,404` |
| relax >= 1 | Sport weight is halved. Relaxing makes sport less likely, not more. | `select.py:269` |
| relax >= 1 | Borrowing is disabled. This tightens the rules. | `select.py:140-141` |
| relax == 2 | Cadence is not checked at all for library series. The next episode comes round early. | `select.py:318` |
| relax == 2 | Resting series are admitted at x0.3. | `select.py:304,328-329` |
| relax == 2 | Films inside the repeat window are admitted if the nearest airing is at least 12 h away, weighted `0.2 x (nearest/repeat)^2`. | `select.py:344,353` |
| relax == 2 | Sport is admitted in dayparts whose sport weight is below 0.5. | `select.py:267` |
| relax == 2 | A remote title held back for being finished, inside the lead window, over the day ceiling or early is admitted as a repeat of `last_spec`. The slot is marked `replay=1`, the episode does not advance and the wanted row is reused. It needs a prior request and stays under the daily cap. | `select.py:387-398`; `runs.py:42-47,72` |

Never relaxed:
- Certificates and the kids cutoff (`select.py:252`)
- Decades and strict matching (`select.py:237-240`)
- Era weight 0 (`select.py:244`)
- Duration fit and the short-episode stranding rule (`select.py:247-251`)
- Back to back (`select.py:298-302,383`)
- A channel kind weight of 0 (`select.py:427`)
- Channel genre weights (`select.py:261`), the genre-repeat penalty (`select.py:280-282`), the neat-fit bonus (`select.py:283`), the pool factor (`select.py:270`) and `show_repeat_penalty` (`select.py:327`)

### 1d. Remote (external) titles

| # | Rule | Decision | Parameters | Also at |
|---|---|---|---|---|
| X1 | A remote title may be newly placed only more than `external_lead_hours` ahead. | `policy.py:98 external_prepared`, used at `select.py:367,387` | setting `external_lead_hours` | `select.py:127` (supply count), `select.py:415,417`. `content.py:50 REMOTE_PRIORITY_HOURS` is a separate literal for the same idea. |
| X2 | Weight is `external_weight` once prepared, else `min(0.1, 0.1 x weight)`. | `policy.py:101 external_weight`, used at `select.py:372,399` | setting `external_weight`, clamped by `max(1.0, ...)` at `policy.py:102` | none |
| X3 | No more than `external_new_per_day` new remote commitments per broadcast day across all channels. Each component of a short run counts. | `select.py:370-371,387` | setting `external_new_per_day` | Ledger written in three places: `library.py:297,331-332` (load), `build.py:411` and `runs.py:104`. Supply copy at `select.py:121,130`. |
| X4 | Unseen first: once prepared, never-placed remote titles displace every other candidate of their kind. | `select.py:411-418` | none | none |
| X5 | Numbering takes the lowest number from `next_episode` that is neither taken (requests plus season 1 files on disk) nor beyond `episode_count`. | `runs.py:22 next_episode_number` | lineup `next_episode`, `episode_count` | `taken` is built at `library.py:196-206,222`. Issued at `runs.py:54-57`. "Finished" is tested at `select.py:382` and `runs.py:93`. `horizon.py:120-146` has a second lowest-unasked loop with different inputs (section 3.5). |
| X6 | A rebuild reuses open requests whose slots all fall inside the rebuilt window ("spares"). A film shares one request. | `library.py:159-163,224-225`; consumed at `runs.py:48-52,65-68` | none | `build.py:668-688 _raise_wanted` inserts or reuses rows. `horizon.py:149 withdraw_orphaned_requests`. |
| X7 | Last-resort repeat: re-air the last request, or the whole cached bundle for a short series. | `select.py:389-398`, `runs.py:42-47,84-94` | `library.external_short_runs` (`library.py:61`) | `last_spec` is seeded at `library.py:226-231`. |
| X8 | Length is the entry's `episode_minutes`, else `external_episode_minutes`. The slot is resized on delivery. | `library.py:211,236`; `lineup.py:610-621` | lineup or setting | `content.py:45 RESIZE_THRESHOLD` |
| X9 | A remote entry's scheduling class is read from its genres only. | `library.py:237` | `genres.scheduling_class` | Its `programme_type` override is not consulted here (section 3.3). |
| X10 | Remote episodes are always season 1 and titled "Episode N". | `runs.py:51,58`; `library.py:229-230`; `horizon.py:140,142` | hardcoded | Three copies of the spec or title construction. |

### 1e. Bands

| # | Rule | Decision | Parameters | Also at |
|---|---|---|---|---|
| B1 | The timetable: a band keeps its length past closedown, is clipped by the next band and by the next day start, and must be at least 60 s long. A start before `day_start` belongs to the small hours. | `bands.py:312 timetable`, `bands.py:303 band_start` | band `start`, `minutes`, `days` | Reused by `wanted.py:139`. Single implementation. |
| B2 | A band holds its whole configured stretch. A shortfall is its own titled holding card. The card is "short" unless a feature played. | `build.py:516 _fill_band` | setting `band_card_message` | Stale contrary comments at `build.py:308-310` and `bands.py:250-252`. |
| B3 | A band carries on around kept slots and anchors in segments of at least 60 s. It does not re-show what it showed before the cut. | `build.py:267-273,485-497` | none | none |
| B4 | Feature or item: `concert` or duration at or above the band, channel or setting item minutes. | `bands.py:75 is_feature`, `bands.py:211` | `band.max_minutes`, channel or setting `band_item_max_minutes` via `policy.py:90` | The inheritance is re-implemented at `wanted.py:83,99`. `guide.py:63` reads the global only. Default 15 sits at `bands.py:27` and `db.py:391`. |
| B5 | Where the index classifies a kind, a band's feature must be flagged as a concert. | `bands.py:199,217` | derived from the pool | none |
| B6 | Search ladder: (match, unknown genre, any genre) x (fresh, recent), then already played today. A strict band has 3 match-only steps. `fresh` drops the recent steps. A close-fit pass runs first when `fit` is given. | `bands.py:264-273` | channel `strict_matching`, band `only_matching` | The class docstring (`bands.py:179-182`) and PLAN 4.5 describe 4 steps. |
| B7 | A wrong decade is never played. An unknown year is allowed only after widening and never on a strict band. | `bands.py:219-221` | band `decades` | `bands.py:62 Band.dated`. `wanted.py:174` requires `dated is True`. |
| B8 | Genre match at each step. | `bands.py:224-230` | band `genres` | `bands.py:54-60 Band.wants` holds the same disjoint test. It is used for reservations (`bands.py:203`) and by `wanted.py:173`. |
| B9 | Items a narrow band needs are held back from the other bands at x0.1. | `bands.py:203-206,291-292` | literal 0.1 | none |
| B10 | Repeat gaps: item hours and feature days. Weight is `min(2, max(0.02, age/repeat))`, or 2.0 when never played. Already played today is weighted `1/(1+n)^2`. | `bands.py:261,284-292` | channel or setting `band_item_repeat_hours`, `band_feature_repeat_days` via `policy.py:90` | Inheritance re-implemented at `wanted.py:103-105,111-113`. |
| B11 | A feature is chosen to end within `band_fit_minutes` of the band end. It may overrun by `band_feature_overrun_minutes` only when nothing closer exists. | `bands.py:278,359-362` | settings, read raw at `build.py:482-483` | Literal defaults 600 and 1200 at `bands.py:186`. |
| B12 | The last band item may overrun by the duration tolerance when a band follows. It never runs into a kept slot or an anchor. The final band may run to the next day start. | `build.py:311-313,499-505`; `bands.py:354` | setting `duration_tolerance_minutes` | none |
| B13 | The band pool is limited to the channel's decades (strict: known years only). | `build.py:472-474` | channel `decades`, `strict_matching` | none |
| B14 | Free time on a bands-only channel: long items fitted to the next fixed point, then short items, fresh before recent. | `bands.py:374 fill_free`, via `build.py:332-340,545` | none | Same function for the overnight at `overnight.py:90`. |
| B15 | Band top-up need: airings inside the repeat window x items per airing, against matching items. | `wanted.py:67 band_needs` | settings `band_fetch*`; `BAND_ITEM_MINUTES = 4` at `wanted.py:53` | Uses `Band.wants`, `Band.dated`, `is_feature` and `timetable`. Duplicates `policy.band_limits`. |
| B16 | Band validation: 1 to 720 minutes, days 0 to 6, decades 1900 to 2100, kinds from `KINDS`. | `bands.py:85 clean` | `MAX_BAND_MINUTES` at `bands.py:29` | none |

### 1f. Overnight

| # | Rule | Decision | Parameters | Also at |
|---|---|---|---|---|
| O1 | A pattern channel replays its day from `overnight_replay_from`, looping at most 12 times, then shows a closedown card. | `overnight.py:26 replay` | channel `overnight_replay_from`; `MAX_LOOPS` at `overnight.py:23` | none |
| O2 | The replay skips fillers and withdrawn media. It starts on a programme, not on adverts. | `overnight.py:43,48-49`; the withdrawn set at `build.py:616-617` | none | none |
| O3 | The replay never starts with the series the day ended on, and never ends on tomorrow's first series. That series' adverts are suppressed with it. | `overnight.py:50-56,69-79` | `_adjacent_show` at `build.py:622` | Same rule as C13. |
| O4 | With nothing to replay, tomorrow's slots are borrowed. | `overnight.py:44-45`, `build.py:603-608` | none | none |
| O5 | A bands-only channel continues from its pool, not from a replay. | `build.py:449-453,627`; `overnight.py:90` | none | PLAN 4.9 says the opposite (section 4). |
| O6 | The replay begins when the day's last item ends, and nothing crosses the next day start. | `overnight.py:58,80` | none | none |

### 1g. Placement by type, decade and genre (line-up)

| # | Rule | Decision | Parameters | Also at |
|---|---|---|---|---|
| P1 | The programme type is decided in this order: the owner's choice, music kind, sport source, Documentary tag, unscripted Sport tag, Animation or Anime, else film or series. | `genres.py:151 programme_type` | `PROGRAMME_TYPES` at `genres.py:137`; `SCRIPTED` at `genres.py:60` | Call sites: `library.py:116`, `lineup.py:233,240,309,318`, `deps.py:135,154`, `admin.py:1207`. |
| P2 | A themed channel takes exactly its own types. A "kids" theme takes the kids flag. | `lineup.py:109-112` | channel `content`; `THEME_TYPES` at `genres.py:141` | The theme list is duplicated at `db.py:337 CHANNEL_CONTENT` and `ChannelEditor.svelte:55`. |
| P3 | General channels take films and series, and any type that no enabled themed channel claims. | `lineup.py:79 claimed_types`, `lineup.py:113-114` | none | none |
| P4 | Excluded genres bar an item on any channel. | `lineup.py:103-105` | channel `excluded_genres` | none |
| P5 | Decades bar placement. | `lineup.py:106-108` | channel `decades` | Same test as E7. Strict matching is not considered. |
| P6 | Among general channels, fit is the share of allowed genres matched. No genres gives 0.01, and an empty allowed list gives 0.05. | `lineup.py:115-121` | channel `allowed_genres`; literals | none |
| P7 | An item goes to the channel with the lowest (load + hours) / fit. Sport and general are balanced separately, with the channel number as the tie-break. | `lineup.py:166 _cheapest`, `lineup.py:147 _bucket`, `lineup.py:153 _load_hours` | none | none |
| P8 | Generation order: longest first, then kids, then certificate order. | `lineup.py:242-243` | literal cert order | none |
| P9 | A hand add or a move pins the entry. Rebalance deletes unpinned library entries. Retyping re-places even pinned titles. | `lineup.py:214,267,326,332,338,374-375` | none | none |
| P10 | An external title added by hand to a single-type themed channel takes that type. | `lineup.py:313-319` | none | none |
| P11 | A channel needs a line-up only if its pattern has a programme token. | `lineup.py:124 carries_programmes`, `lineup.py:47 PROGRAMME_TOKENS` | none | A subset of `rules.py:22 PATTERN_TOKENS`. |

### 1h. Borrowing

| # | Rule | Decision | Parameters | Also at |
|---|---|---|---|---|
| W1 | A channel borrows series of a type it lists, only at relax 0, and only where the daypart asks at least x2 for that type. Cartoon reads the `kids` weight, sport reads `sport`, documentary reads the daypart genre weight for "Documentary". No other type can be borrowed. | `select.py:133 _borrowed` | channel `also_carries`; `BORROW_AT` at `select.py:44`; type mapping at `select.py:144-145` | Pool at `library.py:137-140` (non-anchored only). The UI list is at `ChannelEditor.svelte:53`. Seed at `db.py:864`. |
| W2 | A borrowed series shares its episode position and keeps a per-borrower cadence. | `library.py:165-179` | none | See C6. |

### 1i. Dayparts and weights

| # | Rule | Decision | Parameters | Also at |
|---|---|---|---|---|
| D1 | The daypart table: the channel profile (dict or weekday list), else the settings for weekday, Saturday or Sunday, else the weekday table. | `rules.py:198 dayparts_for_weekday` | channel `daypart_profile`; settings `dayparts*` | Called at `select.py:209` and `build.py:397`. |
| D2 | The current daypart is the last one already started, on the broadcast-day clock (+1440 after midnight). | `rules.py:170 daypart_for`; clock at `select.py:64-67` | none | The clock helper exists five times (section 3.8). |
| D3 | Two-stage pick: the kind by channel kind weight x daypart kind weight, then the item. | `select.py:424-436` | `kind_weights` | none |
| D4 | The item is weighted by the daypart kind weight x the daypart genre weight. The largest matching weight applies, a weight of 0 is a bar, and names are canonical. | `select.py:255`, `select.py:169 _daypart_genre_weight` | daypart `genres` | The evening-only bar is injected into the seed profiles at import (`channel_profiles.py:193-199`). |
| D5 | The kids weight multiplies children's items. | `select.py:256-260` | daypart `kids` | none |
| D6 | Weekend breakfast boost: kids weight at least 4.0 when the daypart is named "Breakfast". | `select.py:225,258-259` | setting `weekend_kids_breakfast`; hardcoded name and 4.0 | Dead with shipped data (section 4). |
| D7 | Channel genre weights: the largest weight among the item's genres. | `select.py:163 _genre_weight` | channel `genre_weights` | The lookup is raw or lowercased, not canonical (section 3.7). |
| D8 | Exceeding the daypart `max_minutes` multiplies by 0.15, except for sport in a sport block. | `select.py:271-272` | daypart `max_minutes` | none |
| D9 | Overrunning the daypart end by more than 30 min multiplies by 0.02 for sport, 0.5 for a film under 75 min over, and 0.25 otherwise. | `select.py:275-277` | literals | The day end is hardcoded as 1440 at `select.py:212` and `build.py:400`. |
| D10 | Sharing a genre with the previous programme multiplies by `genre_repeat_penalty`, except within a sport block. | `select.py:227-228,280-282` | setting | Lowercase comparison, not canonical. |
| D11 | A programme ending within 10 min of the gap end gets x1.5. | `select.py:283-284` | literal 600 | none |
| D12 | Era pool normalisation: weight / pool_size ^ `era_pool_normalise`. | `library.py:243-271` | setting | none |
| D13 | Peak daypart: the daypart's own `peak`, else a start inside `peak_from` to `peak_until`. | `select.py:76-82` | settings; daypart `peak` | `peak` is unreachable from the admin (section 4). |

### 1j. Sport

| # | Rule | Decision | Parameters | Also at |
|---|---|---|---|---|
| S1 | Sport is a scheduling class: a sport source, or a Sport tag on something not scripted. | `genres.py:117 scheduling_class` | `SCRIPTED` | `catalogue.py:250`, `library.py:237`. |
| S2 | Below a daypart sport weight of 0.5, sport is ineligible until relax 2. | `select.py:264-268` | literal 0.5 | none |
| S3 | A sport weight of 3.0 or more makes a block: no `max_minutes` penalty and no genre-repeat penalty. | `select.py:211,271,280` | literal 3.0 | none |
| S4 | Sport is exempt from cadence and the peak hold. It may follow itself at weekends. | `select.py:113,125,299,316,385` | none | See C5, C14. |
| S5 | Line-up load is balanced separately for sport. | `lineup.py:147-150` | none | none |

### 1k. Short-episode runs

| # | Rule | Decision | Parameters | Also at |
|---|---|---|---|---|
| R1 | An episode under the threshold is followed by the next ones until the run length. The run carries the series title as its block. | `runs.py:111 series_run` | channel or setting pair via `policy.py:83` | Remote twin at `runs.py:74 external_run`. |
| R2 | Longer episodes met during a run are held and offered first next time. The look-ahead is 6 episodes, and a run never wraps the series. | `slots.py:85 take_short` | `look_ahead=6` default, never passed | none |
| R3 | A run never shows the same episode twice. | `runs.py:126` | none | none |
| R4 | Supply counting credits a short episode with the run target. | `select.py:119` | none | none |

### 1l. Patterns, adverts, idents, walk

| # | Rule | Decision | Parameters | Also at |
|---|---|---|---|---|
| A1 | Pattern tokens: unknown tokens are dropped, and an empty result becomes `show`. An empty pattern means a bands-only channel. | `rules.py:192 parse_pattern`; `build.py:278-282` | channel `pattern` | `lineup.py:124-128` |
| A2 | With adverts off, `ad` and `break` tokens are removed. | `build.py:280-282` | channel `ads_enabled` | none |
| A3 | A break holds at most `ads_per_break` adverts and `max_break_minutes`, shared across the pattern, the rounding pad and gap padding. | `build.py:92-110,344-355` | channel `ads_per_break` (default 2 at `build.py:240`); setting | `build.py:558 _pad` |
| A4 | Adverts must have a positive advert-era weight. A family channel takes family-safe adverts only. | `select.py:438-452` | setting `advert_era_weights`; channel `family_safe_ads` | The fallback to `DEFAULT_SETTINGS` at `select.py:57` bypasses `policy.value`. |
| A5 | Advert weights: x3 within `advert_year_window` of the programme, x0.1 inside `advert_repeat_penalty_hours`, and never within 900 s if another fits. | `select.py:454-473` | settings; literals 3.0, 0.1, 900 | The ledger `ad_last` is at `library.py:58`, `build.py:305,353,592`. |
| A6 | Idents: the channel's own, else generic, never another channel's. A 10 s stand-in only where the pattern asks. | `select.py:475-492` | channel `idents_enabled`; `STAND_IN_IDENT_SECONDS` at `select.py:39` | none |
| A7 | Start rounding: adverts pad to the next multiple, only on advert channels and only if the pad fits within the break cap. | `build.py:364-373` | setting `start_rounding_minutes` | none |
| A8 | Padding: adverts up to the break room, then at most 2 idents, then a filler card. At most 20 items. | `build.py:558-594` | literals 2 and 20 | none |
| A9 | An anchor is pinned at its time on its days. It is skipped if resting, already placed today, clashing with a kept slot, or unlabelled on a strict channel. It counts as a `show` token. | `build.py:171-185,246-259,316-326` | show `mode`, `anchor_time`, `anchor_days`; default days at `library.py:106-108` | none |
| A10 | The walk gives up after 3000 steps and fills the rest with filler. | `build.py:63,291,439-448` | `MAX_STEPS_PER_DAY` | `bands.MAX_STEPS` at `bands.py:30` |
| A11 | A day is complete if its slots reach within 60 s of closedown. Force or `from_ts` rebuilds unlocked slots from the cut. | `build.py:187-207` | literal 60 | none |
| A12 | Selection is seeded per (seed, channel, day). | `build.py:151`, `horizon.py:23` | none | none |

---

## 2. MODULE CONSTANTS THAT ARE RULES OR TUNABLES

| Location | Value | Controls |
|---|---|---|
| `select.py:44` `BORROW_AT` | 2.0 | Daypart weight at which borrowing opens |
| `select.py:46` `PEAK_OVERRUN_MINUTES` | 20 | How far a pre-peak film may run into the peak |
| `select.py:39` `STAND_IN_IDENT_SECONDS` | 10 | Stand-in ident length |
| `select.py:144-145` | cartoon to `kids`, sport to `sport`, documentary to genre "Documentary", else 0.0 | Which daypart field each borrowable type reads. Film, series and music cannot be borrowed. |
| `select.py:211` | 3.0 | Sport block threshold |
| `select.py:225,259` | "Breakfast", 4.0 | Weekend kids boost: the daypart name and the floor |
| `select.py:243` | 0.2 | Floor weight for a pinned title outside the eras |
| `select.py:260` | 0.2 | Kids weight floor when relaxed |
| `select.py:267,269` | 0.5, 0.5 | Sport ineligibility threshold; relaxed sport multiplier |
| `select.py:272` | 0.15 | Penalty for exceeding the daypart `max_minutes` |
| `select.py:276-277` | 30 min, 0.02, 0.5, 75 min, 0.25 | Daypart overrun penalty |
| `select.py:283-284` | 600 s, 1.5 | Neat-fit bonus |
| `select.py:329` | 0.3 | Resting-series weight at relax 2 |
| `select.py:344` | `12 * 3600` | Hard floor for a forced film repeat |
| `select.py:353,355,357` | 0.2, square law, 2.0, 2.0 | Film recency weights |
| `select.py:387,393` | 1 | Daily cap for a remote film |
| `select.py:463,466,468` | 3.0, 0.1, 900 s | Advert near-year bonus, recent penalty, no-repeat window |
| `select.py:103,212`; `build.py:400` | 1440 | Day end assumed to be midnight for daypart and peak arithmetic, regardless of the `day_end` setting |
| `policy.py:102-103` | `max(1.0, ...)`, `min(0.1, x 0.1)` | External weight clamp, and the weight inside the lead window |
| `policy.py:116` | `min(12 h, cadence // 2)` | Cadence grace |
| `policy.py:141,143` | 1.5 cadences; 2/5 of a cadence | Overdue and own-day thresholds |
| `policy.py:151-158` | 36 h, 0.3, 0.05, 12 h, `max(1.5, bonus x 0.6)`, `min(3.0, 1 + late/WEEK)` | Cadence weight curve |
| `policy.py:65`; `rules.py:61-62,76,157,166,132-133`; `wanted.py:134` | "08:00", "00:00", "21:00", "15", "PG" | Literal fallbacks that duplicate `DEFAULT_SETTINGS` |
| `horizon.py:40,166`; `wanted.py:105,113,119`; `lineup.py:658`; `guide.py:63` | 7, 2, 14, 36, 6, 7, 15 | More duplicated setting defaults read from a raw settings dict |
| `rules.py:16-21` | alias table | Certificate normalisation |
| `rules.py:172` | default daypart, all weights 1.0 | Used when no table exists |
| `build.py:63` `MAX_STEPS_PER_DAY` | 3000 | Walk bound |
| `build.py:204` | 60 s | "Day already complete" tolerance |
| `build.py:215,217` | `12 * 3600` | Back-to-back look-back across the day boundary |
| `build.py:240` | 2 | Fallback for `ads_per_break` |
| `build.py:294` | 60 s | Snap to a fixed item |
| `build.py:401` | `max(60, ...)` min | Minimum holding-card retry step |
| `build.py:462` | 120 s | Filler worth a build note |
| `build.py:492,495,533`; `bands.py:332` | 60 s | Minimum band segment, band length or holding card |
| `build.py:574,582` | 20, 2 | Pad loop guard; most idents run together |
| `bands.py:27` `ITEM_MINUTES` | 15 | Duplicate of the `band_item_max_minutes` default |
| `bands.py:29-30` | 720, 3000 | Longest band; most items per stretch |
| `bands.py:186` | 600, 1200 | Duplicates of the `band_fit_minutes` and `band_feature_overrun_minutes` defaults |
| `bands.py:264-269` | step tuples | The band search ladder. It is already data, but local to `pick`. |
| `bands.py:288,290,292` | 2.0, 0.02, `1/(1+n)^2`, 0.1 | Band pick weights and the reservation penalty |
| `overnight.py:23` `MAX_LOOPS` | 12 | Overnight replay loops |
| `library.py:33` `USABLE` | season 0 excluded | Rotation eligibility |
| `library.py:108` | strip Monday to Friday; weekly Monday | Default anchor days |
| `library.py:117` | year + seasons - 1 | Series end-year approximation. Repeated at `lineup.py:232`. |
| `slots.py:85` | `look_ahead=6` | How far a short run looks past long episodes |
| `slots.py:16` | "Programmes will continue shortly" | Filler title. Overnight card text is at `build.py:624,638`; band card text at `build.py:534`. |
| `lineup.py:117,119` | 0.01, 0.05 | Token fit scores |
| `lineup.py:242` | cert order map | Generation tie-break |
| `genres.py:53-62,137-148` | `CHILDRENS`, `CARTOONS`, `SCRIPTED`, `PROGRAMME_TYPES`, `THEME_TYPES` | Classification tables |
| `channel_profiles.py:193-199` | `EVENING_ONLY`, "17:00" | Game shows barred before 17:00 in seed profiles, applied by code at import |
| `db.py:337,849,864` | `CHANNEL_CONTENT`, `FETCH_KIND_FOR_CONTENT`, `ALSO_CARRIES_FOR_CONTENT` | Theme list and per-theme seeds |
| `wanted.py:53,136,148` | 4 min, 8 days, 14 | Band need estimate |
| `content.py:49-50` | 15 min, 24 h | Delivery deadline lead; remote fetch priority lead |
| `player/maintenance.py:35` | 3600 | Gap refill retry interval |

---

## 3. DUPLICATION AND SPLIT RESPONSIBILITY

**3.1 The relax ladder has no single definition.** Its meaning is the union of these conditions:
- `relaxed`, `not relaxed`, `relax < 2` and `not relax` at `select.py:140,254,260,267,269,271,276,290,304,310,318,334-335,344,379,393,403-404,420,429`
- `if relax or ...` at `policy.py:136`, and `relaxed=` at `policy.py:152`
- the attempt order at `build.py:384`

The docstring at `select.py:200-201` names two effects, and PLAN 4.8 step 3 names five. Three spellings are in use for the same two thresholds: `relax >= 1`, `relax`, and `not relax`.

**3.2 Supply counting duplicates candidate eligibility.** `_series_kept_for_peak` (`select.py:112-130`) re-implements the series and remote branches of `programme()` (`select.py:296-336,373-405`) and has drifted from them. It omits:
- resting series (`select.py:303`)
- decades, strict and era rejection (`select.py:237-245`)
- a daypart genre bar inside the peak (`select.py:255`)
- barred series
- `nas_only_for` for remote titles: `select.py:122` iterates `externals_on` with no such check, while `select.py:361` gates on it
- `finished` remote series (`select.py:382`)

It also calls `external_prepared(t)` with the current slot time, not the time the peak opens. The effect is an over-count of supply. That is the failure its own docstring describes at `select.py:98-101`.

**3.3 "Is this sport" exists in two parallel notions.**
- The class: `category == "sport"` at `select.py:113,125,262,299,316,385`, `lineup.py:150`, `library.py:104,114,237`, `catalogue.py:250`, derived by `genres.py:117`.
- The type: `ptype == "sport"` at `genres.py:163,167`, `select.py:144`, `library.py:116`.

An owner override of `programme_type` to sport changes the type (`library.py:116`) but not the class (`library.py:114`). Such a series is placed on the sport shelf yet scheduled as a weekly series under cadence and the peak hold. Remote titles get their class from genres only (`library.py:237`) and ignore the entry's `programme_type`.

**3.4 Kids detection.** Locations are listed in E6. Two risks:
- `allowed_at` recomputes `is_kids` from genres (`rules.py:167`), while every weight decision uses the cached `item["kids"]`. The cached value also folds in the show-level flag (`library.py:102`). An episode of a show flagged kids with no children's genre is "kids" for weighting, and also for `allowed_at` because `e["kids"]` is set. The two only agree because of `library.py:102`.
- `lineup.generate` sets films to `kids: 0` (`lineup.py:238`), while the scheduler marks films kids via `is_kids` (`library.py:121`). A "kids" themed channel (`lineup.py:112`) can therefore never be given a film.

**3.5 External episode numbering is split over four files.**
- State is loaded at `library.py:196-231`.
- The "next number" function is at `runs.py:22-29`.
- Numbers are issued and specs built at `runs.py:41-62`.
- "Finished" is tested at `select.py:382` and `runs.py:93`.
- Day-ceiling and last-placed ledgers are updated at `build.py:410-411` and again at `runs.py:102-104`.
- Rows are written at `build.py:668-688`.
- Renumbering is at `horizon.py:120-146`, and orphan withdrawal at `horizon.py:149-155`.

`bring_requests_forward` is a second lowest-unasked loop with different inputs. It treats only non-queued requests as settled (`horizon.py:132`). It ignores episodes on disk, which `library.py:222` counts as taken. It also ignores `episode_count`. It can therefore move a queued request onto a number the library already holds. The spec dict is built in three places (`runs.py:51-52,58-59`, `library.py:229-231`) and the "Episode N" title in a fourth (`horizon.py:140,142`).

**3.6 Cadence is split between policy and selector.**
- The formulae are whole in `policy.py:105-158`.
- Who is subject to them is decided in `select.py:316` and `select.py:113`.
- The own-day floor on the weight is composed twice (`select.py:334-335,403-404`).
- Which last airing counts is in `library.py:165-179`.
- Rest is in `slots.py:75-83`, which imports `policy.WEEK` at `slots.py:14` although its docstring says it holds no policy.
- Anchors skip when resting in `build.py:177`, and update the ledger directly at `build.py:321` instead of through `note_show_placed`.

**3.7 Genre comparison has six implementations.**
- Canonical: `genres.py:100 matches`
- Lowercase set, line-up: `lineup.py:75-76,103,115`
- Lowercase set, bands: `bands.py:57-58` and again at `bands.py:225-226`
- Lowercase, previous programme: `select.py:227,281`
- Raw then lowercase key lookup: `select.py:167`
- Canonical casefold: `select.py:179-180`

Stored genres pass through `db.genre_list`, so most of these agree in practice. Channel `genre_weights` keys and daypart `genres` keys are owner-typed JSON. Only the daypart path canonicalises them. A channel genre weight keyed "Sci-Fi" never matches.

**3.8 Broadcast-day clock (+1440 after midnight) has five copies.**
- `build.py:142-147`
- `select.py:64-67`
- `rules.py:159-160` (the inner `late`)
- `bands.py:307-308`
- `overnight.py:40`

**3.9 Strict-matching test.** `not (genres and year)` is written at `select.py:237`, `build.py:252` and `bands.py:222`. The pinned exemption appears at the first two only.

**3.10 Channel inheritance bypasses the policy.**
- `policy.py:53-61` is the declared mechanism, where non-NULL wins.
- `select.py:150-161` uses `or` semantics for `kind_weights` and `era_weights`.
- `wanted.py:99,103-105,111-113` re-implements `band_limits`.
- `guide.py:63` ignores the channel and the band.
- `nas_only_for` lives in `lineup.py:136`.
- `overnight.py:39` reads `overnight_replay_from` directly.
- `build.py:240` reads `ads_per_break` directly.

Settings are also read by string key outside the policy at `select.py:57,82,121,216,219,220,225,226,371`, `build.py:482-483,538`, `library.py:49-50,113,211` and all of `rules.py`. The policy docstring claims "typed access to every tunable scheduling rule" (`policy.py:1`).

**3.11 Decade parsing and the strict flag.** The channel decades JSON is parsed at `select.py:183-190` and `lineup.py:106`. `channel_fit` says it uses "the same test" as the scheduler (`lineup.py:100-102`) but omits `unknown_ok=not strict`. A strict channel is given unknown-year titles it will never air, which is the shelf problem that docstring warns about.

**3.12 Era spans parsed twice.** `select.py:56` and `library.py:49`.

**3.13 Eligibility rules inside the walk.** `scheduler/__init__.py` says build "decides nothing about eligibility". In fact:
- `build.py:177` holds rest
- `build.py:252` holds strict
- `build.py:474` holds decades and strict for band pools
- `build.py:384` holds the ladder order
- `build.py:374-378` holds slack
- `build.py:364-372` holds rounding
- `build.py:582` holds the ident limit

**3.14 Defaults and broadcaster models are in three files.** The generic dayparts, music bands, channels and theme seeds are in `db.py:285-351,460-490,849,864`. The four broadcaster profiles are in `channel_profiles.py`. The theme-to-type table is in `genres.py:141`. The theme list is repeated at `db.py:337` and in `ChannelEditor.svelte:55`. The borrowable types are repeated at `ChannelEditor.svelte:53`.

**3.15 Thin wrappers.** `Builder._filler`, `_programme_slot` and `_media_slot` (`build.py:223-225,596-601`) only unpack `channel["id"]` for the functions in `slots.py`.

---

## 4. DEAD OR SUSPICIOUS CODE, AND DOC DRIFT

Verified by grep over `pitv/` and `tests/`.

**Unused or unreachable**
1. `Library.started_channels` (`library.py:54-56`) is assigned and never read. It costs a UNION query over `history` and `schedule` on every build.
2. The `Band.wants(..., strict=...)` parameter (`bands.py:54`) is never passed by either caller (`bands.py:203`, `wanted.py:173`).
3. `Show.take_short(..., look_ahead=6)` (`slots.py:85`) is never passed. It is a constant in parameter form.
4. `weekend_kids_breakfast` (`db.py:410`, `settings_schema.py:116`, `select.py:225`) requires a weekend daypart named exactly "Breakfast". The only dayparts with that name are weekday ones (`db.py:286`, `channel_profiles.py:51`). Weekend tables open with "Saturday morning" and "Sunday morning". The setting does nothing with shipped data. No test covers it. It also keys a rule on a display name.
5. The daypart `peak` field (`select.py:79-80`, PLAN 4.8) has no seed and no test. The admin's daypart cleaner keeps only name, start, tv, movie, kids, sport and `max_minutes` (`web/src/lib/settingTypes.js:8-13`). A `peak` key cannot be set from the Settings pane and would not survive a save there. The same cleaner drops `genres` from the global daypart tables. The channel editor's own save path was not audited.
6. `external_weight`: `policy.py:102` clamps with `max(1.0, ...)`. Values in the schema's 0 to 1 range have no effect, although the help text says "1 is equal footing" (`settings_schema.py:229-230`). `tests/test_scheduler_policy.py` only checks 1.5.
7. `allowed_genres` on a themed channel is never consulted, because `lineup.py:110-112` returns before line 115. The Toons seed list (`db.py:489`) and the cartoons branch of `_seed_channel_genres` (`db.py:824-832`) have no effect on placement. The seed also contains "Cartoon", an alias of Animation.
8. `Selector._channel_setting` (`select.py:150`) has one caller. `SchedulerPolicy.channel_value` is only used internally and by a test. Neither is dead, but both are part of 3.10.
9. `wanted.py:132` has a function-local import of `scheduler.rules` although `wanted.py:21` already imports from that module at the top. It looks like a leftover from a past import cycle.

**Comments that no longer match the code**
10. `build.py:308-310`: "A band that ends early gives its time back to the channel". `_fill_band` (`build.py:533-542`) holds the stretch with the band's card. `bands.py:250-252` has the same stale sentence ("Such a band gives its time back"). Both contradict PLAN 4.5 and the configuration-authority rule.
11. `select.py:200-201` gives an incomplete account of relax (see 1c).
12. `select.py:307-309` cites PLAN section 4.5 for the daily cap. The rule is in 4.8.
13. The `bands.py:9-11` module docstring, `db.py:331-332` and PLAN 4.5 lines 496-497 all say `content` is only a label that no rule reads. `lineup.py:109-114` decides membership from it, and `db.py:849,864` seed from it.
14. The `bands.py:179-182` Filler docstring lists four search steps. `pick` has six, or three when strict.
15. The `slots.py:3-5` docstring says "no policy in it". The rest rule and the run look-ahead live there, and it imports `policy.WEEK`.
16. `scheduler/__init__.py`: build "decides nothing about eligibility" (see 3.13).
17. `db.py:352` is an orphaned comment about children's genres with nothing under it. `db.py:321-324` (the music channel) and `db.py:331-335` (content label, `LIVE`) sit above the wrong definitions.

**Docs against code**
18. PLAN 4.1 table: Toons kind weights are given as 1.0/0.0. The code has 0.85/0.15 (`db.py:488`). The Music row says "blocks".
19. PLAN 4.2 lines 339-346: the NAS-only paragraph is garbled. A sentence starts mid-clause at "for a channel, its external entries". A stray "(0.7) relative to catalogue programmes" is left over from an older external weight. The default is 1.0 (`db.py:442`).
20. PLAN 4.9 lines 649-650 says a bands-only channel replays its labelled blocks overnight. The code continues from the pool with unlabelled items (`build.py:449-453`, `overnight.py:90`). PLAN 4.5 lines 483-487 says the same as the code, so PLAN contradicts itself.
21. PLAN 4.7, "Certificate and kids rules override any daypart": bands, free time and anchors never call `allowed_at`. A film band or a bands-only channel's free time can play an 18 at 14:00, and an anchored 15 can sit at 17:00. This is either an undocumented exemption or a gap. The owner should decide.
22. PLAN 4.8 step 3 omits rest weeks, the own-day fallback, the peak hold, `max_minutes`, the overrun penalty and the token widening.
23. PLAN 4.4b line 414 points to section 4.5 for the last-resort repeat. It is described in 4.8.
24. REQUIREMENTS row 62 names `Builder.common_weight`. It is a closure inside `Selector.programme` (`select.py:232`). Rows 11 and 32 describe channel membership by genre lists. Since row 78 it is by programme type.
25. `policy.py:7` and `scheduler/__init__.py` say defaults are authoritative in `db.DEFAULT_SETTINGS`. The literal fallbacks listed in section 2 duplicate them.

---

## 5. PROPOSED CONSOLIDATION

**Principle.** Three questions get three homes, plus one leaf for the clock. Every move is a cut and paste with a re-export left behind where tests or outside modules import the old name.

The test surface that must keep working:
- `Selector.programme(channel, rng, t, gap, token, placed_today, prev, barred, relax=, slack=)`
- the `Selector._daypart_genre_weight` staticmethod
- `builder.select.settings`
- `builder.policy.series_due`, `next_episode_due`, `cadence_factor`, `external_prepared`, `external_weight`, `short_episode_seconds`, `band_limits`, `advert_break_seconds`
- `builder.runs.external_slot` and `Runs(...).series_run`
- `bands.Filler(...)`, `bands.timetable`, `bands.Band`
- the library attribute names `free_shows`, `movies_on`, `externals_on`, `anchored`, `bands`, `ad_last`
- `lineup.channel_fit`, `claimed_types`, `nas_only_for`
- everything `tests/test_rules.py` imports from `scheduler.rules`

### Step 0 (prerequisite, removes the cycle risk): `pitv/scheduler/clock.py`, a new leaf module

Move from `rules.py`: `tz_of`, `hhmm_to_minutes`, `local_ts`, `minutes_of_day`, `day_bounds`, `broadcast_day_for`.

Move from `policy.py`: `MINUTE`, `HOUR`, `DAY`, `WEEK`.

Add one `bday_minutes(minute, day_start_min)` and replace the five copies listed in 3.8.

`rules.py` and `policy.py` re-export these names, so `content.py`, `public.py`, `admin.py`, `wanted.py`, `doctor.py`, `readiness.py`, `controller.py`, `maintenance.py` and the tests are untouched. `clock.py` imports only `db.get_setting`.

Why this goes first: `policy.py:18` imports `rules`, and `slots.py:14` imports `policy`. If `rules` starts to take a policy, a policy-to-rules-to-policy cycle appears. After this step, `policy` and `slots` import `clock`, and nothing imports upwards.

### Step 1: `scheduler/policy.py` = what the configuration says

This holds every settings read, every channel or band inheritance, and every numeric tunable.

Add methods:
- `nas_only(channel)`, moved from `lineup.py:136`. Keep `lineup.nas_only_for` as a one-line alias for `tests/test_scheduler.py:14`. This removes the `select.py:20` import of `lineup`.
- `decades(channel)`, the one parser. `select.py:183-190` and `lineup.py:106` call it.
- `strict(channel, band=None)`
- `kids_rule(channel)`
- `kind_weights(channel)` and `era_spans(channel)`, moved from `select.py:150-161`
- `daily_limit(is_episode)`
- `peak_window`
- `rest_weeks(show_row)`
- `external_seconds(entry)`
- `external_new_per_day`
- `band_fit_seconds`, `band_feature_overrun_seconds`, `band_card_message`
- `ads_per_break(channel)`
- `overnight_replay_from(channel)`

Move every literal in section 2 that belongs to programme selection, cadence or the walk into a named constants block at the top of `policy.py`. One block, grouped by theme. Do not promote them to settings in this change.

Define the relax ladder once as data, for example a frozen dataclass per level with fields:
- `daypart_weights`
- `daily_cap`
- `own_day`
- `peak_hold`
- `borrowing`
- `resting`
- `cadence`
- `recent_films`
- `off_daypart_sport`
- `external_repeat`

Add `LADDER = (R0, R1, R2)` and the attempt order that is now at `build.py:384`. `Selector.programme` keeps its `relax: int` parameter and looks the level up, so the tests do not change. Each `relax` comparison in `select.py` becomes a named field read.

`wanted.band_needs` and `guide.py:63` call `policy.band_limits` instead of re-deriving it. `wanted` already receives `settings`, so constructing `SchedulerPolicy(settings, now)` there is enough. Remove the literal fallbacks in `rules.py`, `horizon.py`, `wanted.py` and `lineup.py:658` in favour of `policy.value`.

### Step 2: `scheduler/rules.py` = may this item air here, now

This holds every yes/no, as pure functions taking the policy and plain data.

Keep:
- certificates
- `is_kids` and `allowed_at`
- `in_decades`
- the era functions
- the daypart lookups
- `parse_pattern`

Add, by extraction:
- `labelled(item)`, the strict test of 3.9
- `explicit(item)`, from `select.py:236`
- `candidate_gate(item, ...)`, the zero-returning first half of `common_weight` (`select.py:236-253`)
- `subject_to_cadence(show_or_entry)`, from `select.py:316` and `select.py:113`
- `series_offerable(policy, channel, show, last, t, day_ordinal, level)`, covering rest, daily cap and due
- `external_offerable(...)`, covering `finished`, `prepared`, `room`, `early` and cap from `select.py:377-394`
- `film_offerable(...)`, from `select.py:339-347`
- `back_to_back_ok(...)`, from `select.py:298-302`
- `borrow_asks(dp, ptype)` with the type-to-field table, from `select.py:144-145`

`_series_kept_for_peak` stays in `select.py` because it needs the library. Its loop bodies become calls to `series_offerable` and `external_offerable` with the peak-opening minute. That closes the drift in 3.2 (NAS-only, resting, finished).

`build.py:177` and `build.py:252` call the same rule functions. The owner should decide whether anchors and bands should also pass `allowed_at` (item 21) before that call is added. That is a behaviour change, not a move.

The cadence formulae stay as methods on `SchedulerPolicy`. They are already whole there, and `tests/test_scheduler.py:1015-1023` and `test_scheduler_policy.py` call them. `rules.py` wraps them. `rules` then imports `policy`, which is safe after step 0. `policy` must not import `rules`.

### Step 3: `scheduler/weights.py`, a new module = how much

This holds every multiplier.

Move:
- the second half of `common_weight` (`select.py:254-285`)
- `_genre_weight` and `_daypart_genre_weight`; keep `Selector._daypart_genre_weight = staticmethod(weights.daypart_genre_weight)` for `tests/test_scheduler.py:1183-1192`
- the cadence-with-own-day composition, written once for the two copies at `select.py:334-335,403-404`
- the film recency weights (`select.py:351-357`)
- the advert weights (`select.py:461-469`)
- `Library.pool_factor` and `era_band`, optional; they need the pool counts, so they may stay in `library.py` and be called from weights

Use `genres.matches` and `canonical` for the previous-genre penalty and the channel genre weights (3.7).

`select.py` is then: build candidate lists by calling `rules.*_offerable`, weight them by calling `weights.*`, apply the unseen-first and peak filters, and pick. `weights` imports `policy`, `rules`, `genres` and `clock`. It never imports `library`, `select` or `lineup`.

### Step 4: bands stay in `scheduler/bands.py`, made the only home

- Fold the `Band.wants` genre test and the `_suits` genre test into one helper using `genres.matches`.
- Drop the unused `strict` parameter.
- Take the defaults at `bands.py:27,186` from the policy, leaving `ITEM_MINUTES` as an alias for `guide.py` until that reads the policy.
- Lift the step tuples (`bands.py:264-269`) to a module-level `SEARCH_LADDER`, and fix the docstrings.
- Move `wanted._matching_items` and `_airings_within` beside `Band`, as `Band.matching_count` and `airings_within`. They are band rules, and `wanted.py` keeps only the request logic. `bands` already imports `db`, so there is no new dependency.
- Fix the stale comments in item 10.

### Step 5: `scheduler/externals.py`, a new module, for remote numbering and requests

Move:
- `runs.next_episode_number`, re-exported from `runs` for `select`
- an `episode_spec` and `film_spec` builder, replacing `runs.py:51-52,58-59,67-68` and `library.py:229-231`
- the `taken`, `standing`, `on_disk` and `spare` loading from `library.py:196-231`, as `externals.load_state(conn, library)`
- `bring_requests_forward` and `withdraw_orphaned_requests` from `horizon.py`, re-exported there
- a `note_placed(library, lineup_id, day_str, t)` used by both `build.py:410-411` and `runs.py:102-104`

Make `bring_requests_forward` use the same taken set and the `episode_count` cap as `next_episode_number`.

This module imports `db` only. `library`, `runs`, `build` and `horizon` import it.

### Step 6: `pitv/channel_profiles.py` becomes the one file of broadcaster models and shipped defaults

Move from `db.py`:
- `DEFAULT_DAYPARTS`, `DEFAULT_DAYPARTS_SATURDAY`, `DEFAULT_DAYPARTS_SUNDAY`
- `_band` and `DEFAULT_MUSIC_BANDS`
- `DEFAULT_CHANNELS`
- `CHANNEL_CONTENT`
- `FETCH_KIND_FOR_CONTENT`
- `ALSO_CARRIES_FOR_CONTENT`

`db.py` already imports `channel_profiles` (`db.py:24`), and `channel_profiles` imports nothing. `db` re-exports the names for the tests that use `dbm.DEFAULT_*`. `DEFAULT_SETTINGS` stays in `db.py`: the policy, `settings_schema` and `test_settings_schema_matches_the_defaults` depend on it.

Derive `CHANNEL_CONTENT` from `genres.THEME_TYPES` keys plus "kids" so that the theme list exists once. `channel_profiles` may import `genres`, which imports nothing.

Serve the borrowable types and the themes to the admin from the API instead of the literals at `ChannelEditor.svelte:53,55`.

### Step 7: classification stays in `pitv/genres.py`, with one entry point each

- Add `is_sport(row_or_show)`. It returns true when the scheduling class is sport or the effective programme type is sport. This resolves 3.3.
- Use it in `library.py:104,114,237`, `lineup._bucket` and every `category == "sport"` in `select.py`.
- Move `rules.is_kids` here, since it reads only the flag and genres. Re-export it from `rules`.
- `lineup.generate` should compute film kids with it (`lineup.py:238`).

### Step 8: placement stays in `pitv/lineup.py`

`channel_fit`, `claimed_types`, `_cheapest`, `best_channel` and `generate` stay in `lineup.py`, because the tests import them there. `channel_fit` calls `policy.decades` and passes `unknown_ok=not strict` (3.11). Name its two literals. Do not move placement into the scheduler package: `lineup` imports `scheduler.rules`, `scheduler.slots` and `scheduler.policy`, and after step 1 nothing in `scheduler` imports `lineup`. The dependency must stay one-directional.

### Cleanups that ride along

- Delete `Library.started_channels`.
- Delete the `look_ahead` parameter. Make it a constant in the policy.
- Inline the three Builder wrappers.
- Parse era spans once, with `Selector` reading `library._global_spans` or the policy owning it.
- Decide the fate of `weekend_kids_breakfast`. Either key it on "the first daypart of a weekend day" or remove it. The 1980s profiles already give Saturday mornings a kids weight of 6.0, so the setting is redundant as well as dead.
- Either remove the `max(1.0, ...)` clamp or change the schema minimum to 1.
- Correct the docs listed in section 4.

### Import-cycle risks, in order of likelihood

1. `policy` to `rules` (`policy.py:18`), against the proposed `rules` to `policy`. Solved by step 0. Do step 0 first and alone, and run the suite.
2. `slots` to `policy` (`slots.py:14`). If `rules` or `weights` import `slots` for the `Show` type while `policy` imports anything that reaches `slots`, a cycle forms. Keep `slots` importing only `clock`.
3. `select` to `lineup` to `scheduler.rules` and `scheduler.slots`. This works today only because `lineup` never imports `select`, `build` or `library`. Moving `nas_only_for` to the policy removes the edge. Never let `rules`, `weights` or `policy` import `lineup`.
4. `db` to `channel_profiles` and `genres`. The defaults module must not import `db` or anything under `scheduler`.
5. `library` to `bands`, `policy`, `rules`, `slots`. `rules` and `weights` must take ledgers as arguments (`last`, `placements`, `external_per_day`) and must not import `Library`. A type-only import under `TYPE_CHECKING` is fine.
6. `wanted` to `scheduler.bands`, `library`, `rules`. If `bands` gains the need-counting helpers, it must not import `wanted`.

### Suggested order, each step its own commit

Run `tests/test_scheduler.py`, `test_lineup.py`, `test_scheduler_policy.py` and `test_rules.py` after each step.

0. `clock`
1. policy accessors and the constants block, with no behaviour change
2. the ladder as data
3. `rules` extraction and the `_series_kept_for_peak` rewrite. This intentionally changes supply counts on NAS-only channels and with resting series. Add a test for each.
4. `weights`
5. `externals`
6. bands, `wanted` and `guide`
7. defaults into `channel_profiles`
8. the `genres` entry points
9. dead code and docs

Steps 3, 5 (`bring_requests_forward`), 7 and 8 (`is_sport`, film kids) change behaviour and should each carry a new test. The rest are moves.