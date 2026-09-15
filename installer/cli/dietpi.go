// Render DietPi's first-boot automation files from the install configuration.
package main

import (
	"embed"
	"strings"
)

//go:embed templates/*.template
var templates embed.FS

func b2i(b bool) string {
	if b {
		return "1"
	}
	return "0"
}

// DietpiTxt fills dietpi.txt.template. The global password doubles as root's and the
// "dietpi" user's password; the maintenance user is created by the first-boot script.
func DietpiTxt(c Config) (string, error) {
	raw, err := templates.ReadFile("templates/dietpi.txt.template")
	if err != nil {
		return "", err
	}
	static := c.Network.StaticIP != ""
	pubkey := ""
	if len(c.MaintenanceUser.SSHAuthorizedKeys) > 0 {
		pubkey = c.MaintenanceUser.SSHAuthorizedKeys[0]
	}
	password := c.MaintenanceUser.Password
	if password == "" {
		password = "dietpi"
	}
	r := strings.NewReplacer(
		"@LOCALE@", c.Locale, "@KEYBOARD@", c.Keyboard, "@TIMEZONE@", c.Timezone,
		"@ETHERNET@", b2i(c.Network.Ethernet), "@WIFI@", b2i(c.Wifi.Enabled), "@WIFI_COUNTRY@", c.Wifi.Country,
		"@HOSTNAME@", c.Hostname, "@USESTATIC@", b2i(static), "@STATIC_IP@", c.Network.StaticIP,
		"@GATEWAY@", c.Network.Gateway, "@DNS@", c.Network.DNS, "@GLOBAL_PASSWORD@", password,
		"@SSH_PUBKEY@", pubkey,
	)
	return r.Replace(string(raw)), nil
}

func DietpiWifiTxt(c Config) (string, error) {
	raw, err := templates.ReadFile("templates/dietpi-wifi.txt.template")
	if err != nil {
		return "", err
	}
	esc := func(s string) string { return strings.ReplaceAll(s, "'", `'\''`) }
	return strings.NewReplacer("@SSID@", esc(c.Wifi.SSID), "@PSK@", esc(c.Wifi.PSK)).Replace(string(raw)), nil
}
