// Install configuration: the single pitv-install.json that drives DietPi's first-run
// automation and PiTV's own first-boot script.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"strings"
)

type MaintenanceUser struct {
	Name              string   `json:"name"`
	Password          string   `json:"password"`
	SSHAuthorizedKeys []string `json:"ssh_authorized_keys"`
}

type Wifi struct {
	Enabled bool   `json:"enabled"`
	Country string `json:"country"`
	SSID    string `json:"ssid"`
	PSK     string `json:"psk"`
}

type Network struct {
	Ethernet bool   `json:"ethernet"`
	StaticIP string `json:"static_ip"`
	Gateway  string `json:"gateway"`
	DNS      string `json:"dns"`
}

type NAS struct {
	Host     string   `json:"host"`
	Username string   `json:"username"`
	Password string   `json:"password"`
	Shares   []string `json:"shares"`
}

type WorkDrive struct {
	Device   string `json:"device"`
	CacheDir string `json:"cache_dir"`
}

type PiTVOpts struct {
	AdminPassword string `json:"admin_password"`
}

type ContentOpts struct {
	YoutubeCookiesFile string `json:"youtube_cookies_file"`
}

type Config struct {
	Schema            int             `json:"schema"`
	Mode              string          `json:"mode"`
	Hostname          string          `json:"hostname"`
	Timezone          string          `json:"timezone"`
	Locale            string          `json:"locale"`
	Keyboard          string          `json:"keyboard"`
	MaintenanceUser   MaintenanceUser `json:"maintenance_user"`
	Wifi              Wifi            `json:"wifi"`
	Network           Network         `json:"network"`
	NAS               NAS             `json:"nas"`
	DisplayMode       string          `json:"display_mode"`
	SystemPartitionGB int             `json:"system_partition_gb"`
	WorkDrive         WorkDrive       `json:"work_drive"`
	PiTV              PiTVOpts        `json:"pitv"`
	PiTVContent       ContentOpts     `json:"pitv_content"`
}

