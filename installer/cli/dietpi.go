// Render DietPi's first-boot automation files from the install configuration.
package main

import (
	"crypto/rand"
	"embed"
	"encoding/base64"
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

// randomPassword is the DietPi global password. DietPi sets it on root and the dietpi user and
// keeps a reversibly encrypted copy on the card, so it must not be a password anyone uses:
// the maintenance user, created by the first-boot script with its own password and keys, is
// the only account meant for logging in.
func randomPassword() (string, error) {
	b := make([]byte, 24)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

// DietpiTxt fills dietpi.txt.template.
func DietpiTxt(c Config) (string, error) {
	raw, err := templates.ReadFile("templates/dietpi.txt.template")
	if err != nil {
		return "", err
	}
	global, err := randomPassword()
	if err != nil {
		return "", err
	}
	r := strings.NewReplacer(
		"@LOCALE@", c.Locale, "@KEYBOARD@", c.Keyboard, "@TIMEZONE@", c.Timezone,
		"@ETHERNET@", b2i(c.Network.Ethernet), "@WIFI@", b2i(c.Wifi.Enabled), "@WIFI_COUNTRY@", c.Wifi.Country,
		"@HOSTNAME@", c.Hostname, "@USESTATIC@", b2i(c.Network.StaticIP != ""), "@STATIC_IP@", c.Network.StaticIP,
		"@GATEWAY@", c.Network.Gateway, "@DNS@", c.Network.DNS, "@GLOBAL_PASSWORD@", global,
	)
	return r.Replace(string(raw)), nil
}

// mergeDietpiTxt sets each KEY=value line of update in base, DietPi's own dietpi.txt, and
// appends the keys base lacks. DietPi's file documents and defaults many more settings than
// PiTV sets, so replacing it wholesale would drop them.
func mergeDietpiTxt(base []byte, update string) []byte {
	values := map[string]string{}
	var order []string
	for _, line := range strings.Split(update, "\n") {
		key, _, ok := strings.Cut(line, "=")
		if !ok || strings.HasPrefix(key, "#") {
			continue
		}
		if _, dup := values[key]; !dup {
			order = append(order, key)
		}
		values[key] = line
	}
	var out []string
	done := map[string]bool{}
	for _, line := range strings.Split(strings.TrimRight(string(base), "\n"), "\n") {
		key, _, ok := strings.Cut(strings.TrimLeft(line, " \t"), "=")
		if repl, mine := values[key]; ok && mine {
			if done[key] {
				continue // DietPi reads the first occurrence; drop the rest
			}
			line, done[key] = repl, true
		}
		out = append(out, strings.TrimRight(line, "\r"))
	}
	if len(base) == 0 {
		out = nil
	}
	for _, key := range order {
		if !done[key] {
			out = append(out, values[key])
		}
	}
	return []byte(strings.Join(out, "\n") + "\n")
}

// DietpiWifiTxt fills dietpi-wifi.txt.template. DietPi sources the file as shell, so the
// values are single-quoted.
func DietpiWifiTxt(c Config) (string, error) {
	raw, err := templates.ReadFile("templates/dietpi-wifi.txt.template")
	if err != nil {
		return "", err
	}
	return strings.NewReplacer("@SSID@", shellQuote(c.Wifi.SSID), "@PSK@", shellQuote(c.Wifi.PSK)).Replace(string(raw)), nil
}
