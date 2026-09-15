package main

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	diskfs "github.com/diskfs/go-diskfs"
	"github.com/diskfs/go-diskfs/disk"
	"github.com/diskfs/go-diskfs/filesystem"
	"github.com/diskfs/go-diskfs/partition/mbr"
	"github.com/ulikunitz/xz"
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

func TestSaveIsPrivate(t *testing.T) {
	path := filepath.Join(t.TempDir(), "pitv-install.json")
	if err := os.WriteFile(path, []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	c := DefaultConfig()
	c.MaintenanceUser = MaintenanceUser{Name: "pete", Password: "x"}
	if err := c.Save(path); err != nil {
		t.Fatal(err)
	}
	st, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if perm := st.Mode().Perm(); perm&0o077 != 0 && os.PathSeparator == '/' {
		t.Fatalf("saved with mode %o; the file holds passwords", perm)
	}
	loaded, err := LoadConfig(path)
	if err != nil || loaded.MaintenanceUser.Name != "pete" {
		t.Fatalf("round trip: %v %+v", err, loaded.MaintenanceUser)
	}
}

func TestDietpiTxt(t *testing.T) {
	c := DefaultConfig()
	c.MaintenanceUser = MaintenanceUser{Name: "pete", Password: "secret", SSHAuthorizedKeys: []string{"ssh-ed25519 AAAA pete"}}
	c.Wifi = Wifi{Enabled: true, Country: "GB", SSID: "Home", PSK: "it's a key"}
	txt, err := DietpiTxt(c)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"AUTO_SETUP_NET_HOSTNAME=pitv", "AUTO_SETUP_NET_WIFI_ENABLED=1",
		"AUTO_SETUP_AUTOMATED=1", "AUTO_SETUP_CUSTOM_SCRIPT_EXEC=1"} {
		if !strings.Contains(txt, want) {
			t.Errorf("missing %q", want)
		}
	}
	// root and dietpi get a throwaway password and no key: only the maintenance user logs in.
	if strings.Contains(txt, "secret") || strings.Contains(txt, "ssh-ed25519") || strings.Contains(txt, "@") {
		t.Errorf("maintenance credentials or an unfilled placeholder in dietpi.txt:\n%s", txt)
	}
	other, _ := DietpiTxt(c)
	if other == txt {
		t.Error("the global password must be random per card")
	}
	wifi, _ := DietpiWifiTxt(c)
	if !strings.Contains(wifi, `aWIFI_KEY[0]='it'\''s a key'`) || !strings.Contains(wifi, `aWIFI_SSID[0]='Home'`) {
		t.Errorf("wifi values not quoted: %s", wifi)
	}
}

// A tiny MBR image stands in for DietPi's: a FAT32 boot partition and a root partition.
func dietpiLikeImage(t *testing.T) (string, *disk.Disk) {
	t.Helper()
	img := filepath.Join(t.TempDir(), "t.img")
	d, err := diskfs.Create(img, 64<<20, diskfs.Raw, diskfs.SectorSizeDefault)
	if err != nil {
		t.Fatal(err)
	}
	table := &mbr.Table{Partitions: []*mbr.Partition{
		{Bootable: true, Type: mbr.Fat32LBA, Start: 2048, Size: (48 << 20) / 512},
		{Type: mbr.Linux, Start: 2048 + (48<<20)/512, Size: (12 << 20) / 512},
	}}
	if err := d.Partition(table); err != nil {
		t.Fatal(err)
	}
	return img, d
}

