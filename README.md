# PiTV

A Raspberry Pi 4 that behaves like a 1980s UK television: four (or more) channels broadcasting a
weekly schedule of TV episodes, films and adverts from a NAS library, driven by a remote control,
with a teletext-style on-screen guide and a web interface for the schedule and administration.

- **Design**: [docs/PLAN.md](docs/PLAN.md)
- **Install on the Pi**: [setup/README.md](setup/README.md)
- **Web frontend**: [web/README.md](web/README.md)
- **Installer**: [installer/README.md](installer/README.md)
- **Content app** (fetch, encode, cache): private repo https://github.com/peteclarke-del/PiTV_content

## Development on a desktop

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
export PITV_DATA="$PWD/.dev" PITV_RUN="$PWD/.dev/run"
.venv/bin/pitv fake-library .dev/library --register --max-episodes 40   # tiny fake NAS
.venv/bin/pitv scan
.venv/bin/pitv schedule                    # build 7 days
.venv/bin/pitv listing --channel 3 --ads   # Radio Times style listing
PITV_WINDOWED=1 .venv/bin/pitv play --keyboard --now 2026-09-15T19:30   # player in a window
.venv/bin/pitv web --port 8080             # web UI + API docs at /api/docs
.venv/bin/python -m pytest
```

Player keys in the window or terminal: `1`–`9` channel, `[`/`]` channel down/up, `g` guide,
arrows navigate, `Enter` select, `Esc` back, `i` info, `Space` pause, `m` mute, `+`/`-` volume,
`r` restart programme, `q` quit.
