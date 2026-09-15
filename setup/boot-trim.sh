#!/usr/bin/env bash
# Trim Raspberry Pi OS Lite for a fast boot straight into the player. Run as root.
set -uo pipefail
CONFIG=/boot/firmware/config.txt; [ -f "$CONFIG" ] || CONFIG=/boot/config.txt
CMDLINE=/boot/firmware/cmdline.txt; [ -f "$CMDLINE" ] || CMDLINE=/boot/cmdline.txt

set_cfg() {  # key=value, added under [all] if missing
  grep -qE "^$1=" "$CONFIG" && sed -i -E "s|^$1=.*|$1=$2|" "$CONFIG" || echo "$1=$2" >> "$CONFIG"
}
add_overlay() { grep -q "^dtoverlay=$1" "$CONFIG" || echo "dtoverlay=$1" >> "$CONFIG"; }

set_cfg boot_delay 0
set_cfg disable_splash 1
set_cfg hdmi_drive 2            # HDMI audio
set_cfg disable_overscan 1
add_overlay disable-bt
grep -q "^max_framebuffers" "$CONFIG" || echo "max_framebuffers=2" >> "$CONFIG"

# Display: DISPLAY_MODE=hdmi576 (default: HDMI 720x576p 50 Hz 4:3 into an HDMI-to-SCART converter),
# composite (PAL 4:3 on the Pi 4's 3.5 mm AV jack), hdmi43 (1024x768 for a 4:3 monitor) or hdmi.
DISPLAY_MODE="${DISPLAY_MODE:-hdmi576}"
sed -i '/^dtoverlay=vc4-kms-v3d/d; /^enable_tvout/d; /^sdtv_mode/d; /^sdtv_aspect/d; /^hdmi_group/d; /^hdmi_mode/d; /^hdmi_aspect/d' "$CONFIG"
case "$DISPLAY_MODE" in
  hdmi576)
    echo "hdmi_group=1" >> "$CONFIG"
    echo "hdmi_mode=17" >> "$CONFIG"         # CEA 17: 720x576p 50 Hz, 4:3 (PAL)
    echo "hdmi_drive=2" >> "$CONFIG"
    echo "dtoverlay=vc4-kms-v3d" >> "$CONFIG"
    ;;
  composite)
    echo "enable_tvout=1" >> "$CONFIG"
    echo "sdtv_mode=2" >> "$CONFIG"          # PAL
    echo "sdtv_aspect=1" >> "$CONFIG"        # 4:3
    echo "dtoverlay=vc4-kms-v3d,composite=1" >> "$CONFIG"
    ;;
  hdmi43)
    echo "hdmi_group=2" >> "$CONFIG"
    echo "hdmi_mode=16" >> "$CONFIG"         # 1024x768 60 Hz, 4:3
    echo "dtoverlay=vc4-kms-v3d" >> "$CONFIG"
    ;;
  *)
    echo "dtoverlay=vc4-kms-v3d" >> "$CONFIG"
    ;;
esac

# Hardware watchdog: if the kernel or systemd hangs, the SoC reboots the Pi within 15 s.
grep -q "^dtparam=watchdog=on" "$CONFIG" || echo "dtparam=watchdog=on" >> "$CONFIG"
mkdir -p /etc/systemd/system.conf.d
printf '[Manager]\nRuntimeWatchdogSec=15\nRebootWatchdogSec=2min\n' > /etc/systemd/system.conf.d/watchdog.conf
# Clock: no RTC, so fake-hwclock saves the time on shutdown and timesyncd steps it at boot.
systemctl enable --now systemd-timesyncd 2>/dev/null || true
systemctl enable fake-hwclock 2>/dev/null || true

# Quiet kernel, no rainbow, no cursor blink, no plymouth.
if ! grep -q "quiet" "$CMDLINE"; then sed -i 's/$/ quiet/' "$CMDLINE"; fi
grep -q "loglevel=" "$CMDLINE" || sed -i 's/$/ loglevel=3/' "$CMDLINE"
grep -q "vt.global_cursor_default" "$CMDLINE" || sed -i 's/$/ vt.global_cursor_default=0/' "$CMDLINE"
sed -i 's/ splash//; s/ plymouth.ignore-serial-consoles//' "$CMDLINE"
grep -q "consoleblank" "$CMDLINE" || sed -i 's/$/ consoleblank=0/' "$CMDLINE"

# Services that cost boot time and are not needed for a television.
for svc in bluetooth hciuart avahi-daemon triggerhappy ModemManager apt-daily.timer apt-daily-upgrade.timer \
           man-db.timer rpi-eeprom-update dphys-swapfile cups wpa_supplicant systemd-networkd-wait-online; do
  systemctl disable --now "$svc" 2>/dev/null || true
done
# Keep the network-online target quick: NetworkManager waits at most 10 s.
mkdir -p /etc/systemd/system/NetworkManager-wait-online.service.d
printf '[Service]\nExecStart=\nExecStart=/usr/bin/nm-online -s -q --timeout=10\n' > /etc/systemd/system/NetworkManager-wait-online.service.d/timeout.conf
# journald: small, volatile-ish log to protect the SD card.
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=50M\nRuntimeMaxUse=20M\n' > /etc/systemd/journald.conf.d/pitv.conf
systemctl daemon-reload
echo "boot trim applied; reboot to take effect. Check with: systemd-analyze && systemd-analyze blame | head"