func TestPatchBoot(t *testing.T) {
	img, d := dietpiLikeImage(t)
	if _, err := d.CreateFilesystem(disk.FilesystemSpec{Partition: 1, FSType: filesystem.TypeFat32, VolumeLabel: "boot"}); err != nil {
		t.Fatal(err)
	}
	d.Close()
	edits := map[string]bootEdit{"pitv-install.json": replaceWith([]byte(`{"a":1}`)), "dietpi.txt": replaceWith([]byte("X=1\n"))}
	if err := PatchBoot(img, edits); err != nil {
		t.Fatal(err)
	}
	var seen []byte
	if err := PatchBoot(img, map[string]bootEdit{"dietpi.txt": func(old []byte) []byte { seen = old; return old }}); err != nil {
		t.Fatal(err)
	}
	if string(seen) != "X=1\n" {
		t.Fatalf("an edit must see the current content, got %q", seen)
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
	if err := PatchBoot(img, map[string]bootEdit{"../escape": replaceWith(nil)}); err == nil {
		t.Fatal("a boot file name with a path must be refused")
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
		{"share with a unit specifier", func(c *Config) { c.NAS.Shares = []string{"tv%41"} }, "share names"},
		{"share with a space", func(c *Config) { c.NAS.Shares = []string{"music videos"} }, "share names"},
		{"service account as maintenance user", func(c *Config) { c.MaintenanceUser.Name = "pitv" }, "may not be"},
		{"no maintenance password", func(c *Config) { c.MaintenanceUser.Password = "" }, "needs a password"},
		{"short wifi key", func(c *Config) { c.Wifi = Wifi{Enabled: true, Country: "GB", SSID: "Home", PSK: "short"} }, "wifi password"},
		{"lower-case country", func(c *Config) { c.Wifi = Wifi{Enabled: true, Country: "gb", SSID: "Home", PSK: "long enough"} }, "country"},
		{"cache drive mounted over /var", func(c *Config) { c.WorkDrive.CacheDir = "/var/pitv" }, "cache_dir"},
		{"cache dir escaping /mnt", func(c *Config) { c.WorkDrive.CacheDir = "/mnt/../etc/pitv" }, "cache_dir"},
		{"cache dir without a drive folder", func(c *Config) { c.WorkDrive.CacheDir = "/mnt/pitv" }, "cache_dir"},
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

	refuse, err := openKnownHosts(path, func(string, string) bool { return false })
	if err != nil {
		t.Fatal(err)
	}
	if algos := refuse.Algorithms("pitv:22"); algos != nil {
		t.Fatalf("unknown host has algorithms %v", algos)
	}
	if err := refuse.Callback("pitv:22", remote, key); err == nil {
		t.Fatal("an unknown host must be refused when the user does not trust it")
	}

	asked := 0
	accept, err := openKnownHosts(path, func(host, fp string) bool {
		asked++
		if host != "pitv:22" || !strings.HasPrefix(fp, "SHA256:") {
			t.Errorf("prompt got %q %q", host, fp)
		}
		return true
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := accept.Callback("pitv:22", remote, key); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(path)
	if !strings.HasPrefix(string(data), "pitv ssh-ed25519 ") {
		t.Fatalf("known_hosts not written: %q", data)
	}

	// Second connection: known, no prompt.
	silent, err := openKnownHosts(path, func(string, string) bool { t.Fatal("prompted for a known host"); return false })
	if err != nil {
		t.Fatal(err)
	}
	if err := silent.Callback("pitv:22", remote, key); err != nil {
		t.Fatal(err)
	}
	// The handshake asks for the recorded key type, not Go's first preference.
	if algos := silent.Algorithms("pitv:22"); !slices.Equal(algos, []string{ssh.KeyAlgoED25519}) {
		t.Fatalf("algorithms %v", algos)
	}
	// A different key for the same host is an impersonation attempt, not a new host.
	if err := silent.Callback("pitv:22", remote, testKey(t)); err == nil || !strings.Contains(err.Error(), "does not match") {
		t.Fatalf("changed key must be refused, got %v", err)
	}
}

func xzFile(t *testing.T, data []byte) string {
	t.Helper()
	var buf bytes.Buffer
	w, err := xz.NewWriter(&buf)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := w.Write(data); err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "pitv-test.img.xz")
	if err := os.WriteFile(path, buf.Bytes(), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestDecompressChecksumAndCap(t *testing.T) {
	data := bytes.Repeat([]byte("PiTV"), 1<<16)
	src := xzFile(t, data)
	compressed, _ := os.ReadFile(src)
	sum := sha256.Sum256(compressed)
	good := hex.EncodeToString(sum[:])

	img, err := Decompress(src, t.TempDir(), good, 1<<30, nil)
	if err != nil {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(img); !bytes.Equal(got, data) {
		t.Fatal("decompressed data differs")
	}
	if _, err := Decompress(src, t.TempDir(), strings.Repeat("0", 64), 1<<30, nil); err == nil || !strings.Contains(err.Error(), ".sha256") {
		t.Fatalf("a wrong checksum must fail, got %v", err)
	}
	if _, err := Decompress(src, t.TempDir(), "", int64(len(data))-1, nil); !errors.Is(err, errTooLarge) {
		t.Fatalf("an image larger than the card must fail, got %v", err)
	}

	if err := os.WriteFile(src+".sha256", []byte(good+"  pitv-test.img.xz\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if want, err := expectedSHA256(src); err != nil || want != good {
		t.Fatalf("expectedSHA256 = %q, %v", want, err)
	}
	if want, err := expectedSHA256(src + ".other"); err != nil || want != "" {
		t.Fatalf("no checksum file must mean no check, got %q, %v", want, err)
	}
}

// memDevice is a card in memory; flip corrupts the byte at that offset on read-back.
type memDevice struct {
	data []byte
	pos  int
	flip int
}

func (m *memDevice) Write(p []byte) (int, error) {
	m.data = append(m.data[:m.pos], p...)
	m.pos += len(p)
	return len(p), nil
}

func (m *memDevice) Read(p []byte) (int, error) {
	if m.pos >= len(m.data) {
		return 0, io.EOF
	}
	n := copy(p, m.data[m.pos:])
	if m.flip >= m.pos && m.flip < m.pos+n {
		p[m.flip-m.pos] ^= 0xff
	}
	m.pos += n
	return n, nil
}

func (m *memDevice) Sync() error      { return nil }
func (m *memDevice) SeekStart() error { m.pos = 0; return nil }
func (m *memDevice) Close() error     { return nil }

func TestWriteImagePadsAndVerifies(t *testing.T) {
	img := filepath.Join(t.TempDir(), "t.img")
	if err := os.WriteFile(img, bytes.Repeat([]byte{7}, 5*sectorAlign+100), 0o600); err != nil {
		t.Fatal(err)
	}
	good := &memDevice{flip: -1}
	if err := WriteImage(img, good, nil); err != nil {
		t.Fatal(err)
	}
	if len(good.data) != 6*sectorAlign || good.data[len(good.data)-1] != 0 {
		t.Fatalf("wrote %d bytes; want the image padded with zeros to %d", len(good.data), 6*sectorAlign)
	}
	if err := WriteImage(img, &memDevice{flip: 3 * sectorAlign}, nil); err == nil || !strings.Contains(err.Error(), "verification") {
		t.Fatalf("a card that reads back differently must fail, got %v", err)
	}
}

func TestTarTreeSkipsSecretsAndKeepsLinks(t *testing.T) {
	root := t.TempDir()
	for name, body := range map[string]string{"setup/install.sh": "#!/bin/sh\n", "pitv-install.json": "{}", ".git/config": "x", "dist/pitv.img.xz": "x"} {
		p := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink("setup/install.sh", filepath.Join(root, "link")); err != nil {
		t.Skip("symlinks unavailable:", err)
	}
	var buf bytes.Buffer
	if err := tarTree(&buf, root); err != nil {
		t.Fatal(err)
	}
	gz, err := gzip.NewReader(&buf)
	if err != nil {
		t.Fatal(err)
	}
	names := map[string]string{}
	tr := tar.NewReader(gz)
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		names[hdr.Name] = hdr.Linkname
	}
	if _, ok := names["setup/install.sh"]; !ok {
		t.Errorf("sources missing: %v", names)
	}
	for _, secret := range []string{"pitv-install.json", ".git/config", "dist/pitv.img.xz"} {
		if _, ok := names[secret]; ok {
			t.Errorf("%s must not be uploaded", secret)
		}
	}
	if names["link"] != "setup/install.sh" {
		t.Errorf("symlink not kept as a link: %q", names["link"])
	}
}

func TestHumanSize(t *testing.T) {
	for in, want := range map[int64]string{512 << 20: "512 MB", 32 << 30: "32 GB", 15_931_539_456: "14.8 GB"} {
		if got := humanSize(in); got != want {
			t.Errorf("humanSize(%d) = %q, want %q", in, got, want)
		}
	}
}

func TestAddWorkPartition(t *testing.T) {
	img, d := dietpiLikeImage(t)
	d.Close()
	if err := AddWorkPartition(img, 1<<30, 1); err == nil || !strings.Contains(err.Error(), "no room") {
		t.Fatalf("a card with no room after the system partition must fail, got %v", err)
	}
	if err := AddWorkPartition(img, 4<<30, 1); err != nil {
		t.Fatal(err)
	}
	d2, err := diskfs.Open(img)
	if err != nil {
		t.Fatal(err)
	}
	defer d2.Close()
	table, err := d2.GetPartitionTable()
	if err != nil {
		t.Fatal(err)
	}
	parts := table.(*mbr.Table).Partitions
	if len(parts) < 3 || parts[2].Type != mbr.Linux || parts[2].Start != (1<<30)/512 || parts[2].Size != (3<<30)/512 {
		t.Fatalf("partition 3 = %+v", parts[2])
	}
	if err := AddWorkPartition(img, 4<<30, 1); err == nil {
		t.Fatal("a second work partition must be refused")
	}
}

func TestMergeDietpiTxt(t *testing.T) {
	base := "# DietPi\r\nAUTO_SETUP_LOCALE=C.UTF-8\r\n  AUTO_SETUP_AUTOMATED=0\nCONFIG_GPU_DRIVER=none\nAUTO_SETUP_LOCALE=dup\n"
	got := string(mergeDietpiTxt([]byte(base), "# ours\nAUTO_SETUP_LOCALE=en_GB.UTF-8\nAUTO_SETUP_AUTOMATED=1\nSURVEY_OPTED_IN=0\n"))
	want := "# DietPi\nAUTO_SETUP_LOCALE=en_GB.UTF-8\nAUTO_SETUP_AUTOMATED=1\nCONFIG_GPU_DRIVER=none\nSURVEY_OPTED_IN=0\n"
	if got != want {
		t.Fatalf("got\n%s\nwant\n%s", got, want)
	}
	if got := string(mergeDietpiTxt(nil, "A=1\n")); got != "A=1\n" {
		t.Fatalf("no base: got %q", got)
	}
}

// The example shipped on every card must pass the installer's own validation.
func TestExampleConfigIsValid(t *testing.T) {
	if _, err := LoadConfig("../config/pitv-install.example.json"); err != nil {
		t.Fatal(err)
	}
}
