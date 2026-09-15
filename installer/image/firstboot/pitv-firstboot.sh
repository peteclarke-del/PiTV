#!/usr/bin/env bash
# PiTV first boot, installed as /boot/Automation_Custom_Script.sh. DietPi runs it once, as
# root, at the end of its automated first-run setup. DietPi records its own setup as finished
# before it starts the script and never starts it again, so a run that failed or lost power is
# repeated by hand; every step is idempotent for that reason:
#   sudo bash /boot/Automation_Custom_Script.sh
#
# The installer wrote pitv-install.json to the FAT partition and added partition 3 after the
# system partition, and DietPi's first boot grew the root filesystem up to it. This script
# formats partition 3 as /work, prepares the USB work drive, creates the maintenance user and
# installs both apps. Secrets from the JSON reach files only root can read, or programs through
# stdin and the environment; never a command line, where ps would show them, and never the log.
# The JSON is deleted at the end.
set -Eeuo pipefail

WORK=/work
LOG_DIR="$WORK/install"
LOG=/run/pitv-firstboot.log     # until /work is mounted; then appended to $LOG_DIR/install.log
SRC_PITV=/opt/pitv-src
SRC_CONTENT=/opt/pitv-content-src

log() {
  local line
  line="$(date '+%Y-%m-%d %H:%M:%S') firstboot: $*"
  printf '%s\n' "$line" >> "$LOG"
  printf '%s\n' "$line" 2>/dev/null > /dev/console || true
}
fail() {
  log "ERROR: $*"
  log "first boot FAILED; fix the cause, then run: sudo bash /boot/Automation_Custom_Script.sh"
  exit 1
}
# $BASH_COMMAND is the command as written, before expansion, so no secret reaches the log.
trap 'fail "line $LINENO: \"$BASH_COMMAND\" exited with status $?"' ERR

# fstab_set MOUNTPOINT LINE: replace MOUNTPOINT's entry, through a rename so a power cut
# leaves the old fstab or the new one.
fstab_set() {
  local tmp
  tmp="$(mktemp /etc/fstab.XXXXXX)"
  awk -v mp="$1" '$2 != mp' /etc/fstab > "$tmp"
  printf '%s\n' "$2" >> "$tmp"
  chmod 644 "$tmp"
  mv -f "$tmp" /etc/fstab
}

# The FAT partition is /boot/firmware on current DietPi images and /boot on older ones.
CONF=""
for dir in /boot/firmware /boot; do
  if [ -f "$dir/pitv-install.json" ]; then CONF="$dir/pitv-install.json"; break; fi
done
[ -n "$CONF" ] || fail "no pitv-install.json on the boot partition; write the card with pitv-installer"
FAT="$(dirname "$CONF")"

