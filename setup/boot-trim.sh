#!/usr/bin/env bash
# Trim Raspberry Pi OS Lite or DietPi for a fast boot straight into the player, set the display
# mode and arm the hardware watchdog. Run as root (install.sh does); every run gives the same
# result, so it is safe to repeat with another DISPLAY_MODE.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "boot-trim.sh: run as root" >&2; exit 1; }
CONFIG=/boot/firmware/config.txt; [ -f "$CONFIG" ] || CONFIG=/boot/config.txt
CMDLINE=/boot/firmware/cmdline.txt; [ -f "$CMDLINE" ] || CMDLINE=/boot/cmdline.txt

# DISPLAY_MODE: hdmi576 (default: HDMI 720x576p 50 Hz 4:3 into an HDMI-to-SCART converter),
# composite (PAL 4:3 on the Pi 4's 3.5 mm AV jack), hdmi43 (1024x768 for a 4:3 monitor) or
# hdmi (the monitor's preferred mode).
DISPLAY_MODE="${DISPLAY_MODE:-hdmi576}"
case "$DISPLAY_MODE" in
  hdmi576) display=(hdmi_group=1 hdmi_mode=17 dtoverlay=vc4-kms-v3d) ;;                    # CEA 17: 720x576p 50 Hz 4:3
  composite) display=(enable_tvout=1 sdtv_mode=2 sdtv_aspect=1 'dtoverlay=vc4-kms-v3d,composite=1') ;;  # PAL, 4:3
  hdmi43) display=(hdmi_group=2 hdmi_mode=16 dtoverlay=vc4-kms-v3d) ;;                      # DMT 16: 1024x768 60 Hz
  hdmi) display=(dtoverlay=vc4-kms-v3d) ;;
  *) echo "boot-trim.sh: unknown DISPLAY_MODE $DISPLAY_MODE" >&2; exit 1 ;;
esac

# replace FILE: stdin becomes FILE through a temporary file and a rename, so a power cut
# leaves the old file or the new one.
replace() {
  local tmp
  tmp="$(mktemp "$1.XXXXXX")"
  cat > "$tmp"
  chmod 644 "$tmp"
  mv -f "$tmp" "$1"
}

# config.txt: the settings PiTV owns are removed wherever they are and written again as one
# marked block under [all], so they apply to every model whatever section the file ends in.
# The analogue jack is switched on (DietPi ships it off): it carries composite mode's sound.
BEGIN='# >>> PiTV: written by setup/boot-trim.sh on every install'
END='# <<< PiTV'
{
  awk -v begin="$BEGIN" -v end="$END" '$0 == begin {skip = 1} !skip {print} $0 == end {skip = 0}' "$CONFIG" |
    sed -E -e '/^(boot_delay|disable_splash|disable_overscan|hdmi_drive|max_framebuffers|enable_tvout|sdtv_mode|sdtv_aspect|hdmi_group|hdmi_mode|hdmi_aspect)=/d' \
      -e '/^dtoverlay=(disable-bt|vc4-kms-v3d)/d' -e '/^dtparam=(watchdog|audio)=/d'
  printf '%s\n' "$BEGIN" '[all]' boot_delay=0 disable_splash=1 disable_overscan=1 hdmi_drive=2 max_framebuffers=2 \
    dtoverlay=disable-bt dtparam=audio=on dtparam=watchdog=on "${display[@]}" "$END"
} | replace "$CONFIG"

# cmdline.txt (one line): a quiet kernel, no cursor, no console blanking, no splash.
read -ra args < "$CMDLINE"
kept=()
for a in "${args[@]}"; do
  case "$a" in splash|plymouth.ignore-serial-consoles) ;; *) kept+=("$a") ;; esac
done
for want in quiet loglevel=3 vt.global_cursor_default=0 consoleblank=0; do
  present=0
  for a in "${kept[@]}"; do [ "${a%%=*}" = "${want%%=*}" ] && present=1; done
  [ "$present" = 1 ] || kept+=("$want")
done
printf '%s\n' "${kept[*]}" | replace "$CMDLINE"

unit_exists() { systemctl cat "$1" >/dev/null 2>&1; }

# Hardware watchdog: if the kernel or systemd hangs, the SoC resets the Pi within 15 s (the
# BCM2835 watchdog's limit is just under 16 s).
mkdir -p /etc/systemd/system.conf.d
printf '[Manager]\nRuntimeWatchdogSec=15\nRebootWatchdogSec=2min\n' | replace /etc/systemd/system.conf.d/watchdog.conf
# Clock: there is no RTC, so fake-hwclock (where installed) keeps the last time across reboots
# and timesyncd steps it once the network is up.
if unit_exists systemd-timesyncd.service; then systemctl enable --now systemd-timesyncd.service; fi
if unit_exists fake-hwclock.service; then systemctl enable fake-hwclock.service; fi

# Services that cost boot time and do nothing for a television. wpa_supplicant stays: Wi-Fi
# may be how the Pi reaches the NAS.
for unit in bluetooth.service hciuart.service avahi-daemon.service triggerhappy.service ModemManager.service \
            apt-daily.timer apt-daily-upgrade.timer man-db.timer rpi-eeprom-update.service dphys-swapfile.service \
            cups.service systemd-networkd-wait-online.service; do
  if unit_exists "$unit"; then systemctl disable --now "$unit"; fi
done
# network-online.target stays quick: NetworkManager (Raspberry Pi OS) waits at most 10 s.
if unit_exists NetworkManager-wait-online.service; then
  mkdir -p /etc/systemd/system/NetworkManager-wait-online.service.d
  printf '[Service]\nExecStart=\nExecStart=/usr/bin/nm-online -s -q --timeout=10\n' |
    replace /etc/systemd/system/NetworkManager-wait-online.service.d/timeout.conf
fi
# A small journal protects the SD card. An installer-made card keeps its journal on /work, and
# pitv-firstboot.sh's 60-pitv-work.conf overrides this size there.
mkdir -p /etc/systemd/journald.conf.d
rm -f /etc/systemd/journald.conf.d/pitv.conf   # the name used before 50-pitv.conf
printf '[Journal]\nSystemMaxUse=50M\nRuntimeMaxUse=20M\n' | replace /etc/systemd/journald.conf.d/50-pitv.conf
systemctl daemon-reload
echo "boot trim applied ($DISPLAY_MODE); reboot to take effect. Check with: systemd-analyze && systemd-analyze blame | head"
