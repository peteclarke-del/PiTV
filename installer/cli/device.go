// A raw block device the image is written to, plus disk discovery, per platform.
package main

import "io"

type Disk struct {
	Path      string // /dev/sdb or \\.\PhysicalDrive2
	Model     string
	SizeBytes int64
	Removable bool
	Mounts    []string // mounted volumes/letters that must be dismounted first
}

type RawDevice interface {
	io.Writer
	io.Reader
	Sync() error
	SeekStart() error
	Close() error
}

func humanSize(b int64) string {
	const g = 1 << 30
	if b >= g {
		return formatFloat(float64(b)/g) + " GB"
	}
	return formatFloat(float64(b)/(1<<20)) + " MB"
}

func formatFloat(f float64) string {
	return trimZero(sprintf("%.1f", f))
}

func trimZero(s string) string {
	if len(s) > 2 && s[len(s)-2:] == ".0" {
		return s[:len(s)-2]
	}
	return s
}