# Every value used below, checked against the installer's rules (installer/cli/config.go)
# again, because the JSON on the FAT partition can be edited by hand. Values arrive
# NUL-separated and are assigned with printf -v, so the shell never parses them.
read_config() {
  python3 - "$CONF" <<'PY'
import json, posixpath, re, sys

try:
    with open(sys.argv[1], encoding="utf-8") as f:
        conf = json.load(f)
except (OSError, ValueError) as e:
    sys.exit(f"pitv-install.json: {e}")

bad = []

def get(path, default):
    v = conf
    for key in path.split("."):
        v = v.get(key) if isinstance(v, dict) else None
    return default if v is None else v

def text(path, default="", pattern=None):
    v = get(path, default)
    if not isinstance(v, str) or any(ch in v for ch in "\r\n\0") or (pattern and not re.fullmatch(pattern, v)):
        bad.append(path)
        return ""
    return v

def words(path, default, pattern):
    v = get(path, default)
    if not isinstance(v, list) or not all(isinstance(w, str) and re.fullmatch(pattern, w) for w in v):
        bad.append(path)
        return []
    return v

out = {
    "MODE": text("mode", "normal", r"clean|normal"),
    "CACHE_DIR": text("work_drive.cache_dir", "/mnt/cache/pitv", r"/(mnt|media)/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)+"),
    "WORK_DEV": text("work_drive.device", "auto", r"auto|/dev/[A-Za-z0-9/_-]+"),
    "MU": text("maintenance_user.name", "", r"[a-z_][a-z0-9_-]{0,31}"),
    "MU_PW": text("maintenance_user.password"),
    "MU_KEYS": "\n".join(words("maintenance_user.ssh_authorized_keys", [], r"(ssh-|ecdsa-|sk-)[^\r\n\0]+")),
    "NAS_HOST": text("nas.host", "synologynas", r"[A-Za-z0-9]([A-Za-z0-9.-]{0,252}[A-Za-z0-9])?"),
    "NAS_USER": text("nas.username", "", r"[A-Za-z_][A-Za-z0-9_-]{0,31}"),
    "NAS_PASS": text("nas.password"),
    "SHARES": " ".join(words("nas.shares", ["tvshows", "movies", "ads", "tvsports", "music%20videos"], r"([A-Za-z0-9._-]|%20)+")),
    "DISPLAY_MODE": text("display_mode", "hdmi576", r"hdmi576|composite|hdmi43|hdmi"),
    "ADMIN_PW": text("pitv.admin_password"),
    "COOKIES": text("pitv_content.youtube_cookies_file", "", r"|/[A-Za-z0-9._/-]*"),
}
if out["MU"] in ("", "root", "dietpi", "pitv"):
    bad.append("maintenance_user.name")
if not out["MU_PW"]:
    bad.append("maintenance_user.password")
if posixpath.normpath(out["CACHE_DIR"]) != out["CACHE_DIR"]:
    bad.append("work_drive.cache_dir")
if bad:
    sys.exit("pitv-install.json: invalid or missing " + ", ".join(sorted(set(bad))))
for key, value in out.items():
    sys.stdout.write(f"{key}={value}\0")
sys.stdout.write("OK=1\0")
PY
}
CFG_OK=""
while IFS= read -r -d '' kv; do
  printf -v "CFG_${kv%%=*}" '%s' "${kv#*=}"
done < <(read_config 2>>"$LOG" || true)
[ "$CFG_OK" = 1 ] || fail "$CONF could not be used (see the line above)"
log "==== first boot ($CFG_MODE) ===="

# ---- 1. Maintenance user: first, so a later failure still leaves a way in ------------------
MU="$CFG_MU"
if ! id "$MU" >/dev/null 2>&1; then
  useradd -m -s /bin/bash -G sudo,video,audio,input "$MU"
  log "created maintenance user $MU"
fi
printf '%s:%s\n' "$MU" "$CFG_MU_PW" | chpasswd
MU_HOME="$(getent passwd "$MU" | cut -d: -f6)"
if [ -n "$CFG_MU_KEYS" ]; then
  install -d -m 700 -o "$MU" -g "$MU" "$MU_HOME/.ssh"
  (umask 077; printf '%s\n' "$CFG_MU_KEYS" > "$MU_HOME/.ssh/authorized_keys")
  chown "$MU:$MU" "$MU_HOME/.ssh/authorized_keys"
fi
# Only the maintenance user logs in over SSH. root and dietpi carry the random DietPi global
# password, and dietpi has password-less sudo. The policy is checked before it is applied, so a
# mistake can never lock SSH out; it takes effect at the latest with the reboot at the end.
install -d -m 755 /etc/ssh/sshd_config.d
printf 'AllowUsers %s\nPermitRootLogin no\n' "$MU" > /etc/ssh/sshd_config.d/10-pitv.conf
if sshd -t 2>>"$LOG"; then
  systemctl try-reload-or-restart ssh.service || log "WARNING: ssh did not reload; the SSH policy applies after the reboot"
else
  rm -f /etc/ssh/sshd_config.d/10-pitv.conf
  log "WARNING: sshd rejected the PiTV SSH policy; SSH keeps DietPi's defaults"
fi

# ---- 2. /work: partition 3 of the system card ------------------------------------------------
DISK="/dev/$(lsblk -no PKNAME "$(findmnt -no SOURCE /)")"
case "$DISK" in
  *[0-9]) WORK_PART="${DISK}p3" ;;   # mmcblk0p3, nvme0n1p3
  *) WORK_PART="${DISK}3" ;;         # sda3
