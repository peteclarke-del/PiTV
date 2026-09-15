// Image handling: decompress the .img.xz, drop the configuration files into the FAT boot
// partition, and stream the result to the SD card with progress and verification.
package main

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"math"
	"os"
	"path/filepath"
	"strings"
	"unsafe"

	diskfs "github.com/diskfs/go-diskfs"
	"github.com/ulikunitz/xz"
)

// sectorAlign is the unit every device write is padded and aligned to. Unbuffered writes on
// Windows must be whole sectors from sector-aligned memory; 4096 covers 512-byte and 4K cards.
const sectorAlign = 4096

// expectedSHA256 reads <image>.sha256 (sha256sum format), written by installer/image/build.sh.
// The checksum travels with the image, so it catches a truncated or corrupted copy rather than
// a substituted one; "" means there is no checksum file.
func expectedSHA256(image string) (string, error) {
	data, err := os.ReadFile(image + ".sha256")
	if errors.Is(err, fs.ErrNotExist) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	fields := strings.Fields(string(data))
	if len(fields) == 0 || len(fields[0]) != sha256.Size*2 {
		return "", fmt.Errorf("%s.sha256 is not a sha256sum line", image)
	}
	return strings.ToLower(fields[0]), nil
}

var errTooLarge = errors.New("the image is larger than the card")

// cappedWriter stops a decompression at limit bytes: a damaged or hostile .xz cannot fill the
// host's disk, and an image that would not fit the card fails before anything is written.
type cappedWriter struct {
	w        io.Writer
	n, limit int64
	progress func(int64)
}

func (c *cappedWriter) Write(p []byte) (int, error) {
	if c.n+int64(len(p)) > c.limit {
		return 0, errTooLarge
	}
	n, err := c.w.Write(p)
	c.n += int64(n)
	if c.progress != nil {
		c.progress(c.n)
	}
	return n, err
}

// Decompress writes src (.img.xz) to workDir/pitv.img, refusing to produce more than limit
// bytes. When wantSHA is set, the compressed file must hash to it.
func Decompress(src, workDir, wantSHA string, limit int64, progress func(done int64)) (string, error) {
	in, err := os.Open(src)
	if err != nil {
		return "", err
	}
	defer in.Close()
	sum := sha256.New()
	tee := io.TeeReader(in, sum)
	r, err := xz.NewReader(bufio.NewReaderSize(tee, 1<<20))
	if err != nil {
		return "", fmt.Errorf("not an xz image: %w", err)
	}
	dst := filepath.Join(workDir, "pitv.img")
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return "", err
	}
	_, err = io.CopyBuffer(&cappedWriter{w: out, limit: limit, progress: progress}, r, make([]byte, 4<<20))
	if cerr := out.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		return "", err
	}
	if wantSHA != "" {
		if _, err := io.Copy(sum, in); err != nil { // anything after the last xz stream
			return "", err
		}
		if got := hex.EncodeToString(sum.Sum(nil)); got != wantSHA {
			return "", fmt.Errorf("%s does not match its .sha256 (corrupted copy?)", filepath.Base(src))
		}
	}
	return dst, nil
}

// bootEdit returns a boot file's new content from its current one (nil when absent).
type bootEdit func(old []byte) []byte

func replaceWith(data []byte) bootEdit { return func([]byte) []byte { return data } }

