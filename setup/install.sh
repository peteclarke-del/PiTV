#!/usr/bin/env bash
# PiTV installer for Raspberry Pi OS Lite (64-bit, Bookworm). Run as root on the Pi:
#   sudo ./setup/install.sh
# Idempotent: safe to re-run after `git pull`.
set -euo pipefail

NAS_HOST="${NAS_HOST:-synologynas}"
# Share names as on the NAS; a space is written as %20 (mounted at /mnt/<name without spaces>).
SHARES="${SHARES:-tvshows movies ads tvsports music%20videos}"
mount_name() { echo "$1" | sed 's/%20//g; s/ //g'; }
share_name() { echo "$1" | sed 's/%20/ /g'; }
INSTALL_DIR=/opt/pitv
DATA_DIR=/var/lib/pitv
CACHE_DIR="${CACHE_DIR:-/mnt/cache/pitv}"
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
chown -R pitv:pitv "$INSTALL_DIR"

log "SMB credentials"
if [ ! -f /etc/pitv/smb-credentials ]; then
  read -rp "NAS username for $NAS_HOST: " smb_user
  read -rsp "NAS password: " smb_pass; echo
  printf 'username=%s\npassword=%s\n' "$smb_user" "$smb_pass" > /etc/pitv/smb-credentials
fi
chmod 600 /etc/pitv/smb-credentials

log "CIFS automounts for: $SHARES"
for share in $SHARES; do
  m="$(mount_name "$share")"; n="$(share_name "$share")"
  mkdir -p "/mnt/$m"
  sed -e "s|@NAS@|$NAS_HOST|g" -e "s|@SHARE@|$n|g" -e "s|@MOUNT@|$m|g" "$SRC_DIR/systemd/mnt-share.mount.template" > "/etc/systemd/system/mnt-$m.mount"
  sed -e "s|@MOUNT@|$m|g" "$SRC_DIR/systemd/mnt-share.automount.template" > "/etc/systemd/system/mnt-$m.automount"
done

log "Local cache directory ($CACHE_DIR)"
mkdir -p "$CACHE_DIR"/acquired/{tvshows,tvsports,movies,ads,"music videos"} "$CACHE_DIR"/logs "$CACHE_DIR"/reports && chown -R pitv:pitv "$CACHE_DIR" || true

log "Export the cache over NFS so pitv_content on the desktop can fill it (EXPORT_TO=${EXPORT_TO:-192.168.0.0/24})"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nfs-kernel-server >/dev/null
CACHE_ROOT="$(dirname "$CACHE_DIR")"
grep -q "^$CACHE_ROOT " /etc/exports 2>/dev/null || echo "$CACHE_ROOT ${EXPORT_TO:-192.168.0.0/24}(rw,sync,no_subtree_check,all_squash,anonuid=$(id -u pitv),anongid=$(id -g pitv))" >> /etc/exports
exportfs -ra || true
systemctl enable --now nfs-server >/dev/null 2>&1 || true

log "systemd services"
cp "$SRC_DIR/systemd/pitv-player.service" "$SRC_DIR/systemd/pitv-web.service" "$SRC_DIR/systemd/pitv-splash.service" /etc/systemd/system/
cat > /etc/sudoers.d/pitv <<'SUDO'
pitv ALL=(root) NOPASSWD: /usr/bin/systemctl restart pitv-player, /usr/bin/systemctl stop pitv-player, /usr/bin/systemctl start pitv-player, /usr/bin/systemctl restart pitv-web, /usr/bin/systemctl start pitv-content.service, /usr/bin/systemctl reboot
SUDO
chmod 440 /etc/sudoers.d/pitv
systemctl daemon-reload
for share in $SHARES; do systemctl enable --now "mnt-$(mount_name "$share").automount"; done
systemctl enable pitv-splash pitv-player pitv-web

log "Database, sources and cache settings"
sudo -u pitv PITV_DATA="$DATA_DIR" "$INSTALL_DIR/.venv/bin/python" - <<PY
from pitv import db as dbm
from pitv.config import load_config
cfg = load_config(); cfg.ensure_dirs()
conn = dbm.connect(cfg.db_path); dbm.init_db(conn)
with dbm.tx(conn):
    for stype, name, share, cat in (("tv", "TV Shows", "tvshows", "general"), ("movie", "Movies", "movies", "general"),
                                    ("advert", "Adverts", "ads", "general"), ("tv", "Sport", "tvsports", "sport"),
                                    ("music", "Music videos", "music%20videos", "general")):
        if share in "$SHARES".split():
            path = f"/mnt/{share.replace('%20', '')}"
            if not conn.execute("SELECT 1 FROM sources WHERE path = ?", (path,)).fetchone():
                conn.execute("INSERT INTO sources(type, name, path, remote, category) VALUES (?,?,?,?,?)",
                             (stype, name, path, f"smb://$NAS_HOST/{share}/", cat))
    if not dbm.get_setting(conn, "cache_dir"):
        dbm.set_setting(conn, "cache_dir", "$CACHE_DIR")
    # Everything pitv_content downloads lands under the cache, never on the NAS; register those folders.
    acq = "$CACHE_DIR/acquired"
    for stype, name, sub, cat in (("tv", "Acquired shows", "tvshows", "general"), ("tv", "Acquired sport", "tvsports", "sport"),
                                  ("movie", "Acquired movies", "movies", "general"), ("advert", "Acquired adverts", "ads", "general"),
                                  ("music", "Acquired music videos", "music videos", "general")):
        path = f"{acq}/{sub}"
        if not conn.execute("SELECT 1 FROM sources WHERE path = ?", (path,)).fetchone():
            conn.execute("INSERT INTO sources(type, name, path, category) VALUES (?,?,?,?)", (stype, name, path, cat))
    if not dbm.get_setting(conn, "acquire_dir"):
        dbm.set_setting(conn, "acquire_dir", acq)
print("sources:", [dict(r) for r in conn.execute("SELECT type, path FROM sources")])
PY

log "Boot tuning (DISPLAY_MODE=${DISPLAY_MODE:-composite}: composite | hdmi43 | hdmi)"
DISPLAY_MODE="${DISPLAY_MODE:-composite}" "$SRC_DIR/setup/boot-trim.sh" || true

log "Done. Start with: systemctl start pitv-player pitv-web   (web UI on http://$(hostname -I | awk '{print $1}')/ )"
echo "First run: open the web UI, set the admin password, check Sources, run Scan, then Build schedule."
