// Install configuration: the single pitv-install.json that drives DietPi's first-run
// automation and PiTV's own first-boot script.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
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
	Label    string `json:"label"`
	CacheDir string `json:"cache_dir"`
}

type PiTVOpts struct {
	AdminPassword string `json:"admin_password"`
	WebPort       int    `json:"web_port"`
}

type ContentOpts struct {
	YoutubeCookiesFile string         `json:"youtube_cookies_file"`
	Settings           map[string]any `json:"settings"`
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

var hostnameRe = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`)
var userRe = regexp.MustCompile(`^[a-z_][a-z0-9_-]{0,31}$`)

// Everything below ends up in dietpi.txt, a sed expression or a shell variable on the Pi, so
// the character sets are deliberately narrow.
var nasHostRe = regexp.MustCompile(`^[A-Za-z0-9]([A-Za-z0-9.-]{0,252}[A-Za-z0-9])?$`)
var shareRe = regexp.MustCompile(`^[A-Za-z0-9._%+-]+$`)
var tokenRe = regexp.MustCompile(`^[A-Za-z0-9_./+-]+$`) // locale, keyboard layout, timezone
var addrRe = regexp.MustCompile(`^[0-9A-Fa-f.:/]*$`)    // static IP with prefix, gateway, DNS
var deviceRe = regexp.MustCompile(`^(auto|/dev/[A-Za-z0-9/_-]+)$`)
var pathRe = regexp.MustCompile(`^/[A-Za-z0-9._/-]*$`)

func hasLineBreak(s string) bool { return strings.ContainsAny(s, "\r\n\x00") }

func DefaultConfig() Config {
	return Config{
		Schema: 1, Mode: "normal", Hostname: "pitv", Timezone: "Europe/London", Locale: "en_GB.UTF-8", Keyboard: "gb",
		Wifi: Wifi{Country: "GB"}, Network: Network{Ethernet: true},
		NAS: NAS{Host: "synologynas", Username: "pitv", Shares: []string{"tvshows", "movies", "ads", "tvsports", "music%20videos"}},
		DisplayMode: "hdmi576", SystemPartitionGB: 8,
		WorkDrive: WorkDrive{Device: "auto", Label: "PITVWORK", CacheDir: "/mnt/cache/pitv"},
		PiTV:      PiTVOpts{WebPort: 80},
	}
}

func LoadConfig(path string) (Config, error) {
	c := DefaultConfig()
	data, err := os.ReadFile(path)
	if err != nil {
		return c, err
	}
	if err := json.Unmarshal(data, &c); err != nil {
		return c, fmt.Errorf("%s: %w", path, err)
	}
	return c, c.Validate()
}

func (c Config) Save(path string) error {
	data, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(data, '\n'), 0o600)
}

func (c Config) Validate() error {
	var errs []string
	switch c.Mode {
	case "clean", "normal", "upgrade":
	default:
		errs = append(errs, "mode must be clean, normal or upgrade")
	}
	if !hostnameRe.MatchString(c.Hostname) {
		errs = append(errs, "hostname must be lower-case letters, digits and hyphens")
	}
	if c.MaintenanceUser.Name == "" || !userRe.MatchString(c.MaintenanceUser.Name) {
		errs = append(errs, "maintenance user name is required (lower-case, no spaces)")
	}
	if c.MaintenanceUser.Password == "" && len(c.MaintenanceUser.SSHAuthorizedKeys) == 0 {
		errs = append(errs, "maintenance user needs a password or an SSH key")
	}
	if c.Wifi.Enabled && (c.Wifi.SSID == "" || len(c.Wifi.Country) != 2) {
		errs = append(errs, "wifi needs an SSID and a two-letter country code")
	}
	if !c.Wifi.Enabled && !c.Network.Ethernet {
		errs = append(errs, "enable ethernet or wifi")
	}
	if c.NAS.Host == "" || c.NAS.Username == "" {
		errs = append(errs, "NAS host and username are required")
	}
	if !nasHostRe.MatchString(c.NAS.Host) {
		errs = append(errs, "NAS host must be a host name or IP address")
	}
	if !userRe.MatchString(strings.ToLower(c.NAS.Username)) {
		errs = append(errs, "NAS username must be letters, digits, '_' and '-'")
	}
	for _, s := range c.NAS.Shares {
		if !shareRe.MatchString(s) {
			errs = append(errs, "share names may only contain letters, digits, '.', '_', '-' and %20")
			break
		}
	}
	for _, v := range []string{c.Timezone, c.Locale, c.Keyboard} {
		if !tokenRe.MatchString(v) {
			errs = append(errs, "timezone, locale and keyboard must be plain identifiers")
			break
		}
	}
	for _, v := range []string{c.Network.StaticIP, c.Network.Gateway, c.Network.DNS} {
		if !addrRe.MatchString(v) {
			errs = append(errs, "static IP, gateway and DNS must be addresses")
			break
		}
	}
	if !deviceRe.MatchString(c.WorkDrive.Device) {
		errs = append(errs, "work drive device must be 'auto' or a /dev path")
	}
	if !pathRe.MatchString(c.WorkDrive.CacheDir) {
		errs = append(errs, "cache_dir must be an absolute path")
	}
	if c.PiTVContent.YoutubeCookiesFile != "" && !pathRe.MatchString(c.PiTVContent.YoutubeCookiesFile) {
		errs = append(errs, "youtube_cookies_file must be an absolute path on the Pi")
	}
	for _, v := range []string{c.MaintenanceUser.Password, c.NAS.Password, c.Wifi.PSK, c.Wifi.SSID, c.PiTV.AdminPassword} {
		if hasLineBreak(v) {
			errs = append(errs, "passwords and the SSID must not contain line breaks")
			break
		}
	}
	for _, k := range c.MaintenanceUser.SSHAuthorizedKeys {
		if hasLineBreak(k) || !strings.HasPrefix(k, "ssh-") && !strings.HasPrefix(k, "ecdsa-") && !strings.HasPrefix(k, "sk-") {
			errs = append(errs, "ssh_authorized_keys entries must be single-line public keys")
			break
		}
	}
	switch c.DisplayMode {
	case "hdmi576", "composite", "hdmi43", "hdmi":
	default:
		errs = append(errs, "display_mode must be hdmi576, composite, hdmi43 or hdmi")
	}
	if c.SystemPartitionGB < 6 || c.SystemPartitionGB > 64 {
		errs = append(errs, "system_partition_gb must be between 6 and 64")
	}
	if len(errs) > 0 {
		return errors.New(strings.Join(errs, "; "))
	}
	return nil
}
