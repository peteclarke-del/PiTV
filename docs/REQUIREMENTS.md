# PiTV requirements checklist

Everything asked for, where it lives, and how it is verified. Kept current with the code;
the design behind each row is in [PLAN.md](PLAN.md).

| # | Requirement | Where | Verified by |
|---|---|---|---|
| 1 | Four general channels: 1 and 2 programmes only, 3 and 4 with adverts in a show, ad, ad pattern | `DEFAULT_CHANNELS` in `db.py`; patterns in `scheduler/build.py` | `test_ads_only_on_ad_channels` |
| 2 | Content from the NAS TV, movie, advert, sport and music video shares; the NAS is read-only | sources registered by `setup/install.sh`; shares mounted `ro` (`systemd/mnt-share.mount.template`) | manual on the Pi |
| 3 | Schedule 08:00 to midnight; overnight replays the day until 08:00 | `rules.day_bounds`, `Builder._overnight` | `test_every_channel_day_is_covered`, `test_no_slot_crosses_into_next_day_start` |
| 4 | Watershed at 21:00 with certificate and kids rules; date- and time-appropriate placement | `rules.allowed_at`, `watershed` and `tv_watershed` settings, dayparts | `test_watershed_respected`, `test_watershed_movie_vs_tv`, `test_kids_cutoff` |
| 5 | A week built ahead; episodes advance from the pilot through the seasons; variety; minimal weekly repeats | horizon build, episode cursors, movie repeat scoring | `test_episodes_in_order_per_show`, `test_movies_spread_evenly`, `test_show_stays_on_home_channel` |
| 6 | No episode of a series directly after another of the same series (sport exempt at weekends) | `last_show_id` rule, overnight tail check | `test_no_same_show_back_to_back` |
| 7 | Subtitles show the episode title, not the season/episode code | `slot_titles`, `episode_subtitle` in `scheduler/build.py` | `test_subtitle_is_episode_title`, `test_slot_titles` |
| 8 | Programmes of any era with a healthy pre/post-1980 mix; adverts 1980s and 1990s only | `era_weights`, `advert_era_weights`, era pool normalisation | `test_healthy_mix_of_eras_and_adverts_only_80s_90s`, `test_default_eras_allow_old_programmes_but_not_old_adverts` |
| 9 | Sport share (wrestling, snooker, motorcycle racing, strongman); weekend blocks modelled on a mid-80s week; back to back allowed at weekends | sport category, weekday, Saturday and Sunday dayparts | `test_weekend_afternoons_carry_sport` |
| 10 | Music channel from the music videos share: blocks by decade (1970s to 2000s) and genre, two concerts a day | `build_music_day`, `music_blocks`, `music_decades` | `test_music_channel_day`, `test_music_channel_decades` |
| 11 | Cartoon channel from animated series; family-safe adverts only | the cartoon channel's genre list and line-up generation, `family_safe` flags | `test_cartoons_routed_to_cartoon_channel`, `test_generation_respects_channel_genres`, `test_family_safe_adverts_on_cartoon_channel` |
| 12 | Channels can be added, removed, enabled and disabled | channels API, admin Channels tab | `test_channels_and_settings` |
| 13 | Fast-booting Pi 4 with minimal overhead; hardware H.264 decode | `setup/boot-trim.sh`, `splash.py`, `player/hwdec.py` | manual on the Pi |
| 14 | OSMC RF remote: channel select and steps, volume, mute, pause and restart, guide with up/down/left/right (left no further back than now); keys reassignable by pressing them | `player/input.py`, guide navigation in `player/controller.py`, keymap editor on the admin Player tab | manual on the Pi |
| 15 | Real-time programme guide on screen that agrees with the web guide | `player/osd.py`, 30 s refresh; lookups shared in `guide.py` | screenshots, `test_guide_collapses_music_blocks` |
| 16 | 14" 4:3 CRT through HDMI-to-SCART at PAL 576p; overlays overscan-safe | `DISPLAY_MODE=hdmi576` in `boot-trim.sh`, `display_aspect`, `osd_safe_margin`, `osd_scale` | screenshots at 768x576 |
| 17 | Web interface: schedule view and admin for sources, channels, schedules, weighting, ads on/off and patterns; fast, reactive, clean | FastAPI and Svelte (`pitv/web`, `web/`) | `tests/test_api.py`; browser checks by hand |
| 18 | Virtual remote on the web and from the API | `#/remote`, `POST /api/player/key`, `/channel`, `/volume`, control socket | `test_player_offline`; manual |
| 19 | Admin log viewer; the player logs failures, timeouts and the current file, codec and stream | `logsetup.py`, `/api/logs`, `/api/logs/{name}`, `/api/logs/journal/{unit}`, stream info lines in `controller.py` | `test_content_tool_proxy_non_json_and_log_shape` (log shape); manual |
| 20 | Install log from the SD-card installer and first boot readable in the admin | `/work/install/install.log`, Logs tab source "install" | manual (first boot on the Pi) |
| 21 | Local HDD cache; a day's television cached before 08:00 | manifest window through the next broadcast day; pitv_content fills it; `player/cache.py` evicts | `test_content_manifest_and_report`, `test_content_manifest_ignores_part_files` |
| 22 | Missing programmes are requested from pitv_content, which fetches, encodes to 576p and places them in the cache; downloads never go to the NAS | wanted list and admin Wanted tab, manifest `wanted` and `layouts`, `acquire_dir` | `test_wanted_list`, `test_gap_detection` |
| 23 | Readiness: check in plenty of time; substitute and rebalance missing programmes; flag errors in the logs | `readiness.py` at `readiness_hours`, `POST /api/content/readiness`, live substitution in the player | `test_readiness_substitutes_missing_file`, `test_content_tool_status` |
| 24 | Clear split with pitv_content; both on the Pi sharing the cache; synchronised | `content.py` contract, `.part` and marker rules in `player/cache.py`, PLAN.md section 7 | `test_content_manifest_and_report`, `test_content_manifest_ignores_part_files` |
| 25 | pitv_content's status, log and runs visible and controllable from this admin | `GET /api/content/tool`, `POST /api/content/tool/run`, `GET /api/content/tool/log`, admin Content tab | `test_content_tool_status` |
| 26 | Everything moved to pitv_content (settings, providers, catalogue, jobs) is controllable from this admin | proxy `/api/content/tool/api/*`, Content tab schema form | `test_content_tool_proxy_offline`, `test_content_tool_proxy_non_json_and_log_shape` |
| 27 | No zombie or duplicated fetch/encode code in PiTV | `pitv/acquire` removed | grep in review |
| 28 | Watchdog, no memory leaks, 24/7; recover from power loss to the current time | `sdnotify.py`, unit hardening, hardware watchdog, clock wait and jump handling | manual on the Pi |
| 29 | Desktop preview window shows the Pi's picture (4:3 PAL) so transcodes can be judged | windowed mpv args in `mpv_ipc.default_args`, `setup/dev.sh` | manual |
| 30 | Settings and overrides exportable for backup | `GET /api/export`, System tab | manual |
| 31 | SD-card installer (Linux and Windows): system and work partitions, clean/normal/upgrade modes, first-boot priming of both apps, Wi-Fi and maintenance user | `installer/` (Go CLI, DietPi image build, first-boot script) | `installer/cli` Go tests; first boot on the Pi |
| 32 | Each channel carries only programmes that suit it, driven by configuration in the admin, never by channel names or numbers in code | per-channel allowed and excluded genres, `lineup.channel_fit` and `generate` | `test_generation_respects_channel_genres` |
| 33 | A programme belongs to one channel only; a series on one channel is never scheduled on another | `lineup` table, derived `home_channel_id`, scheduler candidate filter | `test_every_programme_belongs_to_exactly_one_channel`, `test_week_never_shares_a_programme_across_channels`, `test_show_stays_on_home_channel` |
| 34 | Admin can add and remove programmes on a channel from a dropdown, including titles not on disk | line-up drawer, `GET /api/lineup/options`, `POST/PUT/DELETE /api/lineup` | `test_move_and_remove_entries`, `test_lineup_api` |
| 35 | NAS-only switch: schedule only what is on the NAS or in the cache, or also schedule line-up entries that pitv_content will fetch | `nas_only` setting and channel override, external placeholders, `external_lead_days` | `test_external_entry_scheduled_ahead_and_requested` |
| 36 | Transient content: fetched material lives only in the cache and is removed after airing | `transient`, `remove_after_airing`, `transient_keep_days`, `lineup.remove_aired_transients` | manual; covered in part by `test_bind_fetched_attaches_file_to_placeholder` |
| 37 | Fetched files take their scheduled slots; missing ones are substituted in time | `lineup.bind_fetched`, readiness placeholder checks | `test_bind_fetched_attaches_file_to_placeholder`, `test_readiness_substitutes_unfetched_placeholders` |
| 38 | Line-ups kept as a configuration file that survives a database rebuild and can be edited offline | `lineups.json` mirror, `GET /api/lineup/export`, `POST /api/lineup/import`, restore on scan | `test_move_and_remove_entries`, `test_lineup_api` |
