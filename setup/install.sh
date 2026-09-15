#!/usr/bin/env bash
# PiTV installer for Raspberry Pi OS Lite (64-bit, Bookworm). Run as root on the Pi:
#   sudo ./setup/install.sh
# Idempotent: safe to re-run after `git pull`. The first run records its choices in
# /etc/pitv/install.env; later runs (upgrades) reuse them unless overridden in the environment.
set -euo pipefail

ENV_FILE=/etc/pitv/install.env
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
NAS_HOST="${NAS_HOST:-${SAVED_NAS_HOST:-synologynas}}"
# Share names as on the NAS; a space is written as %20 (mounted at /mnt/<name without spaces>).
SHARES="${SHARES:-${SAVED_SHARES:-tvshows movies ads tvsports music%20videos}}"
mount_name() { echo "$1" | sed 's/%20//g; s/ //g'; }
share_name() { echo "$1" | sed 's/%20/ /g'; }
INSTALL_DIR=/opt/pitv
DATA_DIR=/var/lib/pitv
CACHE_DIR="${CACHE_DIR:-${SAVED_CACHE_DIR:-/mnt/cache/pitv}}"
DISPLAY_MODE="${DISPLAY_MODE:-${SAVED_DISPLAY_MODE:-hdmi576}}"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

log() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }

log "Packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  mpv ffmpeg python3 python3-venv python3-pip python3-pil python3-evdev \
  cifs-utils fonts-dejavu-core git rsync alsa-utils

log "User and directories"
id pitv >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin pitv
usermod -aG video,render,audio,input,tty pitv
mkdir -p "$DATA_DIR" /etc/pitv "$INSTALL_DIR"
chown -R pitv:pitv "$DATA_DIR"

log "Code -> $INSTALL_DIR"
rsync -a --delete --exclude .git --exclude .venv --exclude .dev --exclude node_modules --exclude web/node_modules \
  "$SRC_DIR/" "$INSTALL_DIR/"
if [ ! -x "$INSTALL_DIR/.venv/bin/python" ]; then
  python3 -m venv --system-site-packages "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -q -e "$INSTALL_DIR"
# The code is root-owned and read-only to the service user: a compromised web session must
# not be able to rewrite the program it runs under. Bytecode is compiled now, as root.
"$INSTALL_DIR/.venv/bin/python" -m compileall -q "$INSTALL_DIR/pitv"
chown -R root:root "$INSTALL_DIR"
chmod -R u+rwX,go+rX,go-w "$INSTALL_DIR"

log "SMB credentials"
if [ ! -f /etc/pitv/smb-credentials ]; then
  read -rp "NAS username for $NAS_HOST: " smb_user
  read -rsp "NAS password: " smb_pass; echo
  (umask 077; printf 'username=%s\npassword=%s\n' "$smb_user" "$smb_pass" > /etc/pitv/smb-credentials)
fi
chown root:root /etc/pitv/smb-credentials; chmod 600 /etc/pitv/smb-credentials

log "CIFS automounts for: $SHARES"
for share in $SHARES; do
  m="$(mount_name "$share")"; n="$(share_name "$share")"
  mkdir -p "/mnt/$m"
  sed -e "s|@NAS@|$NAS_HOST|g" -e "s|@SHARE@|$n|g" -e "s|@MOUNT@|$m|g" "$SRC_DIR/systemd/mnt-share.mount.template" > "/etc/systemd/system/mnt-$m.mount"
  sed -e "s|@MOUNT@|$m|g" "$SRC_DIR/systemd/mnt-share.automount.template" > "/etc/systemd/system/mnt-$m.automount"
done

log "Local cache directory ($CACHE_DIR)"
# Only on a mounted drive: created on the SD card it would fill the card and hide the missing mount.
if mountpoint -q "$CACHE_DIR" || mountpoint -q "$(dirname "$CACHE_DIR")"; then
  mkdir -p "$CACHE_DIR"/acquired/{tvshows,tvsports,movies,ads,"music videos"} "$CACHE_DIR"/logs "$CACHE_DIR"/reports
  chown -R pitv:pitv "$CACHE_DIR"
else
  echo "WARNING: $(dirname "$CACHE_DIR") is not a mounted drive; skipping the cache directory (mount the work drive and re-run)"
fi