esac
[ -b "$WORK_PART" ] || fail "$DISK has no partition 3; write the card with pitv-installer, which creates it"
if ! mountpoint -q "$WORK"; then
  # A first boot always follows a fresh card write, so anything in partition 3 is left over
  # from an earlier install. Once /work is mounted (fstab below), a re-run keeps it.
  log "formatting $WORK_PART as $WORK"
  mkfs.ext4 -q -F -L PITVWORK "$WORK_PART"
fi
fstab_set "$WORK" "UUID=$(blkid -p -s UUID -o value "$WORK_PART") $WORK ext4 defaults,noatime,nofail,x-systemd.device-timeout=10s 0 2"
systemctl daemon-reload
mkdir -p "$WORK"
mountpoint -q "$WORK" || mount "$WORK"
mkdir -p "$LOG_DIR"
cat "$LOG" >> "$LOG_DIR/install.log"
rm -f "$LOG"
LOG="$LOG_DIR/install.log"

# Databases, state and the journal live on /work so they can never fill the system partition.
# Bind mounts rather than symlinks: services can depend on them (RequiresMountsFor), and they
# hold even where /var/log is a tmpfs.
mkdir -p "$WORK/pitv" "$WORK/pitv-content" "$WORK/log/journal"
for pair in "$WORK/pitv:/var/lib/pitv" "$WORK/log/journal:/var/log/journal"; do
  src="${pair%%:*}" dst="${pair#*:}"
  if [ -L "$dst" ]; then rm -f "$dst"; fi   # a symlink from an earlier version of this script
  mkdir -p "$dst"
  fstab_set "$dst" "$src $dst none bind,nofail 0 0"
done
systemctl daemon-reload
for dst in /var/lib/pitv /var/log/journal; do mountpoint -q "$dst" || mount "$dst"; done
mkdir -p /etc/systemd/journald.conf.d
# Overrides boot-trim.sh's 50-pitv.conf, which is sized for a journal on the system partition.
printf '[Journal]\nStorage=persistent\nSystemMaxUse=200M\nRuntimeMaxUse=20M\n' > /etc/systemd/journald.conf.d/60-pitv-work.conf

# ---- 3. USB work drive: prepared, never wiped unless mode=clean -------------------------------
CACHE_ROOT="$(dirname "$CFG_CACHE_DIR")"
DEV="$CFG_WORK_DEV"
if [ "$DEV" = auto ]; then
  # A USB drive can enumerate a few seconds after boot; give it up to 30 s before deciding
  # there is none, since pitv_content will not install without its cache drive.
  for _ in $(seq 1 15); do
    DEV="$(lsblk -dpno NAME,TYPE,TRAN | awk -v sys="$DISK" '$2=="disk" && $3=="usb" && $1!=sys {print $1; exit}')"
    [ -z "$DEV" ] || break
    udevadm settle --timeout=2 || true
    sleep 2
  done
fi
[ "$DEV" != "$DISK" ] || fail "work_drive.device $DEV is the system card"
if [ -n "$DEV" ] && [ -b "$DEV" ]; then
  first_part() { lsblk -lnpo NAME,TYPE "$DEV" | awk '$2=="part" {print $1; exit}'; }
  PART="$(first_part)"
  if [ "$CFG_MODE" = clean ] || [ -z "$PART" ]; then
    if [ "$CFG_MODE" = clean ]; then log "work drive $DEV: CLEAN install, wiping"; else log "work drive $DEV: no partition, formatting"; fi
    lsblk -lnpo MOUNTPOINT "$DEV" | while read -r mp; do [ -z "$mp" ] || umount "$mp"; done
    parted -s "$DEV" mklabel gpt mkpart primary ext4 0% 100%
    partprobe "$DEV"
    udevadm settle
    PART="$(first_part)"
    [ -n "$PART" ] || fail "no partition on $DEV after partitioning it"
    mkfs.ext4 -q -F -L PITVCACHE "$PART"
  else
    log "work drive $DEV ($PART): keeping existing content"
  fi
  fstab_set "$CACHE_ROOT" "UUID=$(blkid -p -s UUID -o value "$PART") $CACHE_ROOT $(blkid -p -s TYPE -o value "$PART") defaults,noatime,nofail,x-systemd.device-timeout=15s 0 2"
  systemctl daemon-reload
  mkdir -p "$CACHE_ROOT"
  mountpoint -q "$CACHE_ROOT" || mount "$CACHE_ROOT" || log "WARNING: could not mount the work drive; the apps run without a cache until it appears"
