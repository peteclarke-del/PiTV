package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	diskfs "github.com/diskfs/go-diskfs"
	"github.com/diskfs/go-diskfs/disk"
	"github.com/diskfs/go-diskfs/filesystem"
	"github.com/diskfs/go-diskfs/partition/mbr"
)

func TestValidate(t *testing.T) {
	c := DefaultConfig()
	if err := c.Validate(); err == nil {
		t.Fatal("default config must fail: no maintenance user")
	}
	c.MaintenanceUser = MaintenanceUser{Name: "pete", Password: "x"}
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
	c.Mode = "bogus"
	if err := c.Validate(); err == nil || !strings.Contains(err.Error(), "mode") {
		t.Fatal("bad mode must be rejected")
	}
}

func TestDietpiTxt(t *testing.T) {
	c := DefaultConfig()
	c.MaintenanceUser = MaintenanceUser{Name: "pete", Password: "secret", SSHAuthorizedKeys: []string{"ssh-ed25519 AAAA pete"}}
	c.Wifi = Wifi{Enabled: true, Country: "GB", SSID: "Home", PSK: "it's"}
	txt, err := DietpiTxt(c)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"AUTO_SETUP_NET_HOSTNAME=pitv", "AUTO_SETUP_NET_WIFI_ENABLED=1", "AUTO_SETUP_GLOBAL_PASSWORD=secret",
		"AUTO_SETUP_SSH_PUBKEY=ssh-ed25519 AAAA pete", "AUTO_SETUP_AUTOMATED=1", "AUTO_SETUP_CUSTOM_SCRIPT_EXEC=1"} {
		if !strings.Contains(txt, want) {
			t.Errorf("missing %q", want)
		}
	}
	wifi, _ := DietpiWifiTxt(c)
	if !strings.Contains(wifi, `aWIFI_KEY[0]='it'\''s'`) {
		t.Errorf("wifi key not escaped: %s", wifi)
	}
}

// A tiny MBR image with one FAT32 partition stands in for the DietPi boot partition.
func TestPatchBoot(t *testing.T) {
	img := filepath.Join(t.TempDir(), "t.img")
	d, err := diskfs.Create(img, 64<<20, diskfs.Raw, diskfs.SectorSizeDefault)
	if err != nil {
		t.Fatal(err)
	}
	table := &mbr.Table{Partitions: []*mbr.Partition{{Bootable: true, Type: mbr.Fat32LBA, Start: 2048, Size: (60 << 20) / 512}}}
	if err := d.Partition(table); err != nil {
		t.Fatal(err)
	}
	if _, err := d.CreateFilesystem(disk.FilesystemSpec{Partition: 1, FSType: filesystem.TypeFat32, VolumeLabel: "boot"}); err != nil {
		t.Fatal(err)
	}
	d.Close()
	if err := PatchBoot(img, map[string][]byte{"pitv-install.json": []byte(`{"a":1}`), "dietpi.txt": []byte("X=1\n")}); err != nil {
		t.Fatal(err)
	}
	d2, err := diskfs.Open(img)
	if err != nil {
		t.Fatal(err)
	}
	defer d2.Close()
	fs, err := d2.GetFilesystem(1)
	if err != nil {
		t.Fatal(err)
	}
	f, err := fs.OpenFile("/pitv-install.json", os.O_RDONLY)
	if err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 64)
	n, _ := f.Read(buf)
	if string(buf[:n]) != `{"a":1}` {
		t.Fatalf("got %q", buf[:n])
	}
}
