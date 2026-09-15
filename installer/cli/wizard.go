// Interactive prompts that produce pitv-install.json. Every question has a default from
// the previous answer or the built-in defaults, so re-running is quick.
package main

import (
	"bufio"
	"fmt"
	"os"
	"strconv"
	"strings"

	"golang.org/x/term"
)

type prompter struct {
	in  *bufio.Reader
	out *os.File
}

func newPrompter() *prompter { return &prompter{in: bufio.NewReader(os.Stdin), out: os.Stdout} }

func (p *prompter) ask(label, def string) string {
	if def != "" {
		fmt.Fprintf(p.out, "%s [%s]: ", label, def)
	} else {
		fmt.Fprintf(p.out, "%s: ", label)
	}
	line, _ := p.in.ReadString('\n')
	line = strings.TrimSpace(line)
	if line == "" {
		return def
	}
	return line
}

func (p *prompter) askBool(label string, def bool) bool {
	d := "n"
	if def {
		d = "y"
	}
	for {
		v := strings.ToLower(p.ask(label+" (y/n)", d))
		if v == "y" || v == "yes" {
			return true
		}
		if v == "n" || v == "no" {
			return false
		}
	}
}

func (p *prompter) askInt(label string, def int) int {
	for {
		v := p.ask(label, strconv.Itoa(def))
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
		fmt.Fprintln(p.out, "  please enter a number")
	}
}

func (p *prompter) askSecret(label string, keep bool) string {
	suffix := ""
	if keep {
		suffix = " (leave empty to keep the current one)"
	}
	fmt.Fprintf(p.out, "%s%s: ", label, suffix)
	if term.IsTerminal(int(os.Stdin.Fd())) {
		b, err := term.ReadPassword(int(os.Stdin.Fd()))
		fmt.Fprintln(p.out)
		if err == nil {
			return strings.TrimSpace(string(b))
		}
	}
	line, _ := p.in.ReadString('\n')
	return strings.TrimSpace(line)
}

func (p *prompter) choose(label string, options []string, def string) string {
	fmt.Fprintf(p.out, "%s\n", label)
	for i, o := range options {
		mark := " "
		if o == def {
			mark = "*"
		}
		fmt.Fprintf(p.out, "  %s %d) %s\n", mark, i+1, o)
	}
	for {
		v := p.ask("choice", def)
		if n, err := strconv.Atoi(v); err == nil && n >= 1 && n <= len(options) {
			return options[n-1]
		}
		for _, o := range options {
			if o == v {
				return o
			}
		}
	}
}

// Wizard walks through the configuration, starting from `c` (defaults or a loaded file).
func Wizard(c Config) Config {
	p := newPrompter()
	fmt.Println("\nPiTV install configuration (Enter keeps the value in brackets)")
	c.Mode = p.choose("Install mode", []string{"normal", "clean", "upgrade"}, c.Mode)
	if c.Mode == "clean" {
		fmt.Println("  clean: the SD card AND the USB work drive are wiped")
	} else if c.Mode == "normal" {
		fmt.Println("  normal: the SD card is wiped; the USB work drive keeps its content")
	}
	c.Hostname = p.ask("Hostname", c.Hostname)
	c.Timezone = p.ask("Timezone", c.Timezone)
	c.Locale = p.ask("Locale", c.Locale)
	c.Keyboard = p.ask("Keyboard layout", c.Keyboard)

	fmt.Println("\nMaintenance user (SSH login with sudo)")
	c.MaintenanceUser.Name = p.ask("Username", c.MaintenanceUser.Name)
	if pw := p.askSecret("Password", c.MaintenanceUser.Password != ""); pw != "" {
		c.MaintenanceUser.Password = pw
	}
	keyFile := p.ask("SSH public key file (optional, e.g. ~/.ssh/id_ed25519.pub)", "")
	if keyFile != "" {
		if data, err := os.ReadFile(expandHome(keyFile)); err == nil {
			c.MaintenanceUser.SSHAuthorizedKeys = []string{strings.TrimSpace(string(data))}
		} else {
			fmt.Println("  could not read key file:", err)
		}
	}

	fmt.Println("\nNetwork")
	c.Network.Ethernet = p.askBool("Use wired ethernet", c.Network.Ethernet)
	c.Wifi.Enabled = p.askBool("Configure Wi-Fi", c.Wifi.Enabled)
	if c.Wifi.Enabled {
		c.Wifi.Country = strings.ToUpper(p.ask("Wi-Fi country code", c.Wifi.Country))
		c.Wifi.SSID = p.ask("Wi-Fi SSID", c.Wifi.SSID)
		if pw := p.askSecret("Wi-Fi password", c.Wifi.PSK != ""); pw != "" {
			c.Wifi.PSK = pw
		}
	}
	c.Network.StaticIP = p.ask("Static IP with prefix (empty = DHCP)", c.Network.StaticIP)
	if c.Network.StaticIP != "" {
		c.Network.Gateway = p.ask("Gateway", c.Network.Gateway)
		c.Network.DNS = p.ask("DNS server", c.Network.DNS)
	}

	fmt.Println("\nNAS (read-only media shares)")
	c.NAS.Host = p.ask("NAS host", c.NAS.Host)
	c.NAS.Username = p.ask("NAS username", c.NAS.Username)
	if pw := p.askSecret("NAS password", c.NAS.Password != ""); pw != "" {
		c.NAS.Password = pw
	}
	c.NAS.Shares = strings.Fields(p.ask("Shares (space separated; use %20 for a space)", strings.Join(c.NAS.Shares, " ")))

	fmt.Println("\nTelevision and storage")
	c.DisplayMode = p.choose("Display", []string{"hdmi576", "composite", "hdmi43", "hdmi"}, c.DisplayMode)
	c.SystemPartitionGB = p.askInt("System partition size (GB); the rest of the card becomes the work partition", c.SystemPartitionGB)
	c.WorkDrive.Device = p.ask("USB work drive device (auto = first USB disk)", c.WorkDrive.Device)

	fmt.Println("\nPiTV")
	if pw := p.askSecret("PiTV admin password (empty = set on first visit)", c.PiTV.AdminPassword != ""); pw != "" {
		c.PiTV.AdminPassword = pw
	}
	c.PiTVContent.YoutubeCookiesFile = p.ask("YouTube cookies file for pitv_content on the Pi (optional)", c.PiTVContent.YoutubeCookiesFile)
	return c
}

func expandHome(p string) string {
	if strings.HasPrefix(p, "~/") {
		if h, err := os.UserHomeDir(); err == nil {
			return h + p[1:]
		}
	}
	return p
}