log "systemd services"
cp "$SRC_DIR/systemd/pitv-player.service" "$SRC_DIR/systemd/pitv-web.service" "$SRC_DIR/systemd/pitv-splash.service" /etc/systemd/system/
# Exactly the commands pitv/web/api/admin.py SERVICE_ACTIONS and content.py tool_run issue.
cat > /etc/sudoers.d/pitv <<'SUDO'
pitv ALL=(root) NOPASSWD: /usr/bin/systemctl restart pitv-web.service, /usr/bin/systemctl restart pitv-player.service, /usr/bin/systemctl stop pitv-player.service, /usr/bin/systemctl start pitv-player.service, /usr/bin/systemctl restart pitv-content-api.service, /usr/bin/systemctl start pitv-content.service
SUDO
chmod 440 /etc/sudoers.d/pitv
systemctl daemon-reload
for share in $SHARES; do systemctl enable --now "mnt-$(mount_name "$share").automount"; done
systemctl enable pitv-splash pitv-player pitv-web

log "Database and cache settings"
# Values reach Python through the environment, never by splicing them into the source.
# PiTV does not keep sources: the NAS shares are pitv_content's (docs/CONTENT_CONTRACT.md). They
# are written once to /etc/pitv/nas-sources.json, which seeds pitv_content's installer.
sudo -u pitv PITV_DATA="$DATA_DIR" PITV_CACHE_DIR="$CACHE_DIR" "$INSTALL_DIR/.venv/bin/python" - <<'PY'
import os
from pitv import db as dbm
from pitv.config import load_config
cache_dir = os.environ["PITV_CACHE_DIR"]
cfg = load_config(); cfg.ensure_dirs()
conn = dbm.connect(cfg.db_path); dbm.init_db(conn)
with dbm.tx(conn):
    if not dbm.get_setting(conn, "cache_dir"):
        dbm.set_setting(conn, "cache_dir", cache_dir)
    if not dbm.get_setting(conn, "acquire_dir"):
        dbm.set_setting(conn, "acquire_dir", f"{cache_dir}/acquired")
print("cache:", dbm.get_setting(conn, "cache_dir"))
PY
PITV_SHARES="$SHARES" PITV_NAS_HOST="$NAS_HOST" python3 - > /etc/pitv/nas-sources.json <<'PY'
import json, os
# share name on the NAS -> (source id, display name, type, category) for pitv_content
known = {"tvshows": ("tvshows", "TV Shows", "tv", "general"), "movies": ("movies", "Movies", "movie", "general"),
         "ads": ("ads", "Adverts", "advert", "general"), "tvsports": ("tvsports", "Sport", "tv", "sport"),
         "music%20videos": ("musicvideos", "Music videos", "music", "general")}
host = os.environ["PITV_NAS_HOST"]
out = []
for share in os.environ["PITV_SHARES"].split():
    sid, name, stype, cat = known.get(share, (share.replace("%20", ""), share.replace("%20", " "), "tv", "general"))
    out.append({"id": sid, "name": name, "type": stype, "category": cat, "root": f"/mnt/{share.replace('%20', '')}",
                "remote": f"smb://{host}/{share}/", "enabled": True})
print(json.dumps(out))
PY
chmod 644 /etc/pitv/nas-sources.json

log "Boot tuning (DISPLAY_MODE=$DISPLAY_MODE: hdmi576 | composite | hdmi43 | hdmi)"
DISPLAY_MODE="$DISPLAY_MODE" "$SRC_DIR/setup/boot-trim.sh" || true

# Remember this install's choices for upgrades (values are quoted; nothing secret is stored here).
mkdir -p /etc/pitv
{
  printf 'SAVED_NAS_HOST=%q\n' "$NAS_HOST"
  printf 'SAVED_SHARES=%q\n' "$SHARES"
  printf 'SAVED_CACHE_DIR=%q\n' "$CACHE_DIR"
  printf 'SAVED_DISPLAY_MODE=%q\n' "$DISPLAY_MODE"
} > "$ENV_FILE"
chmod 644 "$ENV_FILE"

log "Done. Start with: systemctl start pitv-player pitv-web   (web UI on http://$(hostname -I | awk '{print $1}')/ )"
echo "First run: open the web UI and set the admin password. pitv_content indexes the NAS on its first run; PiTV imports the catalogue and builds the schedule from it."
