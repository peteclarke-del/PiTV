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
