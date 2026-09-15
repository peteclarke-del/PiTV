#!/usr/bin/env bash
# Build the PiTV SD-card image from a DietPi base. Linux only, needs root (loop mounts).
#
#   sudo installer/image/build.sh [--base DietPi_RPi234-ARMv8-Bookworm.img.xz] [--content-src ../PiTV_content] [--out dist/]
#
# The image is DietPi + both apps' source trees + the first-boot hook; nothing is installed
# at build time (no chroot/qemu needed): DietPi's automated first run installs packages and
# runs installer/image/firstboot/pitv-firstboot.sh, which partitions the card, prepares the
# work drive and provisions both apps from /boot/pitv-install.json written by the installer.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
BASE_URL="${BASE_URL:-https://dietpi.com/downloads/images/DietPi_RPi234-ARMv8-Bookworm.img.xz}"
BASE=""; CONTENT_SRC="${CONTENT_SRC:-$ROOT/../../PiTV_content}"; OUT="$ROOT/dist"
while [ $# -gt 0 ]; do case "$1" in
  --base) BASE="$2"; shift 2;; --content-src) CONTENT_SRC="$2"; shift 2;; --out) OUT="$2"; shift 2;;
  *) echo "unknown option $1"; exit 1;; esac; done
[ "$(id -u)" -eq 0 ] || { echo "run with sudo (loop mounts)"; exit 1; }
for t in xz losetup parted mkfs.ext4 rsync; do command -v "$t" >/dev/null || { echo "missing $t"; exit 1; }; done
mkdir -p "$OUT" "$OUT/work"
VERSION="$(cd "$ROOT" && git describe --tags --always 2>/dev/null || echo dev)"
IMG="$OUT/work/pitv-$VERSION.img"

if [ -z "$BASE" ]; then
  BASE="$OUT/work/$(basename "$BASE_URL")"
  [ -f "$BASE" ] || curl -L --fail -o "$BASE" "$BASE_URL"
fi
echo "==> base $BASE"
xz -dkc "$BASE" > "$IMG"
# Room for the sources (~ +512 MB); the first boot grows/creates partitions on the real card.
truncate -s +512M "$IMG"
parted -s "$IMG" resizepart 2 100%

LOOP="$(losetup --find --show --partscan "$IMG")"
trap 'umount -q "$MNT/boot" 2>/dev/null; umount -q "$MNT" 2>/dev/null; losetup -d "$LOOP" 2>/dev/null' EXIT
e2fsck -fp "${LOOP}p2" >/dev/null || true
resize2fs "${LOOP}p2" >/dev/null
MNT="$(mktemp -d)"
mount "${LOOP}p2" "$MNT"; mkdir -p "$MNT/boot"; mount "${LOOP}p1" "$MNT/boot"

echo "==> copying sources"
rsync -a --delete --exclude .git --exclude .venv --exclude .dev --exclude node_modules --exclude web/node_modules --exclude dist \
  "$ROOT/" "$MNT/opt/pitv-src/"
if [ -d "$CONTENT_SRC" ]; then
  rsync -a --delete --exclude .git --exclude .venv --exclude .pytest_cache "$CONTENT_SRC/" "$MNT/opt/pitv-content-src/"
else
  echo "WARNING: pitv_content sources not found at $CONTENT_SRC; image will run PiTV only"
fi
install -m 755 "$HERE/firstboot/pitv-firstboot.sh" "$MNT/boot/Automation_Custom_Script.sh"
cp "$ROOT/installer/config/pitv-install.example.json" "$MNT/boot/pitv-install.example.json"
# DietPi would grow the root partition to fill the card; the first boot sizes it instead.
rm -f "$MNT/etc/systemd/system/multi-user.target.wants/dietpi-fs_partition_resize.service" 2>/dev/null || true
echo "pitv-image $VERSION $(date -u +%FT%TZ)" > "$MNT/etc/pitv-image-version"
sync
umount "$MNT/boot"; umount "$MNT"; losetup -d "$LOOP"; trap - EXIT

echo "==> compressing"
xz -T0 -6 -f "$IMG"
mv "$IMG.xz" "$OUT/pitv-$VERSION.img.xz"
sha256sum "$OUT/pitv-$VERSION.img.xz" > "$OUT/pitv-$VERSION.img.xz.sha256"
echo "==> $OUT/pitv-$VERSION.img.xz"