// PatchBoot applies edits to files in partition 1 (FAT) of the image: the install
// configuration and DietPi's automation files.
func PatchBoot(img string, edits map[string]bootEdit) error {
	d, err := diskfs.Open(img)
	if err != nil {
		return err
	}
	defer d.Close()
	bootFS, err := d.GetFilesystem(1)
	if err != nil {
		return fmt.Errorf("boot partition: %w", err)
	}
	present := map[string]bool{}
	entries, err := bootFS.ReadDir("/")
	if err != nil {
		return fmt.Errorf("boot partition: %w", err)
	}
	for _, e := range entries {
		present[strings.ToLower(e.Name())] = true
	}
	for name, edit := range edits {
		if name == "" || strings.ContainsAny(name, `/\`) || name == "." || name == ".." {
			return fmt.Errorf("boot file %q must be a plain file name", name)
		}
		var old []byte
		if present[strings.ToLower(name)] {
			f, err := bootFS.OpenFile("/"+name, os.O_RDONLY)
			if err != nil {
				return fmt.Errorf("%s: %w", name, err)
			}
			old, err = io.ReadAll(f)
			f.Close()
			if err != nil {
				return fmt.Errorf("%s: %w", name, err)
			}
		}
		f, err := bootFS.OpenFile("/"+name, os.O_CREATE|os.O_RDWR|os.O_TRUNC)
		if err != nil {
			return fmt.Errorf("%s: %w", name, err)
		}
		_, err = f.Write(edit(old))
		if cerr := f.Close(); err == nil {
			err = cerr
		}
		if err != nil {
			return fmt.Errorf("%s: %w", name, err)
		}
	}
	return nil
}

// minCardBytes is the smallest card for a system partition of systemGB GiB: the work
// partition gets at least 1 GiB.
func minCardBytes(systemGB int) int64 { return int64(systemGB+1) << 30 }

// AddWorkPartition adds MBR partition 3 from systemGB GiB to the end of the card. DietPi's
// first boot grows the root partition as far as the next partition, so this boundary is what
// sizes the system partition; pitv-firstboot.sh formats partition 3 as /work.
func AddWorkPartition(img string, cardBytes int64, systemGB int) error {
	f, err := os.OpenFile(img, os.O_RDWR, 0)
	if err != nil {
		return err
	}
	defer f.Close()
	mbr := make([]byte, 512)
	if _, err := io.ReadFull(f, mbr); err != nil {
		return err
	}
	if mbr[510] != 0x55 || mbr[511] != 0xAA {
		return errors.New("the image has no MBR partition table")
	}
	entry := func(i int) []byte { return mbr[446+16*i : 446+16*(i+1)] }
	if entry(1)[4] == 0 {
		return errors.New("the image has no root partition")
	}
	for i := 2; i < 4; i++ {
		if entry(i)[4] != 0 {
			return fmt.Errorf("the image already has a partition %d", i+1)
		}
	}
	const sector = 512
	start := uint64(systemGB) << 30 / sector
	end := min(uint64(cardBytes)/sector, math.MaxUint32) // MBR addresses 2^32-1 sectors at most
	rootEnd := uint64(binary.LittleEndian.Uint32(entry(1)[8:])) + uint64(binary.LittleEndian.Uint32(entry(1)[12:]))
	if rootEnd > start {
		return fmt.Errorf("the image's root partition is larger than %d GB", systemGB)
	}
	if cardBytes < minCardBytes(systemGB) {
		return fmt.Errorf("the card (%s) has no room for a work partition after a %d GB system partition", humanSize(cardBytes), systemGB)
	}
	e := entry(2)
	lbaOnly := []byte{0xFE, 0xFF, 0xFF} // CHS fields unused, as for any partition past 8 GB
	copy(e[1:4], lbaOnly)
	e[4] = 0x83 // Linux
	copy(e[5:8], lbaOnly)
	binary.LittleEndian.PutUint32(e[8:12], uint32(start))
	binary.LittleEndian.PutUint32(e[12:16], uint32(end-start))
	if _, err := f.WriteAt(mbr, 0); err != nil {
		return err
	}
	return f.Close()
}

func alignedBuffer(size int) []byte {
	b := make([]byte, size+sectorAlign)
	off := int(uintptr(unsafe.Pointer(&b[0])) % sectorAlign)
	if off != 0 {
		off = sectorAlign - off
	}
	return b[off : off+size]
}

// WriteImage streams the .img to the raw device, then reads the written range back and
// compares SHA-256 so a bad or counterfeit card is caught before the first boot. The last
// block is zero-padded to a whole sector; that tail lies beyond the image's partitions.
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
	total := (st.Size() + sectorAlign - 1) / sectorAlign * sectorAlign
	report := func(done int64, phase string) {
		if progress != nil {
			progress(done, total, phase)
		}
	}
	buf := alignedBuffer(4 << 20)
	written := sha256.New()
	var done int64
	for {
		n, rerr := io.ReadFull(in, buf)
		if rem := n % sectorAlign; rem != 0 {
			clear(buf[n : n+sectorAlign-rem])
			n += sectorAlign - rem
		}
		if n > 0 {
			written.Write(buf[:n])
			w, err := dev.Write(buf[:n])
			if err == nil && w != n {
				err = io.ErrShortWrite
			}
			if err != nil {
				return fmt.Errorf("write failed after %d MB: %w", done>>20, err)
			}
			done += int64(n)
			report(done, "writing")
		}
		if rerr == io.EOF || rerr == io.ErrUnexpectedEOF {
			break
		}
		if rerr != nil {
			return rerr
		}
	}
	if err := dev.Sync(); err != nil {
		return err
	}
	if err := dev.SeekStart(); err != nil {
		return err
	}
	return readBack(dev, buf, total, written.Sum(nil), report)
}

func readBack(dev RawDevice, buf []byte, total int64, want []byte, report func(int64, string)) error {
	got := sha256.New()
	var read int64
	for read < total {
		chunk := min(int64(len(buf)), total-read)
		n, err := io.ReadFull(dev, buf[:chunk])
		if err != nil {
			return fmt.Errorf("verify read failed at %d MB: %w", read>>20, err)
		}
		got.Write(buf[:n])
		read += int64(n)
		report(read, "verifying")
	}
	if !bytes.Equal(got.Sum(nil), want) {
		return errors.New("verification failed: the card does not match the image (bad or counterfeit card?)")
	}
	return nil
}
