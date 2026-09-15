// A raw block device the image is written to, plus disk discovery, per platform.
package main

import (
	"fmt"
	"io"
	"strings"
)

type Disk struct {
	Path      string // /dev/sdb or \\.\PhysicalDrive2
	Model     string
	SizeBytes int64
	Mounts    []string // mounted volumes/letters that must be dismounted first
}

// RawDevice is an opened disk. Sync must leave the device so that the next Read comes from
// the medium rather than an operating system cache, or the read-back verification proves
// nothing.
type RawDevice interface {
	io.ReadWriteCloser
	Sync() error
	SeekStart() error
}

// maxCardBytes bounds what is offered: SDXC stops at 2 TB, and a larger USB disk is far more
// likely someone's backup drive than a card reader.
const maxCardBytes = 2 << 40

// ListDisks is the platform's candidate disks (removable, USB or SD, holding no part of the
// running system) that could be an SD card.
func ListDisks() ([]Disk, error) {
	all, err := listDisks()
	if err != nil {
		return nil, err
	}
	var out []Disk
	for _, d := range all {
		if d.SizeBytes > 0 && d.SizeBytes <= maxCardBytes {
			out = append(out, d)
		}
	}
	return out, nil
}

func humanSize(b int64) string {
	v, unit := float64(b)/(1<<20), "MB"
	if b >= 1<<30 {
		v, unit = float64(b)/(1<<30), "GB"
	}
	return strings.TrimSuffix(fmt.Sprintf("%.1f", v), ".0") + " " + unit
}
