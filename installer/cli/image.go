// Image handling: decompress the .img.xz, drop the configuration files into the FAT boot
// partition, and stream the result to the SD card with progress and verification.
package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"

	diskfs "github.com/diskfs/go-diskfs"
	"github.com/diskfs/go-diskfs/filesystem"
	"github.com/ulikunitz/xz"
)

// Decompress writes image.img.xz to a temporary .img file (the image is small: DietPi plus
// the two apps' sources; the first boot grows partitions on the real card).
func Decompress(src, workDir string, progress func(done int64)) (string, error) {
	in, err := os.Open(src)
	if err != nil {
		return "", err
	}
	defer in.Close()
	r, err := xz.NewReader(bufio.NewReaderSize(in, 4<<20))
	if err != nil {
		return "", fmt.Errorf("not an xz image: %w", err)
	}
	dst := filepath.Join(workDir, "pitv.img")
	out, err := os.Create(dst)
	if err != nil {
		return "", err
	}
	defer out.Close()
	var done int64
	buf := make([]byte, 4<<20)
	for {
		n, rerr := r.Read(buf)
		if n > 0 {
			if _, err := out.Write(buf[:n]); err != nil {
				return "", err
			}
			done += int64(n)
			if progress != nil {
				progress(done)
			}
		}
		if rerr == io.EOF {
			break
		}
		if rerr != nil {
			return "", rerr
		}
	}
	return dst, nil
}

// PatchBoot writes files into partition 1 (FAT) of the image: the install configuration,
// DietPi's automation files and, when Wi-Fi is on, dietpi-wifi.txt.
func PatchBoot(img string, files map[string][]byte) error {
	d, err := diskfs.Open(img)
	if err != nil {
		return err
	}
	defer d.Close()
	fs, err := d.GetFilesystem(1)
	if err != nil {
		return fmt.Errorf("boot partition: %w", err)
	}
	for name, data := range files {
		f, err := fs.OpenFile("/"+name, os.O_CREATE|os.O_RDWR|os.O_TRUNC)
		if err != nil {
			return fmt.Errorf("%s: %w", name, err)
		}
		if _, err := f.Write(data); err != nil {
			f.Close()
			return fmt.Errorf("%s: %w", name, err)
		}
		f.Close()
	}
	return nil
}

var _ filesystem.FileSystem = nil

// WriteImage streams the .img to the raw device, then reads the written range back and
// compares SHA-256 so a bad card or a cable hiccup is caught before the first boot.
func WriteImage(img string, dev RawDevice, progress func(done, total int64, phase string)) error {
	in, err := os.Open(img)
	if err != nil {
		return err
	}
	defer in.Close()
	st, err := in.Stat()
	if err != nil {
		return err
	}
	total := st.Size()
	hasher := sha256.New()
	buf := make([]byte, 4<<20)
	var done int64
	start := time.Now()
	for {
		n, rerr := in.Read(buf)
		if n > 0 {
			hasher.Write(buf[:n])
			if _, err := dev.Write(buf[:n]); err != nil {
				return fmt.Errorf("write failed after %d MB: %w", done>>20, err)
			}
			done += int64(n)
			if progress != nil {
				progress(done, total, "writing")
			}
		}
		if rerr == io.EOF {
			break
		}
		if rerr != nil {
			return rerr
		}
	}
	if err := dev.Sync(); err != nil {
		return err
	}
	_ = start
	want := hex.EncodeToString(hasher.Sum(nil))
	if err := dev.SeekStart(); err != nil {
		return err
	}
	verifier := sha256.New()
	var read int64
	for read < total {
		chunk := int64(len(buf))
		if total-read < chunk {
			chunk = total - read
		}
		n, err := io.ReadFull(dev, buf[:chunk])
		if err != nil {
			return fmt.Errorf("verify read failed at %d MB: %w", read>>20, err)
		}
		verifier.Write(buf[:n])
		read += int64(n)
		if progress != nil {
			progress(read, total, "verifying")
		}
	}
	if got := hex.EncodeToString(verifier.Sum(nil)); got != want {
		return fmt.Errorf("verification failed: the card does not match the image (bad card?)")
	}
	return nil
}
