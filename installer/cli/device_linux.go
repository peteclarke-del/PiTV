//go:build linux

package main

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"golang.org/x/sys/unix"
)

const adminHint = "run it with sudo"

func IsAdmin() bool { return os.Geteuid() == 0 }

// systemMounts are the mount points of the running system. A disk that holds any of them,
// or active swap, is never offered, whatever its bus: laptops boot from eMMC and a Pi
// running this installer boots from its own SD card or a USB disk.
var systemMounts = []string{"/", "/boot", "/usr", "/var", "/home", "/etc", "/opt", "/srv"}

func isSystemMount(mp string) bool {
	for _, s := range systemMounts {
		if mp == s || (s != "/" && strings.HasPrefix(mp, s+"/")) {
			return true
		}
	}
	return false
}

// listDisks returns the removable, USB and MMC disks from /sys/block that hold no part of the
// running system.
func listDisks() ([]Disk, error) {
	entries, err := os.ReadDir("/sys/block")
	if err != nil {
		return nil, err
	}
	use := diskUsage()
	var out []Disk
	for _, e := range entries {
		name := e.Name()
		if strings.HasPrefix(name, "loop") || strings.HasPrefix(name, "ram") || strings.HasPrefix(name, "dm-") ||
			strings.HasPrefix(name, "zram") || strings.HasPrefix(name, "md") {
			continue
		}
		base := filepath.Join("/sys/block", name)
		real, _ := filepath.EvalSymlinks(base)
		removable := sysfsValue(base, "removable") == "1"
		if !(removable || strings.Contains(real, "/usb") || strings.HasPrefix(name, "mmcblk")) {
			continue
		}
		mounts, system := use[name], false
		for _, mp := range mounts {
			system = system || mp == "[SWAP]" || isSystemMount(mp)
		}
		sectors, _ := strconv.ParseInt(sysfsValue(base, "size"), 10, 64) // always 512-byte units
		if system {
			continue
		}
		model := sysfsValue(base, "device/model")
		if model == "" {
			model = sysfsValue(base, "device/name")
		}
		// Deepest first, so a mount nested inside another is released before its parent.
		sort.Slice(mounts, func(i, j int) bool { return len(mounts[i]) > len(mounts[j]) })
		out = append(out, Disk{Path: "/dev/" + name, Model: model, SizeBytes: sectors * 512, Mounts: mounts})
	}
	return out, nil
}

func sysfsValue(dir, name string) string {
	b, _ := os.ReadFile(filepath.Join(dir, name))
	return strings.TrimSpace(string(b))
}

// wholeDisks resolves a block device's sysfs directory to the whole disks beneath it: a
// partition to its parent, a device-mapper or md device (LVM, LUKS, RAID) to the disks of
// its members.
func wholeDisks(sys string) []string {
	if members, _ := os.ReadDir(filepath.Join(sys, "slaves")); len(members) > 0 {
		var out []string
		for _, m := range members {
			out = append(out, wholeDisks(filepath.Join("/sys/class/block", m.Name()))...)
		}
		return out
	}
	real, err := filepath.EvalSymlinks(sys)
	if err != nil {
		return nil
	}
	if _, err := os.Stat(filepath.Join(real, "partition")); err == nil {
		real = filepath.Dir(real)
	}
	return []string{filepath.Base(real)}
}

// devDisks resolves a /dev path (including /dev/mapper links) to its whole disks.
func devDisks(dev string) []string {
	if !strings.HasPrefix(dev, "/dev/") {
		return nil
	}
	real, err := filepath.EvalSymlinks(dev)
	if err != nil {
		return nil
	}
	return wholeDisks(filepath.Join("/sys/class/block", filepath.Base(real)))
}

var mountUnescape = strings.NewReplacer(`\040`, " ", `\011`, "\t", `\012`, "\n", `\134`, `\`)

// diskUsage maps each whole disk to its mount points and "[SWAP]" for active swap. A mount
// is matched by its major:minor number, which names the device even where the source reads
// /dev/root, and by its source path, which is all a btrfs subvolume offers.
func diskUsage() map[string][]string {
	use := map[string][]string{}
	add := func(disks []string, what string) {
		for _, d := range disks {
			use[d] = append(use[d], what)
		}
	}
	if f, err := os.Open("/proc/self/mountinfo"); err == nil {
		sc := bufio.NewScanner(f)
		for sc.Scan() {
			// id parent major:minor root mountpoint options [optional...] - fstype source superoptions
			fields := strings.Fields(sc.Text())
			sep := -1
			for i, v := range fields {
				if v == "-" {
					sep = i
					break
				}
			}
			if len(fields) < 5 || sep < 0 || sep+2 >= len(fields) {
				continue
			}
			disks := wholeDisks("/sys/dev/block/" + fields[2])
			if len(disks) == 0 {
				disks = devDisks(fields[sep+2])
			}
			add(disks, mountUnescape.Replace(fields[4]))
		}
		f.Close()
	}
	if f, err := os.Open("/proc/swaps"); err == nil {
		sc := bufio.NewScanner(f)
		for sc.Scan() {
			if fields := strings.Fields(sc.Text()); len(fields) > 0 {
				add(devDisks(fields[0]), "[SWAP]")
			}
		}
		f.Close()
	}
	for d, mounts := range use {
		use[d] = uniq(mounts)
	}
	return use
}

func uniq(s []string) []string {
	seen := map[string]bool{}
	out := s[:0]
	for _, v := range s {
		if !seen[v] {
			seen[v] = true
			out = append(out, v)
		}
	}
	return out
}

type linuxDevice struct{ *os.File }

// Sync flushes the writes and then drops the device's pages from the page cache, so the
// verification pass reads the card and not the kernel's copy of what was written.
func (d linuxDevice) Sync() error {
	if err := d.File.Sync(); err != nil {
		return err
	}
	return unix.Fadvise(int(d.Fd()), 0, 0, unix.FADV_DONTNEED)
}

func (d linuxDevice) SeekStart() error {
	_, err := d.Seek(0, io.SeekStart)
	return err
}

// OpenDisk unmounts every filesystem on the disk and opens it exclusively: O_EXCL on a block
// device fails while anything still has it mounted or claimed.
func OpenDisk(d Disk) (RawDevice, error) {
	for _, mp := range d.Mounts {
		if out, err := exec.Command("umount", mp).CombinedOutput(); err != nil {
			return nil, fmt.Errorf("could not unmount %s: %s", mp, strings.TrimSpace(string(out)))
		}
	}
	f, err := os.OpenFile(d.Path, os.O_RDWR|os.O_EXCL, 0)
	if err != nil {
		return nil, err
	}
	return linuxDevice{f}, nil
}

// Rescan asks the kernel to re-read the new partition table. Failure is harmless: the table
// is read again when the card is next inserted.
func Rescan(d Disk) { _ = exec.Command("partprobe", d.Path).Run() }
