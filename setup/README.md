# Installing PiTV on the Raspberry Pi 4

Two routes. The SD-card installer is the normal one: it produces a card that boots into a
fully provisioned Pi with PiTV and pitv_content on it. The manual script is for an existing
Raspberry Pi OS Lite install.

Either way you need: a Pi 4, an SD card, a USB hard drive for the cache, the OSMC RF
remote's USB dongle (it appears as a keyboard; no configuration), wired Ethernet to the NAS,
and an SMB account on the NAS with read access to the shares (read-only is enough).

## Route 1: the SD-card installer

1. Build or obtain the image and the installer binary (see
   [installer/README.md](../installer/README.md)).
2. Run the installer as root or administrator: `sudo dist/pitv-installer-linux-amd64` (on
   Windows `pitv-installer-windows-amd64.exe` from an administrator prompt). It asks once for
   mode (`normal` for a first install), hostname, maintenance user, network and Wi-Fi, NAS
   host, credentials and shares, display mode, system partition size, USB drive, and the
   PiTV admin password; saves the answers in `pitv-install.json`; then writes and verifies
   the card. The maintenance user is the only account that can log in over SSH.
3. Put the card in the Pi with the USB drive and remote dongle attached and power on. The
   first boot takes several minutes (DietPi setup, packages, both apps) and ends with a
   reboot into normal operation. Its log is at `/work/install/install.log` and in the admin
   under Logs. If it fails, the last lines of the log (also on the TV) give the reason: log in
   over SSH as the maintenance user, fix it and run `sudo bash /boot/Automation_Custom_Script.sh`
   to finish. DietPi does not run it again by itself.
4. Open `http://pitv/`. The admin password is the one given to the installer (or set it on
   first visit). The shares you gave the installer are pitv_content's sources: check them on
   the Sources page in the pitv_content section. Once pitv_content has indexed them, PiTV
   imports the catalogue and builds the schedule by itself within ten minutes; the
   Dashboard's import and build buttons do it at once. Channel 1 is on air as soon as the
   schedule exists.

Later upgrades: `pitv-installer upgrade --host pitv --user <maintenance user>` syncs the
local checkouts over SSH and re-runs both install scripts without touching the work
partition, the USB drive or the NAS settings (`--nas` changes the NAS host). The first
connection shows the Pi's host key fingerprint and records it once accepted; a different key
later is refused.

## Route 2: manual install on Raspberry Pi OS Lite

1. Flash Raspberry Pi OS Lite (64-bit, Bookworm). In the imager set hostname `pitv`, enable
   SSH and configure a wired connection (a static IP is best).
2. Boot, log in, and run the installer:
   ```sh
   sudo apt-get install -y git
   git clone https://github.com/peteclarke-del/PiTV.git ~/PiTV
   cd ~/PiTV && sudo ./setup/install.sh
   ```
   `install.sh` is idempotent. It asks once for the NAS username and password (stored
   root-only in `/etc/pitv/smb-credentials`); installs mpv, ffmpeg, Python, Pillow, evdev and
   cifs-utils; creates the `pitv` system user; copies the code to `/opt/pitv` with a venv,
   owned by root so the service cannot rewrite it; writes a CIFS mount and automount unit per
   share under `/mnt/` (read-only); creates the cache directory and its `acquired` folders on
   the mounted drive; installs the three services and a sudoers entry, checked with `visudo`,
   granting exactly the service actions the admin offers (restart the web service; restart,
   stop and start the player; restart pitv_content's API; start a pitv_content run); sets
   `cache_dir` and `acquire_dir` in PiTV's database, which is all PiTV keeps about content;
   writes the share list to `/etc/pitv/nas-sources.json` for pitv_content; and runs
   `boot-trim.sh`. Environment variables: `NAS_HOST` (`synologynas`), `SHARES` (`tvshows
   movies ads tvsports music%20videos`; a space in a share name is written `%20`),
   `CACHE_DIR` (`/mnt/cache/pitv`), `DISPLAY_MODE` (`hdmi576`), and `NAS_USER` with
   `NAS_PASS` to write the credentials without the prompt. Later runs reuse the values of the
   first from `/etc/pitv/install.env`.
3. Mount the USB drive at `/mnt/cache` (add it to `/etc/fstab` with `nofail`) before running
   the installer, or set the cache directory afterwards in Admin, Weighting, Cache.
4. Install pitv_content from its own repository (private,
   https://github.com/peteclarke-del/PiTV_content) with the same cache directory, passing the
   share list so it becomes pitv_content's sources (read on its first install only; after
   that, sources are edited in the admin):
   `sudo CACHE_DIR=/mnt/cache/pitv PITV_URL=http://127.0.0.1 NAS_SOURCES="$(cat /etc/pitv/nas-sources.json)" ./setup/install-on-pi.sh`.
5. Reboot. The test card shows within seconds, then channel 1 once the clock is
   synchronised and a schedule exists. Open `http://pitv/`, set the admin password and check
   the Sources page in the pitv_content section. PiTV imports the catalogue once
   pitv_content has indexed the shares and then builds the schedule; the Dashboard's
   buttons do both at once.

Updating later: `cd ~/PiTV && git pull && sudo ./setup/install.sh`, then
`sudo systemctl restart pitv-player pitv-web`.

## The television

The set is a 14" 4:3 colour CRT fed through an HDMI-to-SCART converter. `boot-trim.sh`
keeps everything it sets in one marked block at the end of `config.txt`, rewritten on every
run, and switches on the analogue jack, which carries composite mode's sound. `DISPLAY_MODE`
selects the display lines in that block:

| Mode | Output |
|---|---|
| `hdmi576` (default) | HDMI CEA mode 17: 720x576p, 50 Hz, 4:3 PAL, for the converter |
| `composite` | The Pi's own PAL composite output on the 3.5 mm jack |
| `hdmi43` | 1024x768 for a 4:3 monitor |
| `hdmi` | The monitor's preferred mode |

mpv is told the screen is 4:3 (`display_aspect`) so the anamorphic 720x576 frame has the
right geometry: 4:3 programmes fill the screen and widescreen films are letterboxed.
Overlays keep a 7% overscan margin and use larger type; margin, text scale and
`drm_connector` (if mpv picks the wrong output) are in Admin, Weighting, Player.

On the desktop the windowed player (`setup/dev.sh start`) is a preview of the Pi's picture:
a 768x576 4:3 window with the same scaler and deinterlacer and the same cache-first file
resolution, so what pitv_content transcodes can be checked as it plays.

## Useful commands

```sh
journalctl -u pitv-player -f       # player log (also Admin, Logs)
journalctl -u pitv-web -f          # web log
sudo -u pitv /opt/pitv/.venv/bin/pitv catalogue --reindex   # ask pitv_content to re-index, then import
systemd-analyze blame | head       # boot time breakdown
systemctl status mnt-tvshows.automount   # one per share
```
