# PiTV installer

Everything needed to put PiTV and pitv_content on an SD card and keep them up to date. The
user-facing steps are in [setup/README.md](../setup/README.md); this file documents the parts.

```
installer/
├── image/build.sh          builds pitv-<version>.img.xz from a DietPi base (Linux, sudo)
├── image/firstboot/        the first-boot provisioning script baked into the image
├── cli/                    the installer program (Go): Linux and Windows
├── config/                 pitv-install.example.json
└── build-cli.sh            cross-compiles the installer into dist/
```

## How it fits together

1. Image. `sudo installer/image/build.sh` downloads the DietPi Bookworm image for the Pi
   2/3/4 (or takes `--base`), grows the root partition by 512 MB, copies both apps' source
   trees in (PiTV from this checkout; pitv_content from `../../PiTV_content` or
   `--content-src`) under `/opt/pitv-src` and `/opt/pitv-content-src`, installs the first-boot
   hook as `Automation_Custom_Script.sh` on the boot partition, and disables DietPi's own
   root-partition auto-grow. Nothing is installed at build time, so no chroot or emulation
   is needed. Output: `dist/pitv-<version>.img.xz` and a `.sha256`.
2. Installer. `pitv-installer` (one binary per platform, run as root or administrator)
   asks the questions once (mode, hostname, timezone, locale, keyboard, maintenance user
   with password and SSH key, Ethernet, Wi-Fi, static IP, NAS host, credentials and shares,
   display mode, system partition size, USB drive, PiTV admin password, YouTube cookies file
   for pitv_content), validates and saves `pitv-install.json`, decompresses the image,
   writes `dietpi.txt`, `pitv-install.json` and (with Wi-Fi) `dietpi-wifi.txt` into the FAT
   boot partition, unmounts and writes the card, then reads it back and compares SHA-256.
3. First boot. DietPi's automation sets locale, hostname, network, SSH and passwords, then
   runs `pitv-firstboot.sh`:
   - the system partition is resized to `system_partition_gb` (8 by default) and the rest
     of the card becomes `/work` (label `PITVWORK`); `/var/lib/pitv` and `/var/log/journal`
     become symlinks into it, with the journal persistent and capped at 200 MB;
   - the USB drive (`work_drive.device`, `auto` = first USB disk) is partitioned and
     formatted (label `PITVCACHE`) in `clean` mode or when it has no partition; otherwise its
     content is kept. It is mounted `nofail` at the cache directory's parent and the cache
     folders are created;
   - the maintenance user is created (groups sudo, video, audio, input) with its password
     and authorized keys;
   - PiTV is installed with `setup/install.sh` (NAS credentials, shares, cache directory
     and display mode from the config) and the admin password is set;
   - pitv_content is installed with its `setup/install-on-pi.sh` when its sources are in
     the image;
   - the log is written to `/work/install/install.log` (a copy goes to
     `/boot/pitv-install.log`), the custom script is disabled in `dietpi.txt`, the config
     with its credentials is deleted from the boot partition, and the Pi reboots. Every
     later boot is a normal boot. On failure the script stops with the reason in the log;
     fix it and reboot to retry.
4. Upgrade. `pitv-installer upgrade --host pitv --user <maintenance user>` (or mode
   `upgrade` in the guided flow) uploads the local checkouts over SSH (password or `--key`),
   syncs them to `/opt/pitv-src` and `/opt/pitv-content-src`, re-runs both install scripts
   with sudo and restarts the services; the work partition, logs and USB drive are untouched.
   If the Pi cannot boot, write a fresh card in `normal` mode instead: the USB drive keeps
   its content.

Modes: `clean` wipes the card and the USB drive; `normal` wipes the card only; `upgrade`
installs the latest code and keeps everything else. All three append to the install log,
which the PiTV admin shows under Logs.

## Building

```sh
installer/build-cli.sh        # needs Go 1.23+; outputs dist/pitv-installer-{linux-amd64,linux-arm64,windows-amd64.exe}
sudo installer/image/build.sh # needs xz, parted, losetup, mkfs.ext4, rsync; outputs dist/pitv-<version>.img.xz
```

`installer/cli` has Go tests for config validation, `dietpi.txt` rendering and boot-partition
patching: `cd installer/cli && go test ./...`.

## Using

```sh
sudo dist/pitv-installer-linux-amd64                 # guided: wizard, pick a card, write
sudo dist/pitv-installer-linux-amd64 config          # wizard only, writes pitv-install.json
sudo dist/pitv-installer-linux-amd64 disks           # list removable disks
sudo dist/pitv-installer-linux-amd64 write --image dist/pitv-v0.1.0.img.xz --disk /dev/sdb --config pitv-install.json [--yes]
dist/pitv-installer-linux-amd64 upgrade --host pitv --user pete [--content-src ../PiTV_content] [--key ~/.ssh/id_ed25519]
dist/pitv-installer-linux-amd64 version
```

Only removable, USB or MMC disks are offered, so a system drive cannot be picked by
accident. On Windows disks are listed as `\\.\PhysicalDriveN` and their volumes are locked
and dismounted before writing. `pitv-install.json` holds the NAS and user passwords in
clear text and is saved with mode 0600; keep it out of version control.
