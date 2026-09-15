# PiTV requirements checklist

Everything asked for, where it lives, and how it is verified. Kept current with the code.

| # | Requirement | Where | Verified by |
|---|---|---|---|
| 1 | Four base channels: 1 and 2 programmes only, 3 and 4 with adverts in a show/ad/ad pattern | `DEFAULT_CHANNELS`, patterns in `scheduler/build.py` | `test_ads_only_on_ad_channels` |
| 2 | Content from the NAS TV, movie and advert shares; NAS is read-only | sources `tvshows`, `movies`, `ads` mounted `ro` (`setup/install.sh`) | manual on the Pi |
| 3 | Schedule 08:00 to midnight, overnight replays the day until 08:00 | `day_bounds`, `Builder._overnight` | `test_every_channel_day_is_covered` |
| 4 | Watershed at 21:00; date- and time-appropriate placement | `rules.allowed_at`, dayparts | `test_watershed_respected` |
| 5 | A week built ahead; episodes advance from the pilot through the seasons; variety; minimal weekly repeats | horizon build, episode cursors, movie repeat scoring | `test_episodes_in_order_per_show`, `test_movies_spread_evenly` |
| 6 | No episode of a series directly after another of the same series (sport exempt at weekends) | `last_show_id` rule, overnight tail check | `test_no_same_show_back_to_back` |
| 7 | Subtitles show the episode title, not the season/episode code | `slot_titles` / `episode_subtitle` in `scheduler/build.py` | `test_subtitle_is_episode_title`, `test_slot_titles` |
| 8 | Programmes of any era with a healthy pre/post-1980 mix; adverts 1980s/1990s only | `era_weights`, `advert_era_weights`, pool normalisation | `test_healthy_mix_of_eras_and_adverts_only_80s_90s` |
| 9 | Sport share, wrestling/snooker/motorcycle racing/strongman, weekend blocks modelled on a mid-80s week, back to back allowed at weekends | sport category, weekday/Saturday/Sunday dayparts | `test_weekend_afternoons_carry_sport` |
| 10 | Music channel from the music videos share: blocks by decade (1970s–2000s) and genre, two concerts a day | `build_music_day`, `music_blocks`, `music_decades` | `test_music_channel_day`, `test_music_channel_decades` |
| 11 | Cartoon channel from animated series; family-safe adverts only (no alcohol, sexual, smoking) | cartoon routing, `family_safe` flags | `test_cartoons_routed_to_cartoon_channel`, `test_family_safe_adverts_on_cartoon_channel` |
| 12 | Channels can be added, removed, enabled and disabled | channels API, admin Channels | `test_channels_and_settings` |
| 13 | Fast-booting Pi 4 with minimal overhead, hardware H.264 decode | `setup/boot-trim.sh`, `player/hwdec.py` | manual on the Pi |
| 14 | OSMC RF remote: channel select and +/−, volume +/−, mute, pause/restart, guide with up/down/left/right (left no further back than now) | `player/input.py`, controller guide navigation | manual; control-socket smoke test |
| 15 | Real-time programme guide on screen; agrees with the web guide | `player/osd.py` guide, 30 s refresh; lookups shared in `guide.py` | screenshots, `test_guide_collapses_music_blocks` |
| 16 | 14" 4:3 CRT via HDMI-to-SCART, PAL 576p; overlays overscan-safe | `DISPLAY_MODE=hdmi576`, `display_aspect`, `osd_safe_margin` | screenshots at 768x576 |
| 17 | Web interface: schedule view, admin for sources, channels, schedules, weighting, ads on/off, patterns; fast, reactive, clean | FastAPI + Svelte (`pitv/web`, `web/`) | `tests/test_api.py`, Playwright checks |
| 18 | Admin log viewer; player logs failures, timeouts, current file/codec/stream | `logsetup.py`, `/api/logs`, stream info lines | `test_content_tool_status` (logs API) |
| 19 | Local HDD cache; a day's television cached before 08:00 | manifest window through the next broadcast day; pitv_content fills it; `player/cache.py` evicts | `test_content_manifest_and_report`, `test_content_manifest_ignores_part_files` |
| 20 | Missing programmes are requested from pitv_content, which fetches, encodes to 576p and places them in the cache; downloads never go to the NAS | wanted list, manifest `wanted`/`layouts`, `acquire_dir` | `test_wanted_list`, `test_gap_detection` |
| 21 | Readiness: check in plenty of time; substitute and rebalance missing programmes; flag errors in the logs | `readiness.py`, live substitution in the player | `test_readiness_substitutes_missing_file` |
| 22 | Clear split with pitv_content; both on the Pi sharing the cache; synchronised | `content.py` contract, `.part`/marker rules in `player/cache.py`, `docs/PLAN.md` §7 | `test_content_manifest_and_report`, `test_content_manifest_ignores_part_files` |
| 23 | Everything moved to pitv_content is controllable and viewable from this admin | proxy `/api/content/tool/api/*`, Content tab schema form | `test_content_tool_proxy_offline`, `test_content_tool_proxy_non_json_and_log_shape` |
| 24 | No zombie or duplicated fetch/encode code in PiTV | `pitv/acquire` removed | grep in review |
| 25 | Watchdog, no memory leaks, 24/7; recover from power loss to the current time | `sdnotify.py`, unit hardening, hardware watchdog, clock wait/jump | manual on the Pi |
| 26 | Desktop preview window shows the Pi's picture (4:3 PAL) so transcodes can be judged | windowed mpv args in `mpv_ipc.default_args` | manual |
| 27 | SD-card installer (Linux and Windows): system + work partitions, clean/normal/upgrade modes, first-boot priming of both apps, Wi-Fi and maintenance user, install log in admin | `installer/` (Go CLI, DietPi image build, first-boot script), Logs → install | `installer/cli` Go tests; first boot on the Pi |