else
  log "WARNING: no USB work drive found; the apps run without a cache"
fi

# ---- 4. PiTV ----------------------------------------------------------------------------------
# install.sh writes the NAS credentials file and creates the cache folders (on a mounted drive only).
log "installing PiTV (display $CFG_DISPLAY_MODE, shares: $CFG_SHARES)"
( cd "$SRC_PITV" && NAS_HOST="$CFG_NAS_HOST" NAS_USER="$CFG_NAS_USER" NAS_PASS="$CFG_NAS_PASS" SHARES="$CFG_SHARES" \
    CACHE_DIR="$CFG_CACHE_DIR" DISPLAY_MODE="$CFG_DISPLAY_MODE" bash ./setup/install.sh ) < /dev/null >> "$LOG" 2>&1 \
  || fail "PiTV install failed (see above)"
if [ -n "$CFG_ADMIN_PW" ]; then
  # On stdin: an argument would show in ps, an environment variable in /proc/<pid>/environ.
  printf '%s' "$CFG_ADMIN_PW" | runuser -u pitv -- env PITV_DATA=/var/lib/pitv /opt/pitv/.venv/bin/python -c '
import sys
from pitv import db as dbm
from pitv.config import load_config
from pitv.web.auth import hash_password
conn = dbm.connect(load_config().db_path)
dbm.init_db(conn)
with dbm.tx(conn):
    dbm.set_setting(conn, "admin_password_hash", hash_password(sys.stdin.read()))
print("admin password set")' >> "$LOG" 2>&1
fi

# ---- 5. pitv_content ---------------------------------------------------------------------------
if ! mountpoint -q "$CACHE_ROOT"; then
  # pitv_content refuses a cache on the SD card; installing it now would only fail.
  log "WARNING: pitv_content not installed: the work drive is not mounted at $CACHE_ROOT. Plug it in, then run: sudo bash /boot/Automation_Custom_Script.sh"
elif [ -f "$SRC_CONTENT/setup/install-on-pi.sh" ]; then
  log "installing pitv_content"
  # The NAS shares, as written by install.sh, become pitv_content's sources on its first install,
  # and it mounts them with the NAS login install.sh keeps root-only (contract section 4).
  # shellcheck disable=SC2094 # the installer and this redirection both append to the log
  ( cd "$SRC_CONTENT" && CACHE_DIR="$CFG_CACHE_DIR" WORK_DIR="$WORK/pitv-content" PITV_URL=http://127.0.0.1 \
      NAS_SOURCES="$(< /etc/pitv/nas-sources.json)" NAS_CREDENTIALS_FILE=/etc/pitv/smb-credentials \
      INSTALL_LOG="$LOG" YOUTUBE_COOKIES_FILE="$CFG_COOKIES" \
      bash ./setup/install-on-pi.sh ) < /dev/null >> "$LOG" 2>&1 \
    || log "WARNING: pitv_content install failed (see above); PiTV runs without it"
else
  log "WARNING: pitv_content sources not present in the image; skipping"
fi

# ---- 6. Done -----------------------------------------------------------------------------------
log "first boot complete; rebooting into normal operation"
cp "$LOG" "$FAT/pitv-install.log"
# Nothing secret may stay on the FAT partition, which any computer the card is put in can read.
# DietPi has already moved its own files off it on current images; on older ones /boot is FAT.
rm -f "$CONF" "$FAT/dietpi-wifi.txt"
sed -i 's/^\(AUTO_SETUP_GLOBAL_PASSWORD\)=.*/\1=dietpi/' /boot/dietpi.txt
sync
reboot
