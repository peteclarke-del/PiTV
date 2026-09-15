# PiTV

A Raspberry Pi 4 that behaves like a 1980s UK television: six channels broadcasting a weekly
schedule of TV episodes, films, adverts, sport, music videos and cartoons from a NAS library,
driven by an RF remote, with a teletext-style on-screen guide and a web interface for the
schedule and administration. It plays through an HDMI-to-SCART converter into a 14" 4:3 CRT.

- Design: [docs/PLAN.md](docs/PLAN.md); requirements checklist: [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)
- Install on the Pi: [setup/README.md](setup/README.md) (SD-card installer or manual script)
- Installer internals: [installer/README.md](installer/README.md)
- Web frontend: [web/README.md](web/README.md)
- Content app (fetches, encodes and fills the cache; runs on the same Pi): private repo
  https://github.com/peteclarke-del/PiTV_content

## Running on the Pi

Write a card with the installer, plug in the USB cache drive and the remote dongle, and
power on. After the first boot the Pi shows the test card, then channel 1 once a schedule
exists; the web UI is at `http://pitv/`. Details in [setup/README.md](setup/README.md).

## Development on a desktop

Needs Python 3.11+, mpv and ffprobe on the path.

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
export PITV_DATA="$PWD/.dev" PITV_RUN="$PWD/.dev/run"
.venv/bin/pitv fake-library .dev/library --register --max-episodes 40   # tiny fake NAS with tiny ffmpeg clips
.venv/bin/pitv scan
.venv/bin/pitv schedule                    # build 7 days
.venv/bin/pitv listing --channel 3 --ads   # Radio Times style listing
PITV_WINDOWED=1 .venv/bin/pitv play --keyboard --now 2026-09-15T19:30   # player in a 768x576 preview window
.venv/bin/pitv web --port 8080             # web UI; API docs at /api/docs
.venv/bin/python -m pytest
```

`setup/dev.sh start|stop|restart|status` runs the web service and the windowed player
together against `.dev`. The frontend is built separately (see web/README.md) and the built
files under `pitv/web/static/` are committed, so the Pi never runs Node.

Player keys in the window or terminal: `1` to `9` channel, `[` and `]` channel down and up,
`g` guide, arrows navigate, `Enter` select, `Esc` back, `i` info, `Space` pause, `m` mute,
`+` and `-` volume, `r` restart programme, `q` quit.
