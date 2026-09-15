# Installing PiTV on the Raspberry Pi 4

1. Flash **Raspberry Pi OS Lite (64-bit, Bookworm)**. In the imager set hostname `pitv`, enable SSH,
   and configure a wired connection (a static IP is best; no Wi-Fi needed).
2. Boot, log in, and fetch the code:
   ```sh
   sudo apt-get install -y git
   git clone https://github.com/peteclarke-del/PiTV.git ~/PiTV
   cd ~/PiTV && sudo ./setup/install.sh
   ```
   The installer asks once for the NAS username/password (stored root-only in
   `/etc/pitv/smb-credentials`), mounts `smb://synologynas/tvshows/`, `movies/` and `ads/`
   under `/mnt/`, installs mpv/ffmpeg/python, creates the `pitv` user, the services and
   the local cache directory.
3. If a USB hard drive is attached for the cache, mount it at `/mnt/cache` first (add it to
   `/etc/fstab` with `nofail`), then re-run the installer or set the cache directory in the
   web UI (Admin → Weighting → Player/Cache).
4. Plug in the OSMC RF remote's USB dongle. It appears as a keyboard; no configuration needed.
5. Reboot. Within ~15 s the test card shows, then channel 1. Open `http://pitv/` for the
   web UI, set the admin password, run **Scan**, then **Build schedule**.

### Filling the cache with pitv_content

The separate `pitv_content` tool (PiTV_content project) runs on the desktop at 01:00 and 06:30,
reads `http://pitv/api/content/manifest?days=1`, copies or transcodes tomorrow's programmes into
the Pi's cache and fetches anything on the wanted list (music videos, missing episodes) into
`<cache>/acquired/...`, then posts `/api/content/report`. The installer exports the cache
directory over NFS to the LAN for that; on the desktop mount it with
`sudo mount -t nfs pitv:/mnt/cache /mnt/pitv-cache`. PiTV itself never downloads or encodes;
it only reads the cache and evicts old files from it. Downloads never go onto the NAS shares.
The tool's status, log and settings are on the Admin → Content tab.

### The television

The set is a 14" 4:3 colour CRT fed through an **HDMI-to-SCART converter**. The installer
configures HDMI to CEA mode 17 (720x576p, 50 Hz, 4:3 PAL) and tells mpv the screen is 4:3 so
the anamorphic 720x576 frame is shown with the right geometry; 4:3 programmes fill the screen
and widescreen films are letterboxed. `DISPLAY_MODE=composite` selects the Pi's own PAL
composite output instead, `hdmi43` a 1024x768 monitor. Overlays keep a 7% overscan margin and
use larger type; both are adjustable in Admin → Weighting → Player, as is `drm_connector` if
mpv picks the wrong output.

On the desktop the windowed player (`setup/dev.sh start`) is a preview of the Pi's picture:
a 768x576 4:3 window with the same scaler and deinterlacer and the same cache-first file
resolution, so what pitv_content transcodes can be checked as it plays.

Updating later: `cd ~/PiTV && git pull && sudo ./setup/install.sh` then
`sudo systemctl restart pitv-player pitv-web`.

Useful commands:
```sh
journalctl -u pitv-player -f       # player log
journalctl -u pitv-web -f          # web log
systemd-analyze blame | head       # boot time breakdown
```
