#!/usr/bin/env bash
# Build the PiTV SD-card image from a DietPi base. Linux only, needs root (loop mounts).
#
#   sudo installer/image/build.sh [--base DietPi_RPi234-ARMv8-Bookworm.img.xz] [--content-src ../PiTV_content] [--out dist/]
#
# The image is DietPi + both apps' source trees + the first-boot hook; nothing is installed
# at build time (no chroot/qemu needed): DietPi's automated first run installs packages and
# runs installer/image/firstboot/pitv-firstboot.sh, which partitions the card, prepares the
# work drive and provisions both apps from /boot/pitv-install.json written by the installer.
#
# The base image is only used with a good detached signature (<image>.asc beside it) from
# DietPi's release key, identified by its pinned fingerprint, so neither a tampered download
# nor a truncated one can end up on a card. The key itself is fetched from DIETPI_KEY_URL, or
# read from DIETPI_KEY_FILE for an offline build; where it comes from does not matter, since
# only a key with the pinned fingerprint is accepted.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
BASE_URL="${BASE_URL:-https://dietpi.com/downloads/images/DietPi_RPi234-ARMv8-Bookworm.img.xz}"
# MichaIng <micha@dietpi.com>, DietPi's release signing key. Checked on 2026-09-15 against both
# keyserver.ubuntu.com and https://github.com/MichaIng.gpg.
DIETPI_KEY_FPR=974105F494304547F1A9E5E00442B9ADE65643FE
DIETPI_KEY_URL="${DIETPI_KEY_URL:-https://github.com/MichaIng.gpg}"
DIETPI_KEY_FILE="${DIETPI_KEY_FILE:-}"
BASE=""
CONTENT_SRC="${CONTENT_SRC:-$ROOT/../../PiTV_content}"
OUT="$ROOT/dist"

die() { echo "build.sh: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE="${2:?--base needs a file}"; shift 2 ;;
    --content-src) CONTENT_SRC="${2:?--content-src needs a directory}"; shift 2 ;;
    --out) OUT="${2:?--out needs a directory}"; shift 2 ;;
    *) die "unknown option $1" ;;
  esac
done
[ "$(id -u)" -eq 0 ] || die "run with sudo (loop mounts)"
for t in xz losetup parted e2fsck resize2fs rsync curl gpg sha256sum; do
  command -v "$t" >/dev/null || die "missing $t"
done

# fetch URL DEST: through a .part file, so an interrupted transfer never passes for a complete one.
fetch() { curl -fL --proto '=https' --tlsv1.2 -o "$2.part" "$1" && mv -f "$2.part" "$2"; }

# verify FILE: FILE.asc must be a good signature over FILE by the pinned DietPi key.
verify() {
  [ -f "$1" ] && [ -f "$1.asc" ] || return 1
  local gnupg status=""
  gnupg="$(mktemp -d)"
  if [ -n "$DIETPI_KEY_FILE" ]; then
    gpg --homedir "$gnupg" --batch --quiet --import "$DIETPI_KEY_FILE" 2>/dev/null
  else
    curl -fsSL --proto '=https' "$DIETPI_KEY_URL" | gpg --homedir "$gnupg" --batch --quiet --import 2>/dev/null
  fi || { rm -rf "$gnupg"; die "could not load DietPi's signing key"; }
  status="$(gpg --homedir "$gnupg" --batch --status-fd 1 --verify "$1.asc" "$1" 2>/dev/null)" || true
  rm -rf "$gnupg"
  # VALIDSIG ends with the fingerprint of the primary key that made the signature.
  grep -Eq "^\[GNUPG:\] VALIDSIG .* $DIETPI_KEY_FPR\$" <<< "$status"
}

mkdir -p "$OUT/work"
if [ -z "$BASE" ]; then
  BASE="$OUT/work/$(basename "$BASE_URL")"
  if ! verify "$BASE"; then
    echo "==> downloading $BASE_URL"
    fetch "$BASE_URL" "$BASE"
    fetch "$BASE_URL.asc" "$BASE.asc"
    verify "$BASE" || die "$BASE: the DietPi signature does not verify"
  fi
