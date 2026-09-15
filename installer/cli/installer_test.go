package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"

	diskfs "github.com/diskfs/go-diskfs"
	"github.com/diskfs/go-diskfs/disk"
	"github.com/diskfs/go-diskfs/filesystem"
	"github.com/diskfs/go-diskfs/partition/mbr"
	"golang.org/x/crypto/ssh"
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

func TestValidateRejectsShellHostileValues(t *testing.T) {
	good := DefaultConfig()
	good.MaintenanceUser = MaintenanceUser{Name: "pete", Password: "x"}
	if err := good.Validate(); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name string
		mut  func(c *Config)
		want string
	}{
		{"nas host with sed delimiter", func(c *Config) { c.NAS.Host = "nas|evil" }, "NAS host"},
		{"share with quote", func(c *Config) { c.NAS.Shares = []string{"tv'shows"} }, "share names"},
		{"timezone with newline", func(c *Config) { c.Timezone = "Europe/London\nX=1" }, "timezone"},
		{"password with newline", func(c *Config) { c.NAS.Password = "a\nAUTO_SETUP=1" }, "line breaks"},
		{"relative cache dir", func(c *Config) { c.WorkDrive.CacheDir = "cache" }, "cache_dir"},
		{"device outside /dev", func(c *Config) { c.WorkDrive.Device = "/etc/passwd" }, "work drive"},
		{"gateway with shell", func(c *Config) { c.Network.Gateway = "$(reboot)" }, "addresses"},
		{"ssh key not a key", func(c *Config) { c.MaintenanceUser.SSHAuthorizedKeys = []string{"echo hi"} }, "public keys"},
	}
	for _, tc := range cases {
		c := good
		c.NAS.Shares = append([]string(nil), good.NAS.Shares...)
		tc.mut(&c)
		err := c.Validate()
		if err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Errorf("%s: expected an error mentioning %q, got %v", tc.name, tc.want, err)
		}
	}
}

func testKey(t *testing.T) ssh.PublicKey {
	t.Helper()
	pub, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	key, err := ssh.NewPublicKey(pub)
	if err != nil {
		t.Fatal(err)
	}
	return key
}

func TestHostKeyTrustOnFirstUse(t *testing.T) {
	path := filepath.Join(t.TempDir(), "ssh", "known_hosts")
	remote := &net.TCPAddr{IP: net.IPv4(192, 168, 1, 20), Port: 22}
	key := testKey(t)

	refuse, err := hostKeyCallback(path, func(string, string) bool { return false })
	if err != nil {
		t.Fatal(err)
	}
	if err := refuse("pitv:22", remote, key); err == nil {
		t.Fatal("an unknown host must be refused when the user does not trust it")
	}

	asked := 0
	accept, err := hostKeyCallback(path, func(host, fp string) bool {
		asked++
		if host != "pitv:22" || !strings.HasPrefix(fp, "SHA256:") {
			t.Errorf("prompt got %q %q", host, fp)
		}
		return true
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := accept("pitv:22", remote, key); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(path)
	if !strings.HasPrefix(string(data), "pitv ssh-ed25519 ") {
		t.Fatalf("known_hosts not written: %q", data)
	}

	// Second connection: known, no prompt.
	silent, err := hostKeyCallback(path, func(string, string) bool { t.Fatal("prompted for a known host"); return false })
	if err != nil {
		t.Fatal(err)
	}
	if err := silent("pitv:22", remote, key); err != nil {
		t.Fatal(err)
	}
	// A different key for the same host is an impersonation attempt, not a new host.
	if err := silent("pitv:22", remote, testKey(t)); err == nil || !strings.Contains(err.Error(), "does not match") {
		t.Fatalf("changed key must be refused, got %v", err)
	}
}
