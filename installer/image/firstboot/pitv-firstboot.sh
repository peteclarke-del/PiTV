#!/usr/bin/env bash
# PiTV first boot. Runs once, as root, at the end of DietPi's automated first-run setup
# (/boot/Automation_Custom_Script.sh). Everything it does is idempotent and logged to the
# install log on the work partition. On success it marks the boot done and starts the apps;
# every later boot is a normal boot.
#
# Secrets (NAS password, user passwords, Wi-Fi key) are read from pitv-install.json and
# written only to root-only files; they never go on a command line or into the install log,
# and the JSON and DietPi's copies are scrubbed from the FAT partition at the end.
set -uo pipefail

BOOT=/boot
CONF="$BOOT/pitv-install.json"
SRC_PITV=/opt/pitv-src
SRC_CONTENT=/opt/pitv-content-src
WORK=/work
LOG_DIR="$WORK/install"
LOG="$LOG_DIR/install.log"
DONE_MARK="$WORK/install/firstboot.done"
DISK=/dev/mmcblk0

json() { python3 -c "import json,sys; d=json.load(open('$CONF')); v=d
for k in sys.argv[1].split('.'):
    v=v.get(k) if isinstance(v,dict) else None
print('' if v is None else (json.dumps(v) if isinstance(v,(dict,list)) else v))" "$1" 2>/dev/null; }

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') firstboot: $*" | tee -a "$LOG" >/dev/console 2>/dev/null || echo "$(date '+%Y-%m-%d %H:%M:%S') firstboot: $*" >> "$LOG"; }
fail() { log "ERROR: $*"; log "first boot FAILED; fix the problem and reboot to retry"; exit 1; }

[ -f "$CONF" ] || { mkdir -p /tmp/install; echo "no $CONF" > /tmp/install/error; exit 1; }
MODE="$(json mode)"; MODE="${MODE:-normal}"

# ---- 1. Partitions: system to a fixed size, the rest of the card becomes /work ---------------
mkdir -p "$WORK"
SYS_GB="$(json system_partition_gb)"; SYS_GB="${SYS_GB:-8}"
if ! lsblk -no NAME "${DISK}p3" >/dev/null 2>&1; then
  mkdir -p /tmp/install; LOG=/tmp/install/install.log
  log "mode=$MODE: sizing system partition to ${SYS_GB} GB and creating the work partition"
  parted -s "$DISK" resizepart 2 "${SYS_GB}GiB" || fail "could not resize the system partition"
  resize2fs "${DISK}p2" || fail "could not grow the root filesystem"
  parted -s -a optimal "$DISK" mkpart primary ext4 "${SYS_GB}GiB" 100% || fail "could not create the work partition"
  partprobe "$DISK"; sleep 2
  mkfs.ext4 -q -F -L PITVWORK "${DISK}p3" || fail "could not format the work partition"
fi
grep -q " $WORK " /etc/fstab || echo "LABEL=PITVWORK $WORK ext4 defaults,noatime,nofail,x-systemd.device-timeout=10 0 2" >> /etc/fstab
mountpoint -q "$WORK" || mount "$WORK" || fail "could not mount the work partition"
mkdir -p "$LOG_DIR"
[ -f /tmp/install/install.log ] && cat /tmp/install/install.log >> "$LOG" && rm -f /tmp/install/install.log
LOG="$LOG_DIR/install.log"
log "==== first boot ($MODE) $(cat /etc/dietpi/.version 2>/dev/null | tr '\n' ' ') ===="

# Logs, databases and state live on /work so they can never fill the system partition.
mkdir -p "$WORK/pitv" "$WORK/pitv-content" "$WORK/log/journal"
if [ ! -L /var/lib/pitv ]; then rm -rf /var/lib/pitv; ln -s "$WORK/pitv" /var/lib/pitv; fi
if [ ! -L /var/log/journal ]; then rm -rf /var/log/journal; ln -s "$WORK/log/journal" /var/log/journal; fi
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=persistent\nSystemMaxUse=200M\nRuntimeMaxUse=20M\n' > /etc/systemd/journald.conf.d/pitv.conf

# ---- 2. Work drive (USB HDD): prepare, never wipe unless mode=clean ---------------------------
CACHE_DIR="$(json work_drive.cache_dir)"; CACHE_DIR="${CACHE_DIR:-/mnt/cache/pitv}"
CACHE_ROOT="$(dirname "$CACHE_DIR")"
DEV="$(json work_drive.device)"; DEV="${DEV:-auto}"
if [ "$DEV" = "auto" ]; then
  DEV="$(lsblk -dpno NAME,TYPE,TRAN 2>/dev/null | awk '$2=="disk" && $3=="usb" {print $1; exit}')"
fi
if [ -n "$DEV" ] && [ -b "$DEV" ]; then
  PART="$(lsblk -pno NAME,TYPE "$DEV" | awk '$2=="part" {print $1; exit}')"
  if [ "$MODE" = "clean" ] || [ -z "$PART" ]; then
    log "work drive $DEV: $( [ "$MODE" = "clean" ] && echo 'CLEAN install: wiping' || echo 'no partition found: formatting')"
    parted -s "$DEV" mklabel gpt mkpart primary ext4 0% 100% || fail "could not partition the work drive"
    partprobe "$DEV"; sleep 2
    PART="$(lsblk -pno NAME,TYPE "$DEV" | awk '$2=="part" {print $1; exit}')"
    mkfs.ext4 -q -F -L PITVCACHE "$PART" || fail "could not format the work drive"
  else
    log "work drive $DEV ($PART): keeping existing content"
  fi
  FSTYPE="$(lsblk -no FSTYPE "$PART")"
  grep -q " $CACHE_ROOT " /etc/fstab || echo "$PART $CACHE_ROOT $FSTYPE defaults,noatime,nofail,x-systemd.device-timeout=15 0 2" >> /etc/fstab
  mkdir -p "$CACHE_ROOT"; mountpoint -q "$CACHE_ROOT" || mount "$CACHE_ROOT" || log "WARNING: could not mount the work drive; the apps will run without a cache until it appears"
else
  log "WARNING: no USB work drive found; the apps will run without a cache"
fi
if mountpoint -q "$CACHE_ROOT"; then
  mkdir -p "$CACHE_DIR"/{acquired/{tvshows,tvsports,movies,ads,"music videos"},logs,reports}
else
  log "WARNING: $CACHE_ROOT is not mounted; not creating $CACHE_DIR on the SD card"
fi

# ---- 3. Users ---------------------------------------------------------------------------------
MU="$(json maintenance_user.name)"
if [ -n "$MU" ] && ! id "$MU" >/dev/null 2>&1; then
  useradd -m -s /bin/bash -G sudo,video,audio,input "$MU" && log "created maintenance user $MU"
fi
if [ -n "$MU" ]; then
  MP="$(json maintenance_user.password)"; [ -n "$MP" ] && echo "$MU:$MP" | chpasswd
  KEYS="$(python3 -c "import json; print('\n'.join(json.load(open('$CONF')).get('maintenance_user',{}).get('ssh_authorized_keys',[])))")"
  if [ -n "$KEYS" ]; then mkdir -p "/home/$MU/.ssh"; echo "$KEYS" > "/home/$MU/.ssh/authorized_keys"; chmod 700 "/home/$MU/.ssh"; chmod 600 "/home/$MU/.ssh/authorized_keys"; chown -R "$MU:$MU" "/home/$MU/.ssh"; fi
fi

# ---- 4. PiTV -----------------------------------------------------------------------------------
NAS_HOST="$(json nas.host)"; NAS_USER="$(json nas.username)"; NAS_PASS="$(json nas.password)"
SHARES="$(python3 -c "import json; print(' '.join(json.load(open('$CONF')).get('nas',{}).get('shares',[])))")"
mkdir -p /etc/pitv
(umask 077; printf 'username=%s\npassword=%s\n' "$NAS_USER" "$NAS_PASS" > /etc/pitv/smb-credentials)
chown root:root /etc/pitv/smb-credentials; chmod 600 /etc/pitv/smb-credentials
log "installing PiTV (display $(json display_mode), shares: $SHARES)"
( cd "$SRC_PITV" && NAS_HOST="$NAS_HOST" SHARES="$SHARES" CACHE_DIR="$CACHE_DIR" DISPLAY_MODE="$(json display_mode)" \
  ./setup/install.sh ) >> "$LOG" 2>&1 || fail "PiTV install failed (see above)"
ADMIN_PW="$(json pitv.admin_password)"
if [ -n "$ADMIN_PW" ]; then
  # Through the environment (runuser keeps it), never as an argument where ps would show it.
  PITV_ADMIN_PASSWORD="$ADMIN_PW" PITV_DATA=/var/lib/pitv runuser -u pitv -- /opt/pitv/.venv/bin/python - <<'PY' >> "$LOG" 2>&1
import os
from pitv import db as dbm
from pitv.config import load_config
from pitv.web.auth import hash_password
cfg = load_config(); conn = dbm.connect(cfg.db_path); dbm.init_db(conn)
with dbm.tx(conn):
    dbm.set_setting(conn, "admin_password_hash", hash_password(os.environ["PITV_ADMIN_PASSWORD"]))
print("admin password set")
PY
fi
unset ADMIN_PW NAS_PASS MP

# ---- 5. pitv_content ---------------------------------------------------------------------------
if [ -d "$SRC_CONTENT" ] && [ -x "$SRC_CONTENT/setup/install-on-pi.sh" ]; then
  log "installing pitv_content"
  # The NAS shares become pitv_content's sources (seeded on its first install only).
  ( cd "$SRC_CONTENT" && CACHE_DIR="$CACHE_DIR" WORK_DIR="$WORK/pitv-content" PITV_URL="http://127.0.0.1" \
    NAS_SOURCES="$(cat /etc/pitv/nas-sources.json 2>/dev/null || echo '[]')" INSTALL_LOG="$LOG" \
    YOUTUBE_COOKIES_FILE="$(json pitv_content.youtube_cookies_file)" ./setup/install-on-pi.sh ) >> "$LOG" 2>&1 \
    || log "WARNING: pitv_content install failed (see above); PiTV will run without it"
else
  log "WARNING: pitv_content sources not present in the image; skipping"
fi

# ---- 6. Done -----------------------------------------------------------------------------------
touch "$DONE_MARK"
log "first boot complete; rebooting into normal operation"
cp "$LOG" "$BOOT/pitv-install.log" 2>/dev/null || true
sed -i 's/^\(AUTO_SETUP_CUSTOM_SCRIPT_EXEC\)=.*/\1=0/' "$BOOT/dietpi.txt"
# Nothing secret may stay on the FAT partition: the install JSON, DietPi's Wi-Fi key file and
# the global password it was given (reset to DietPi's shipped placeholder).
sed -i 's/^\(AUTO_SETUP_GLOBAL_PASSWORD\)=.*/\1=dietpi/' "$BOOT/dietpi.txt"
rm -f "$CONF" "$BOOT/dietpi-wifi.txt"
sync
reboot
