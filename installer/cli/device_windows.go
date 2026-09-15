//go:build windows

package main

import (
	"encoding/json"
	"fmt"
	"os/exec"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/windows"
)

const adminHint = "run it from an administrator prompt (right-click, Run as administrator)"

type psDisk struct {
	Number       int    `json:"Number"`
	FriendlyName string `json:"FriendlyName"`
	Size         int64  `json:"Size"`
	BusType      string `json:"BusType"`
	IsSystem     bool   `json:"IsSystem"`
	IsBoot       bool   `json:"IsBoot"`
}

// listDisks uses PowerShell's Get-Disk: the USB and SD disks that are neither the system nor
// the boot disk.
func listDisks() ([]Disk, error) {
	out, err := exec.Command("powershell", "-NoProfile", "-Command",
		"Get-Disk | Select-Object Number,FriendlyName,Size,BusType,IsSystem,IsBoot | ConvertTo-Json -Compress").Output()
	if err != nil {
		return nil, fmt.Errorf("Get-Disk failed: %w", err)
	}
	text := strings.TrimSpace(string(out))
	if text == "" {
		return nil, nil
	}
	if !strings.HasPrefix(text, "[") { // ConvertTo-Json emits a bare object for a single disk
		text = "[" + text + "]"
	}
	var disks []psDisk
	if err := json.Unmarshal([]byte(text), &disks); err != nil {
		return nil, fmt.Errorf("Get-Disk output: %w", err)
	}
	var res []Disk
	for _, d := range disks {
		bus := strings.ToUpper(d.BusType)
		if d.IsSystem || d.IsBoot || (bus != "USB" && bus != "SD" && bus != "MMC") {
			continue
		}
		letters, err := volumeLetters(d.Number)
		if err != nil {
			return nil, err
		}
		res = append(res, Disk{Path: fmt.Sprintf(`\\.\PhysicalDrive%d`, d.Number), Model: d.FriendlyName, SizeBytes: d.Size, Mounts: letters})
	}
	return res, nil
}

func volumeLetters(number int) ([]string, error) {
	out, err := exec.Command("powershell", "-NoProfile", "-Command",
		fmt.Sprintf("Get-Partition -DiskNumber %d | Where-Object DriveLetter | ForEach-Object { $_.DriveLetter }", number)).Output()
	if err != nil {
		return nil, fmt.Errorf("Get-Partition for disk %d failed: %w", number, err)
	}
	var letters []string
	for _, l := range strings.Fields(string(out)) {
		letters = append(letters, l+":")
	}
	return letters, nil
}

type winDevice struct {
	h       windows.Handle
	volumes []windows.Handle
}

const (
	fsctlLockVolume     = 0x00090018
	fsctlDismountVolume = 0x00090020
)

func (d *winDevice) Write(p []byte) (int, error) {
	var n uint32
	err := windows.WriteFile(d.h, p, &n, nil)
	return int(n), err
}

func (d *winDevice) Read(p []byte) (int, error) {
	var n uint32
	err := windows.ReadFile(d.h, p, &n, nil)
	if err == nil && n == 0 {
		return 0, syscall.EIO
	}
	return int(n), err
}

// Sync flushes; the handle is opened without buffering, so reads already come from the card.
func (d *winDevice) Sync() error { return windows.FlushFileBuffers(d.h) }

func (d *winDevice) SeekStart() error {
	_, err := windows.SetFilePointer(d.h, 0, nil, windows.FILE_BEGIN)
	return err
}

// Close releases the disk and then the volume locks, which lets Windows mount the new
// partitions.
func (d *winDevice) Close() error {
	var err error
	if d.h != 0 {
		err = windows.CloseHandle(d.h)
	}
	for _, v := range d.volumes {
		windows.CloseHandle(v)
	}
	return err
}

func openHandle(path string, flags uint32) (windows.Handle, error) {
	p, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return 0, err
	}
	return windows.CreateFile(p, windows.GENERIC_READ|windows.GENERIC_WRITE,
		windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE, nil, windows.OPEN_EXISTING, flags, 0)
}

// lockVolume takes the exclusive lock Windows requires before raw writes to a mounted volume's
// sectors. Explorer and indexers hold short-lived handles, so a busy volume is retried for a
// few seconds before giving up.
func lockVolume(h windows.Handle) error {
	var ret uint32
	var err error
	for range 20 {
		if err = windows.DeviceIoControl(h, fsctlLockVolume, nil, 0, nil, 0, &ret, nil); err == nil {
			return nil
		}
		time.Sleep(250 * time.Millisecond)
	}
	return err
}

// OpenDisk locks and dismounts every volume on the disk (so Windows neither writes to it nor
// re-mounts it half way through), then opens the physical drive for unbuffered raw access.
// The volume handles stay open until Close, because closing them releases the locks.
func OpenDisk(d Disk) (RawDevice, error) {
	dev := &winDevice{}
	for _, letter := range d.Mounts {
		h, err := openHandle(`\\.\`+letter, 0)
		if err != nil {
			dev.Close()
			return nil, fmt.Errorf("open volume %s: %w", letter, err)
		}
		dev.volumes = append(dev.volumes, h)
		if err := lockVolume(h); err != nil {
			dev.Close()
			return nil, fmt.Errorf("volume %s is in use; close any window or program using it (%w)", letter, err)
		}
		var ret uint32
		if err := windows.DeviceIoControl(h, fsctlDismountVolume, nil, 0, nil, 0, &ret, nil); err != nil {
			dev.Close()
			return nil, fmt.Errorf("dismount %s: %w", letter, err)
		}
	}
	h, err := openHandle(d.Path, windows.FILE_FLAG_NO_BUFFERING|windows.FILE_FLAG_WRITE_THROUGH)
	if err != nil {
		dev.Close()
		return nil, fmt.Errorf("open %s: %w", d.Path, err)
	}
	dev.h = h
	return dev, nil
}

// Rescan is a no-op: Windows picks up the new partition table when the volume locks are released.
func Rescan(Disk) {}

func IsAdmin() bool {
	var sid *windows.SID
	err := windows.AllocateAndInitializeSid(&windows.SECURITY_NT_AUTHORITY, 2, windows.SECURITY_BUILTIN_DOMAIN_RID,
		windows.DOMAIN_ALIAS_RID_ADMINS, 0, 0, 0, 0, 0, 0, &sid)
	if err != nil {
		return false
	}
	defer windows.FreeSid(sid)
	member, err := windows.Token(0).IsMember(sid)
	return err == nil && member
}
