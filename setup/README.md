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

### The television

The target set is a 14" 4:3 colour CRT. By default the installer configures the Pi 4's
composite output (PAL, 4:3) on the 3.5 mm AV jack; use a TRRS-to-phono lead into the set's
SCART or AV input. If the set is fed through an HDMI-to-SCART converter instead, install with
`DISPLAY_MODE=hdmi43 sudo ./setup/install.sh` (HDMI forced to 1024x768, 4:3). Overlays keep a
7% overscan margin and use larger type; both are adjustable in Admin → Weighting → Player.
If mpv picks the wrong output, set `drm_connector` there (e.g. `Composite-1`).

Updating later: `cd ~/PiTV && git pull && sudo ./setup/install.sh` then
`sudo systemctl restart pitv-player pitv-web`.

Useful commands:
```sh
journalctl -u pitv-player -f       # player log
journalctl -u pitv-web -f          # web log
systemd-analyze blame | head       # boot time breakdown
```
