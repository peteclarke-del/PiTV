# PiTV

A Raspberry Pi 4 that behaves like a 1980s UK television: six channels broadcasting a weekly
schedule of TV episodes, films, adverts, sport, music videos and cartoons from a NAS library,
driven by an RF remote, with a teletext-style on-screen guide and a web interface for the
schedule and administration. It plays through an HDMI-to-SCART converter into a 14" 4:3 CRT.

Each channel carries its own line-up of series and films: generated from per-channel genre
lists, edited in the admin, and exclusive, so a programme never appears on two channels. With
NAS-only switched off, a line-up can also name titles that are not on disk; pitv_content
fetches them into the Pi's cache ahead of air time and they are removed after airing.

The work is split between two apps on the same Pi. pitv_content owns the sources (the NAS
shares and the online providers), indexes them and publishes a library index. PiTV imports
that index as its programme catalogue, builds the line-ups and the schedule, and asks
pitv_content for every file the schedule needs; pitv_content copies or transcodes it from the
NAS, or fetches it online, into the cache on the USB drive. The player plays the cache copy,
and the NAS original only if it has to. PiTV never indexes the NAS, downloads or encodes, and
pitv_content never decides what airs. The interface is [docs/CONTENT_CONTRACT.md](docs/CONTENT_CONTRACT.md).

- Design: [docs/PLAN.md](docs/PLAN.md); requirements checklist: [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)
- Install on the Pi: [setup/README.md](setup/README.md) (SD-card installer or manual script)
- Installer internals: [installer/README.md](installer/README.md)
- Web frontend: [web/README.md](web/README.md)
- pitv_content (sources, index, fetching, encoding, fills the cache; runs on the same Pi):
  private repo https://github.com/peteclarke-del/PiTV_content

## Running on the Pi

Write a card with the installer, plug in the USB cache drive and the remote dongle, and
power on. After the first boot the Pi shows the test card, then channel 1 once a schedule
exists; the web UI is at `http://pitv/`. Details in [setup/README.md](setup/README.md).

## Development on a desktop

Needs Python 3.11+, mpv, and ffmpeg to generate the fake library's clips. pitv_content is
not needed: the fake library comes with the index it would publish.

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
export PITV_DATA="$PWD/.dev" PITV_RUN="$PWD/.dev/run"
.venv/bin/pitv fake-library .dev/library --import --max-episodes 40   # tiny clips, their library index, imported
.venv/bin/pitv catalogue --file .dev/library/library.json            # re-import the index after editing it
.venv/bin/pitv schedule                    # build 7 days
.venv/bin/pitv listing --channel 3 --ads   # Radio Times style listing
PITV_WINDOWED=1 .venv/bin/pitv play --keyboard --now 2026-09-15T19:30   # player in a 768x576 preview window
.venv/bin/pitv web --port 8080             # web UI; API docs at /api/docs
.venv/bin/python -m pytest
```

`pitv catalogue` without `--file` asks pitv_content's API at `content_tool_url` and falls back
to `<cache_dir>/index/library.json`; `--reindex` asks pitv_content to re-index first and
waits for that job to finish before importing. Use it when pitv_content is running locally.

`setup/dev.sh` runs the whole system on a desktop, wired as it is on the Pi: the web service,
the windowed player and pitv_content's API (port 8091, as 8081 is often taken), all against
`.dev` and a RAM-backed cache at `/dev/shm/pitvcache/pitv`. `setup` builds the fake library,
seeds pitv_content's sources from it and points PiTV at the cache; `start`, `stop`, `restart`
and `status` manage the three processes; `index` has pitv_content re-index and PiTV import;
`cache` has pitv_content work PiTV's manifest into the cache, the same as Run now in the
admin. pitv_content is taken from `PATH`, or `PITV_CONTENT_BIN`, or a `PiTV_content` checkout
two folders up. A first end-to-end run is `setup/dev.sh setup && setup/dev.sh start &&
setup/dev.sh index && setup/dev.sh cache`. The frontend is built separately (see web/README.md) and the built
files under `pitv/web/static/` are committed, so the Pi never runs Node.

Player keys in the window or terminal: `1` to `9` channel, `[` and `]` channel down and up,
`g` guide, arrows navigate, `Enter` select, `Esc` back, `i` info, `Space` pause, `m` mute,
`+` and `-` volume, `r` restart programme, `q` quit.
