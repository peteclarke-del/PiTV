# PiTV installer

Everything needed to put PiTV and pitv_content on an SD card and keep them up to date. The
user-facing steps are in [setup/README.md](../setup/README.md); this file documents the parts.

```
installer/
├── image/build.sh          builds pitv-<version>.img.xz from a signed DietPi base (Linux, sudo)
├── image/firstboot/        the first-boot provisioning script baked into the image
├── cli/                    the installer program (Go): Linux and Windows
├── config/                 pitv-install.example.json
└── build-cli.sh            cross-compiles the installer into dist/
```

## How it fits together

1. Image. `sudo installer/image/build.sh` downloads the DietPi Bookworm image for the Pi
   2/3/4 (or takes `--base`) together with its detached signature, and refuses it unless the
   signature was made by DietPi's release key, whose fingerprint is pinned in the script. It
   grows the root partition by 512 MB, copies both apps' source trees in (PiTV from this
   checkout; pitv_content from `../../PiTV_content` or `--content-src`) under `/opt/pitv-src`
   and `/opt/pitv-content-src`, leaving out version control, virtual environments, build
   output and any `pitv-install.json`, and installs the first-boot hook as
   `/boot/Automation_Custom_Script.sh`. DietPi's own first-boot resize service stays enabled:
   it is what imports `dietpi.txt` from the FAT partition, and it grows the root partition only
   as far as the work partition the installer adds. Nothing is installed at build time, so no
   chroot or emulation is needed. Output: `dist/pitv-<version>.img.xz` and its `.sha256`.
2. Installer. `pitv-installer` (one binary per platform, run as root or administrator) asks
   the questions once (mode, hostname, timezone, locale, keyboard, maintenance user with
   password and SSH key, Ethernet, Wi-Fi, static IP, NAS host, credentials and shares, display
   mode, system partition size, USB drive, PiTV admin password, YouTube cookies file for
   pitv_content), validates and saves `pitv-install.json`, checks the image against the
   `.sha256` beside it when there is one, and decompresses it, never to more bytes than the
   card holds. Into the FAT partition it merges its settings into DietPi's own `dietpi.txt`,
   keeping DietPi's other defaults, and writes `pitv-install.json` and, with Wi-Fi,
   `dietpi-wifi.txt`. It adds
   partition 3 from `system_partition_gb` to the end of the card, then unmounts the card,
   writes it, drops the operating system's cached copy and reads the card back to compare
   SHA-256.
3. First boot. DietPi moves `dietpi.txt` and `dietpi-wifi.txt` off the FAT partition, grows
   the root partition up to partition 3, sets locale, hostname, network and OpenSSH, and runs
   `pitv-firstboot.sh`:
   - the maintenance user is created first (groups sudo, video, audio, input) with its
     password and authorized keys, so any later failure still leaves a way in. SSH then
     admits that user only: root and the `dietpi` account carry the DietPi global password,
     which the installer generates at random for every card and nobody knows;
   - partition 3 is formatted as `/work` (label `PITVWORK`) unless it is already mounted.
     `/var/lib/pitv` and `/var/log/journal` are bind mounts from it, with the journal
     persistent and capped at 200 MB, so databases and logs can never fill the system
     partition;
   - the USB drive (`work_drive.device`, `auto` = first USB disk other than the system card)
     is partitioned and formatted (label `PITVCACHE`) in `clean` mode or when it has no
     partition; otherwise its content is kept. It is mounted `nofail` at the cache directory's
     parent, which must be under `/mnt` or `/media`;
   - PiTV is installed with `setup/install.sh`, given the NAS credentials, shares, cache
     directory and display mode, and the admin password is set. `install.sh` writes the
     credentials root-only, mounts the shares read-only and lists them in
     `/etc/pitv/nas-sources.json`;
   - pitv_content is installed with its `setup/install-on-pi.sh` when its source tree is in
     the image, with `NAS_SOURCES` from `/etc/pitv/nas-sources.json`, so the installer's
     shares become pitv_content's sources. It reads them on its first install only; after
     that, sources are edited in the admin (Sources, in the pitv_content section);
   - the log is written to `/work/install/install.log` (a copy goes to `pitv-install.log` on
     the FAT partition), `pitv-install.json` is deleted, and the Pi reboots. Every later boot
     is a normal boot.

   Secrets from `pitv-install.json` go to root-only files or reach programs on stdin or in
   the environment, never on a command line or in the log. DietPi runs the script once and
   never again, so a failed or interrupted first boot is finished by hand: log in as the
   maintenance user, fix the cause given at the end of the log, and run
   `sudo bash /boot/Automation_Custom_Script.sh`. Every step is safe to repeat.
4. Upgrade. `pitv-installer upgrade --host pitv --user <maintenance user>` (or mode `upgrade`
   in the guided flow) connects over SSH (password, or `--key`), checks the Pi's host key
   against `~/.ssh/known_hosts` (shown and recorded on first contact, refused if it ever
   changes), and streams the local checkouts to a private temporary directory on the Pi. As
   root it syncs them to `/opt/pitv-src` and `/opt/pitv-content-src`, re-runs both install
   scripts and restarts the services. The work partition, logs and USB drive are untouched,
   and so is the NAS host unless `--nas` names a new one. If the Pi cannot boot, write a
   fresh card in `normal` mode instead: the USB drive keeps its content.

Modes: `clean` wipes the card and the USB drive; `normal` wipes the card only; `upgrade`
installs the latest code and keeps everything else. All three append to the install log,
which the PiTV admin shows under Logs.

## Building

```sh
installer/build-cli.sh        # needs Go 1.26+; outputs dist/pitv-installer-{linux-amd64,linux-arm64,windows-amd64.exe}
sudo installer/image/build.sh # needs xz, parted, losetup, e2fsck, resize2fs, rsync, curl, gpg; outputs dist/pitv-<version>.img.xz
```

`build.sh` fetches DietPi's public key from `https://github.com/MichaIng.gpg`; for an offline
build point `DIETPI_KEY_FILE` at a saved copy. Either way only the key with the pinned
fingerprint is accepted.

`installer/cli` has Go tests for config validation, `dietpi.txt` rendering and merging,
boot-partition patching, the work partition, decompression limits and checksums, write
verification, host key trust and the upload filter: `cd installer/cli && go test ./...`.

## Using

```sh
sudo dist/pitv-installer-linux-amd64                 # guided: wizard, pick a card, write
sudo dist/pitv-installer-linux-amd64 config          # wizard only, writes pitv-install.json
sudo dist/pitv-installer-linux-amd64 disks           # list the disks a card can be written to
sudo dist/pitv-installer-linux-amd64 write --image dist/pitv-v0.1.0.img.xz --disk /dev/sdb --config pitv-install.json [--yes]
dist/pitv-installer-linux-amd64 upgrade --host pitv --user pete [--content-src ../PiTV_content] [--key ~/.ssh/id_ed25519] [--nas nas2]
dist/pitv-installer-linux-amd64 version
```

Only removable, USB or SD disks of 2 TB or less are offered. On Linux a disk that holds any
part of the running system (`/`, `/boot`, `/usr`, `/var`, `/home`, `/etc`, `/opt`, `/srv` or
swap) is never listed, whatever its bus; on Windows the system and boot disks are excluded,
disks are listed as `\\.\PhysicalDriveN`, and their volumes are locked and dismounted before
writing. `pitv-install.json` holds the NAS and user passwords in clear text and is always
saved with mode 0600; keep it out of version control. The upload and image build never
include it.
