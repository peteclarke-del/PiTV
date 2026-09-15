# PiTV installer

Everything needed to put PiTV and pitv_content on an SD card and keep them up to date.

```
installer/
├── image/build.sh          builds pitv-<version>.img.xz from a DietPi base (Linux, sudo)
├── image/firstboot/        the first-boot provisioning script baked into the image
├── cli/                    the installer program (Go): Linux and Windows
├── config/                 pitv-install.example.json
└── build-cli.sh            cross-compiles the installer into dist/
```

## How it fits together

1. **Image.** `sudo installer/image/build.sh` downloads DietPi (Bookworm, Pi 2/3/4), adds both
   apps' source trees under `/opt/pitv-src` and `/opt/pitv-content-src`, installs the first-boot
   hook and disables DietPi's own root-partition auto-grow. Nothing is installed at build time,
   so no chroot or emulation is needed.
2. **Installer.** `pitv-installer` (one binary per platform, run as administrator/root) asks the
   questions once (mode, hostname, maintenance user with password and SSH key, Wi-Fi, static IP,
   NAS credentials, display, partition size, admin password), saves `pitv-install.json`, then
   decompresses the image, writes `dietpi.txt`, `dietpi-wifi.txt` and `pitv-install.json` into the
   boot partition, writes the card and verifies it by reading it back.
3. **First boot.** DietPi's automation sets locale, hostname, network, SSH and passwords, then runs
   `Automation_Custom_Script.sh` (our `pitv-firstboot.sh`): the system partition is sized to
   `system_partition_gb` and the rest of the card becomes `/work` (logs, databases, state, journal),
   the USB drive is prepared (wiped only in `clean` mode; otherwise existing content is kept and
   only the cache directories are created), the maintenance user is created, PiTV and then
   pitv_content are installed and primed (services, timers, API), the admin password is set, the
   install log is written to `/work/install/install.log`, the config with its credentials is
   removed from the boot partition, and the Pi reboots into normal operation. Every later boot is
   a normal boot.
4. **Upgrade.** `pitv-installer upgrade --host pitv --user <maintenance user>` (or mode `upgrade`
   in the guided flow) syncs the local checkouts to the Pi over SSH and re-runs both install
   scripts; the work partition, logs and USB drive are untouched. If the Pi cannot boot, write a
   fresh card in `normal` mode instead: the USB drive keeps its content.

Modes: **clean** wipes the card and the USB work drive; **normal** wipes the card only;
**upgrade** installs the latest code and keeps everything else. All three append to the install
log, which the PiTV admin shows under Logs → install.

## Building

```sh
installer/build-cli.sh                   # needs Go 1.22+; outputs dist/pitv-installer-*
sudo installer/image/build.sh            # needs xz, parted, losetup, rsync; outputs dist/pitv-<version>.img.xz
```

## Using

```sh
sudo dist/pitv-installer-linux-amd64             # guided
sudo dist/pitv-installer-linux-amd64 disks       # list cards
sudo dist/pitv-installer-linux-amd64 write --image dist/pitv-v0.1.0.img.xz --disk /dev/sdb --config pitv-install.json
dist/pitv-installer-linux-amd64 upgrade --host pitv --user pete --content-src ../PiTV_content
```

On Windows run `pitv-installer-windows-amd64.exe` from an administrator prompt; disks are listed
as `\\.\PhysicalDriveN` and their drive letters are dismounted before writing.