else
  verify "$BASE" || die "$BASE: no good DietPi signature (put DietPi's $(basename "$BASE").asc beside it)"
fi
echo "==> base $BASE (signature verified)"

VERSION="$(git -C "$ROOT" describe --tags --always --dirty 2>/dev/null || echo dev)"
IMG="$OUT/work/pitv-$VERSION.img"
LOOP=""
MNT=""
cleanup() {
  if [ -n "$MNT" ]; then
    umount -q "$MNT/boot/firmware" 2>/dev/null || true
    umount -q "$MNT/boot" 2>/dev/null || true
    umount -q "$MNT" 2>/dev/null || true
    rmdir "$MNT" 2>/dev/null || true
  fi
  if [ -n "$LOOP" ]; then losetup -d "$LOOP" 2>/dev/null || true; fi
}
trap cleanup EXIT

xz -dc "$BASE" > "$IMG"
# Room for the sources; the first boot sizes the partitions on the real card.
truncate -s +512M "$IMG"
parted -s "$IMG" resizepart 2 100%
LOOP="$(losetup --find --show --partscan "$IMG")"
rc=0
e2fsck -fp "${LOOP}p2" >/dev/null || rc=$?
[ "$rc" -lt 4 ] || die "e2fsck could not repair the base image's root filesystem (exit $rc)"
resize2fs "${LOOP}p2" >/dev/null
MNT="$(mktemp -d)"
mount "${LOOP}p2" "$MNT"
# Current DietPi images mount the FAT partition at /boot/firmware and keep dietpi.txt and the
# automation script in /boot on the root filesystem; older ones have the FAT partition at /boot.
FAT="$MNT/boot"
[ -d "$MNT/boot/firmware" ] && FAT="$MNT/boot/firmware"
mount "${LOOP}p1" "$FAT"

echo "==> copying sources"
# Never shipped: version control, environments, build output and pitv-install.json, which
# holds every password of an install and may sit in a developer's checkout.
common_excludes=(--exclude .git --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache
                 --exclude node_modules --exclude pitv-install.json --exclude '*.img' --exclude '*.img.xz')
rsync -a --delete "${common_excludes[@]}" --exclude .dev --exclude dist --exclude '*.egg-info' \
  --exclude '/installer/cli/pitv-installer*' "$ROOT/" "$MNT/opt/pitv-src/"
if [ -d "$CONTENT_SRC" ]; then
  rsync -a --delete "${common_excludes[@]}" "$CONTENT_SRC/" "$MNT/opt/pitv-content-src/"
else
  echo "WARNING: pitv_content sources not found at $CONTENT_SRC; image will run PiTV only"
fi
install -m 755 "$HERE/firstboot/pitv-firstboot.sh" "$MNT/boot/Automation_Custom_Script.sh"
install -m 644 "$ROOT/installer/config/pitv-install.example.json" "$FAT/pitv-install.example.json"
if [ "$FAT" != "$MNT/boot" ]; then
  # DietPi's first boot copies dietpi.txt and dietpi-wifi.txt from the FAT partition only when
  # they are newer (cp -u). Dating the root filesystem's copies at the epoch makes the
  # installer's files win whatever timestamp the FAT entries carry.
  for f in dietpi.txt dietpi-wifi.txt; do
    if [ -f "$MNT/boot/$f" ]; then touch -d @0 "$MNT/boot/$f"; fi
  done
fi
echo "pitv-image $VERSION $(date -u +%FT%TZ)" > "$MNT/etc/pitv-image-version"
sync
umount "$FAT"
umount "$MNT"
rmdir "$MNT"
MNT=""
losetup -d "$LOOP"
LOOP=""

echo "==> compressing"
xz -T0 -6 -f "$IMG"
mv -f "$IMG.xz" "$OUT/pitv-$VERSION.img.xz"
(cd "$OUT" && sha256sum "pitv-$VERSION.img.xz" > "pitv-$VERSION.img.xz.sha256")
echo "==> $OUT/pitv-$VERSION.img.xz"
