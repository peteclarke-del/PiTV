//go:build windows

package main

import (
	"encoding/json"
	"fmt"
	"os/exec"
	"strings"
	"syscall"

	"golang.org/x/sys/windows"
)

type psDisk struct {
	Number       int    `json:"Number"`
	FriendlyName string `json:"FriendlyName"`
	Size         int64  `json:"Size"`
	BusType      string `json:"BusType"`
	IsSystem     bool   `json:"IsSystem"`
	IsBoot       bool   `json:"IsBoot"`
}

// ListDisks uses PowerShell's Get-Disk; only USB/SD disks that are not the system or boot
// disk are offered.
func ListDisks() ([]Disk, error) {
	cmd := exec.Command("powershell", "-NoProfile", "-Command",
		"Get-Disk | Select-Object Number,FriendlyName,Size,BusType,IsSystem,IsBoot | ConvertTo-Json -Compress")
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("Get-Disk failed: %w", err)
	}
	text := strings.TrimSpace(string(out))
	if !strings.HasPrefix(text, "[") {
		text = "[" + text + "]"
	}
	var disks []psDisk
	if err := json.Unmarshal([]byte(text), &disks); err != nil {
		return nil, err
	}
	var res []Disk
	for _, d := range disks {
		if d.IsSystem || d.IsBoot {
			continue
		}
		bus := strings.ToUpper(d.BusType)
		if bus != "USB" && bus != "SD" && bus != "MMC" {
			continue
		}
		disk := Disk{Path: fmt.Sprintf(`\\.\PhysicalDrive%d`, d.Number), Model: d.FriendlyName, SizeBytes: d.Size, Removable: true}
		disk.Mounts = volumeLetters(d.Number)
		res = append(res, disk)
	}
	return res, nil
}

func volumeLetters(number int) []string {
	cmd := exec.Command("powershell", "-NoProfile", "-Command",
		fmt.Sprintf("Get-Partition -DiskNumber %d | Where-Object DriveLetter | ForEach-Object { $_.DriveLetter }", number))
	out, _ := cmd.Output()
	var letters []string
	for _, l := range strings.Fields(string(out)) {
		letters = append(letters, strings.TrimSpace(l)+":")
	}
	return letters
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

func (d *winDevice) Sync() error { return windows.FlushFileBuffers(d.h) }

func (d *winDevice) SeekStart() error {
	_, err := windows.SetFilePointer(d.h, 0, nil, windows.FILE_BEGIN)
	return err
}

func (d *winDevice) Close() error {
	for _, v := range d.volumes {
		windows.CloseHandle(v)
	}
	return windows.CloseHandle(d.h)
}

// OpenDisk locks and dismounts every volume on the disk (so Windows does not write to it
// or re-mount it half way through), then opens the physical drive for raw access.
func OpenDisk(d Disk) (RawDevice, error) {
	if !IsAdmin() {
		return nil, fmt.Errorf("writing to %s needs an administrator prompt: right-click, Run as administrator", d.Path)
	}
	dev := &winDevice{}
	for _, letter := range d.Mounts {
		path, _ := windows.UTF16PtrFromString(`\\.\` + letter)
		h, err := windows.CreateFile(path, windows.GENERIC_READ|windows.GENERIC_WRITE,
			windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE, nil, windows.OPEN_EXISTING, 0, 0)
		if err != nil {
			continue
		}
		var ret uint32
		_ = windows.DeviceIoControl(h, fsctlLockVolume, nil, 0, nil, 0, &ret, nil)
		_ = windows.DeviceIoControl(h, fsctlDismountVolume, nil, 0, nil, 0, &ret, nil)
		dev.volumes = append(dev.volumes, h)
	}
	path, _ := windows.UTF16PtrFromString(d.Path)
	h, err := windows.CreateFile(path, windows.GENERIC_READ|windows.GENERIC_WRITE,
		windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE, nil, windows.OPEN_EXISTING, windows.FILE_FLAG_NO_BUFFERING|windows.FILE_FLAG_WRITE_THROUGH, 0)
	if err != nil {
		dev.Close()
		return nil, fmt.Errorf("open %s: %w", d.Path, err)
	}
	dev.h = h
	return dev, nil
}

func Rescan(d Disk) {}

func IsAdmin() bool {
	var sid *windows.SID
	err := windows.AllocateAndInitializeSid(&windows.SECURITY_NT_AUTHORITY, 2, windows.SECURITY_BUILTIN_DOMAIN_RID,
		windows.DOMAIN_ALIAS_RID_ADMINS, 0, 0, 0, 0, 0, 0, &sid)
	if err != nil {
		return false
	}
	defer windows.FreeSid(sid)
	token := windows.Token(0)
	member, err := token.IsMember(sid)
	return err == nil && member
}
