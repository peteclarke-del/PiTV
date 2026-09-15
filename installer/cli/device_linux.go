//go:build linux

package main

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
)

func sprintf(f string, a ...any) string { return fmt.Sprintf(f, a...) }

// ListDisks reads /sys/block; only removable or USB/MMC disks are offered so a system
// drive can never be picked by accident.
func ListDisks() ([]Disk, error) {
	entries, err := os.ReadDir("/sys/block")
	if err != nil {
		return nil, err
	}
	mounts := mountedDevices()
	var out []Disk
	for _, e := range entries {
		name := e.Name()
		if strings.HasPrefix(name, "loop") || strings.HasPrefix(name, "ram") || strings.HasPrefix(name, "dm-") || strings.HasPrefix(name, "zram") {
			continue
		}
		base := filepath.Join("/sys/block", name)
		removable := strings.TrimSpace(readFile(filepath.Join(base, "removable"))) == "1"
		link, _ := os.Readlink(filepath.Join(base, "device"))
		usb := strings.Contains(link, "usb") || strings.Contains(readFile(filepath.Join(base, "device", "uevent")), "usb")
		mmc := strings.HasPrefix(name, "mmcblk")
		if !(removable || usb || mmc) {
			continue
		}
		sectors, _ := strconv.ParseInt(strings.TrimSpace(readFile(filepath.Join(base, "size"))), 10, 64)
		model := strings.TrimSpace(readFile(filepath.Join(base, "device", "model")))
		if model == "" {
			model = strings.TrimSpace(readFile(filepath.Join(base, "device", "name")))
		}
		d := Disk{Path: "/dev/" + name, Model: model, SizeBytes: sectors * 512, Removable: removable || usb || mmc}
		for dev, mp := range mounts {
			if strings.HasPrefix(dev, d.Path) {
				d.Mounts = append(d.Mounts, mp)
			}
		}
		if d.SizeBytes > 0 {
			out = append(out, d)
		}
	}
	return out, nil
}

func readFile(p string) string {
	b, _ := os.ReadFile(p)
	return string(b)
}

func mountedDevices() map[string]string {
	out := map[string]string{}
	f, err := os.Open("/proc/mounts")
	if err != nil {
		return out
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) >= 2 && strings.HasPrefix(fields[0], "/dev/") {
			out[fields[0]] = fields[1]
		}
	}
	return out
}

type linuxDevice struct{ f *os.File }

func (d *linuxDevice) Write(p []byte) (int, error) { return d.f.Write(p) }
func (d *linuxDevice) Read(p []byte) (int, error)  { return d.f.Read(p) }
func (d *linuxDevice) Sync() error                 { return d.f.Sync() }
func (d *linuxDevice) SeekStart() error            { _, err := d.f.Seek(0, 0); return err }
func (d *linuxDevice) Close() error                { return d.f.Close() }

// OpenDisk unmounts every partition of the disk and opens it exclusively for writing.
func OpenDisk(d Disk) (RawDevice, error) {
	if os.Geteuid() != 0 {
		return nil, fmt.Errorf("writing to %s needs root: run with sudo", d.Path)
	}
	for _, mp := range d.Mounts {
		if out, err := exec.Command("umount", mp).CombinedOutput(); err != nil {
			return nil, fmt.Errorf("could not unmount %s: %s", mp, strings.TrimSpace(string(out)))
		}
	}
	f, err := os.OpenFile(d.Path, os.O_RDWR|os.O_EXCL, 0)
	if err != nil {
		return nil, err
	}
	return &linuxDevice{f: f}, nil
}

// Rescan asks the kernel to re-read the partition table after writing.
func Rescan(d Disk) { _ = exec.Command("partprobe", d.Path).Run() }

func IsAdmin() bool { return os.Geteuid() == 0 }
