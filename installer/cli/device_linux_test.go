//go:build linux

package main

import "testing"

func TestSystemMounts(t *testing.T) {
	for mp, want := range map[string]bool{"/": true, "/boot/firmware": true, "/var/lib": true, "/home": true,
		"/media/pete/bootfs": false, "/run/media/pete/boot": false, "/mnt/sd": false, "/homework": false} {
		if got := isSystemMount(mp); got != want {
			t.Errorf("isSystemMount(%q) = %v, want %v", mp, got, want)
		}
	}
}

// The disk holding / must never be offered, whatever its bus.
func TestListDisksExcludesRoot(t *testing.T) {
	disks, err := ListDisks()
	if err != nil {
		t.Skip("no /sys/block:", err)
	}
	root := diskUsage()
	for _, d := range disks {
		for _, mp := range root[d.Path[len("/dev/"):]] {
			if isSystemMount(mp) || mp == "[SWAP]" {
				t.Errorf("%s holds %s but is offered", d.Path, mp)
			}
		}
	}
	found := false
	for _, mounts := range root {
		for _, mp := range mounts {
			found = found || mp == "/"
		}
	}
	if !found {
		t.Log("could not map / to a disk on this machine (container or btrfs); check skipped")
	}
}