// Everything below ends up in dietpi.txt, a systemd unit, fstab or a shell variable on the
// Pi, so the character sets are deliberately narrow. pitv-firstboot.sh applies the same rules
// again, because the JSON on the boot partition can be edited by hand after it is written.
var (
	hostnameRe = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`)
	userRe     = regexp.MustCompile(`^[a-z_][a-z0-9_-]{0,31}$`)
	nasHostRe  = regexp.MustCompile(`^[A-Za-z0-9]([A-Za-z0-9.-]{0,252}[A-Za-z0-9])?$`)
	shareRe    = regexp.MustCompile(`^([A-Za-z0-9._-]|%20)+$`) // %20 is the only escape: it stands for a space
	tokenRe    = regexp.MustCompile(`^[A-Za-z0-9_./+-]+$`)     // locale, keyboard layout, timezone
	addrRe     = regexp.MustCompile(`^[0-9A-Fa-f.:/]*$`)       // static IP with prefix, gateway, DNS
	countryRe  = regexp.MustCompile(`^[A-Z]{2}$`)
	deviceRe   = regexp.MustCompile(`^(auto|/dev/[A-Za-z0-9/_-]+)$`)
	pathRe     = regexp.MustCompile(`^/[A-Za-z0-9._/-]*$`)
	// The cache directory's parent is where the USB drive is mounted, so it must lie under
	// /mnt or /media: a drive mounted over /var or /etc would hide the system.
	cacheDirRe = regexp.MustCompile(`^/(mnt|media)/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)+$`)
	pskHexRe   = regexp.MustCompile(`^[0-9A-Fa-f]{64}$`)
)

// reservedUsers are accounts that exist for other reasons. As the maintenance user, root and
// dietpi would be locked out by the SSH policy, and pitv is the unprivileged service account:
// making it a sudo-capable login would hand the web service root.
var reservedUsers = map[string]bool{"root": true, "dietpi": true, "pitv": true}

func hasLineBreak(s string) bool { return strings.ContainsAny(s, "\r\n\x00") }

func DefaultConfig() Config {
	return Config{
		Schema: 1, Mode: "normal", Hostname: "pitv", Timezone: "Europe/London", Locale: "en_GB.UTF-8", Keyboard: "gb",
		Wifi: Wifi{Country: "GB"}, Network: Network{Ethernet: true},
		NAS:         NAS{Host: "synologynas", Username: "pitv", Shares: []string{"tvshows", "movies", "ads", "tvsports", "music%20videos"}},
		DisplayMode: "hdmi576", SystemPartitionGB: 8,
		WorkDrive: WorkDrive{Device: "auto", CacheDir: "/mnt/cache/pitv"},
	}
}

// readConfig parses a configuration file over the defaults without validating it, so a file
// that fails validation can still seed the wizard.
func readConfig(path string) (Config, error) {
	c := DefaultConfig()
	data, err := os.ReadFile(path)
	if err != nil {
		return c, err
	}
	if err := json.Unmarshal(data, &c); err != nil {
		return c, fmt.Errorf("%s: %w", path, err)
	}
	return c, nil
}

func LoadConfig(path string) (Config, error) {
	c, err := readConfig(path)
	if err != nil {
		return c, err
	}
	return c, c.Validate()
}

func (c Config) JSON() ([]byte, error) {
	data, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return nil, err
	}
	return append(data, '\n'), nil
}

// Save writes the file through a new 0600 temporary file and a rename: the passwords in it
// must not inherit the mode of an older, world-readable copy, and an interrupted save must
// not leave half a file.
func (c Config) Save(path string) error {
	data, err := c.JSON()
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".pitv-install-*.json")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), path)
}

func validPSK(psk string) bool {
	if pskHexRe.MatchString(psk) {
		return true
	}
	if len(psk) < 8 || len(psk) > 63 {
		return false
	}
	for _, r := range psk {
		if r < 0x20 || r > 0x7e {
			return false
		}
	}
	return true
}

func (c Config) Validate() error {
	var errs []string
	check := func(ok bool, msg string) {
		if !ok {
			errs = append(errs, msg)
		}
	}
	all := func(values []string, ok func(string) bool) bool {
		for _, v := range values {
			if !ok(v) {
				return false
			}
		}
		return true
	}
	switch c.Mode {
	case "clean", "normal", "upgrade":
	default:
		errs = append(errs, "mode must be clean, normal or upgrade")
	}
	check(hostnameRe.MatchString(c.Hostname), "hostname must be lower-case letters, digits and hyphens")
	check(userRe.MatchString(c.MaintenanceUser.Name), "maintenance user name is required (lower-case, no spaces)")
	check(!reservedUsers[c.MaintenanceUser.Name], "maintenance user may not be root, dietpi or pitv")
	check(c.MaintenanceUser.Password != "", "maintenance user needs a password (sudo asks for it)")
	check(!c.Wifi.Enabled || (c.Wifi.SSID != "" && len(c.Wifi.SSID) <= 32 && countryRe.MatchString(c.Wifi.Country)),
		"wifi needs an SSID of up to 32 bytes and a two-letter country code")
	check(!c.Wifi.Enabled || validPSK(c.Wifi.PSK), "wifi password must be 8 to 63 printable ASCII characters or 64 hex digits")
	check(c.Wifi.Enabled || c.Network.Ethernet, "enable ethernet or wifi")
	check(nasHostRe.MatchString(c.NAS.Host), "NAS host must be a host name or IP address")
	check(userRe.MatchString(strings.ToLower(c.NAS.Username)), "NAS username must be letters, digits, '_' and '-'")
	check(all(c.NAS.Shares, shareRe.MatchString), "share names may only contain letters, digits, '.', '_', '-' and %20")
	check(all([]string{c.Timezone, c.Locale, c.Keyboard}, tokenRe.MatchString), "timezone, locale and keyboard must be plain identifiers")
	check(all([]string{c.Network.StaticIP, c.Network.Gateway, c.Network.DNS}, addrRe.MatchString), "static IP, gateway and DNS must be addresses")
	check(deviceRe.MatchString(c.WorkDrive.Device), "work drive device must be 'auto' or a /dev path")
	check(cacheDirRe.MatchString(c.WorkDrive.CacheDir) && path.Clean(c.WorkDrive.CacheDir) == c.WorkDrive.CacheDir,
		"cache_dir must be a folder on the drive, below /mnt/<drive> or /media/<drive>")
	check(c.PiTVContent.YoutubeCookiesFile == "" || pathRe.MatchString(c.PiTVContent.YoutubeCookiesFile),
		"youtube_cookies_file must be an absolute path on the Pi")
	secrets := []string{c.MaintenanceUser.Password, c.NAS.Password, c.Wifi.PSK, c.Wifi.SSID, c.PiTV.AdminPassword}
	check(all(secrets, func(s string) bool { return !hasLineBreak(s) }), "passwords and the SSID must not contain line breaks")
	check(all(c.MaintenanceUser.SSHAuthorizedKeys, func(k string) bool {
		return !hasLineBreak(k) && (strings.HasPrefix(k, "ssh-") || strings.HasPrefix(k, "ecdsa-") || strings.HasPrefix(k, "sk-"))
	}), "ssh_authorized_keys entries must be single-line public keys")
	switch c.DisplayMode {
	case "hdmi576", "composite", "hdmi43", "hdmi":
	default:
		errs = append(errs, "display_mode must be hdmi576, composite, hdmi43 or hdmi")
	}
	check(c.SystemPartitionGB >= 6 && c.SystemPartitionGB <= 64, "system_partition_gb must be between 6 and 64")
	if len(errs) > 0 {
		return errors.New(strings.Join(errs, "; "))
	}
	return nil
}
