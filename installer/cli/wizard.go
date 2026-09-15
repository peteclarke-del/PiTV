// Interactive prompts that produce pitv-install.json. Every question has a default from
// the previous answer or the built-in defaults, so re-running is quick.
package main

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"

	"golang.org/x/term"
)

type prompter struct {
	in  *bufio.Reader
	out io.Writer
}

// console is the one reader of standard input. A second bufio.Reader on the same stream would
// swallow lines the first one had already buffered.
var console = &prompter{in: bufio.NewReader(os.Stdin), out: os.Stdout}

// line reads one answer without its line ending. End of input stops the program: every
// caller would otherwise loop on the default, or take it for a question like "Continue".
func (p *prompter) line() string {
	s, err := p.in.ReadString('\n')
	if errors.Is(err, io.EOF) && s == "" {
		die("no more input")
	}
	return strings.TrimRight(s, "\r\n")
}

func (p *prompter) ask(label, def string) string {
	if def != "" {
		fmt.Fprintf(p.out, "%s [%s]: ", label, def)
	} else {
		fmt.Fprintf(p.out, "%s: ", label)
	}
	if v := strings.TrimSpace(p.line()); v != "" {
		return v
	}
	return def
}

func (p *prompter) askBool(label string, def bool) bool {
	d := "n"
	if def {
		d = "y"
	}
	for {
		switch strings.ToLower(p.ask(label+" (y/n)", d)) {
		case "y", "yes":
			return true
		case "n", "no":
			return false
		}
	}
}

func (p *prompter) askInt(label string, def int) int {
	for {
		if n, err := strconv.Atoi(p.ask(label, strconv.Itoa(def))); err == nil {
			return n
		}
		fmt.Fprintln(p.out, "  please enter a number")
	}
}

// askSecret reads without echo on a terminal. The answer is used exactly as typed: leading
// or trailing spaces are legal in a Wi-Fi key or a password.
func (p *prompter) askSecret(label string, keep bool) string {
	suffix := ""
	if keep {
		suffix = " (leave empty to keep the current one)"
	}
	fmt.Fprintf(p.out, "%s%s: ", label, suffix)
	if fd := int(os.Stdin.Fd()); term.IsTerminal(fd) {
		b, err := term.ReadPassword(fd)
		fmt.Fprintln(p.out)
		if err != nil {
			die("reading the answer: " + err.Error())
		}
		return string(b)
	}
	return p.line()
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

// secret keeps the current value when the answer is empty.
func (p *prompter) secret(label string, current *string) {
	if v := p.askSecret(label, *current != ""); v != "" {
		*current = v
	}
}

// Wizard walks through the configuration, starting from c (defaults or a loaded file).
func Wizard(c Config) Config {
	p := console
	fmt.Println("\nPiTV install configuration (Enter keeps the value in brackets)")
	c.Mode = p.choose("Install mode", []string{"normal", "clean", "upgrade"}, c.Mode)
	switch c.Mode {
	case "clean":
		fmt.Println("  clean: the SD card AND the USB work drive are wiped")
	case "normal":
		fmt.Println("  normal: the SD card is wiped; the USB work drive keeps its content")
	}
	c.Hostname = p.ask("Hostname", c.Hostname)
	c.Timezone = p.ask("Timezone", c.Timezone)
	c.Locale = p.ask("Locale", c.Locale)
	c.Keyboard = p.ask("Keyboard layout", c.Keyboard)

	fmt.Println("\nMaintenance user (SSH login with sudo)")
	c.MaintenanceUser.Name = p.ask("Username", c.MaintenanceUser.Name)
	p.secret("Password", &c.MaintenanceUser.Password)
	if keyFile := p.ask("SSH public key file (optional, e.g. ~/.ssh/id_ed25519.pub)", ""); keyFile != "" {
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
		p.secret("Wi-Fi password", &c.Wifi.PSK)
	}
	c.Network.StaticIP = p.ask("Static IP with prefix (empty = DHCP)", c.Network.StaticIP)
	if c.Network.StaticIP != "" {
		c.Network.Gateway = p.ask("Gateway", c.Network.Gateway)
		c.Network.DNS = p.ask("DNS server", c.Network.DNS)
	}

	fmt.Println("\nNAS (read-only media shares)")
	c.NAS.Host = p.ask("NAS host", c.NAS.Host)
	c.NAS.Username = p.ask("NAS username", c.NAS.Username)
	p.secret("NAS password (blank for none)", &c.NAS.Password)
	c.NAS.Shares = strings.Fields(p.ask("Shares (space separated; use %20 for a space)", strings.Join(c.NAS.Shares, " ")))

	fmt.Println("\nTelevision and storage")
	c.DisplayMode = p.choose("Display", []string{"hdmi576", "composite", "hdmi43", "hdmi"}, c.DisplayMode)
	c.SystemPartitionGB = p.askInt("System partition size (GB); the rest of the card becomes the work partition", c.SystemPartitionGB)
	c.WorkDrive.Device = p.ask("USB work drive device (auto = first USB disk)", c.WorkDrive.Device)

	fmt.Println("\nPiTV")
	p.secret("PiTV admin password (empty = set on first visit)", &c.PiTV.AdminPassword)
	c.PiTVContent.YoutubeCookiesFile = p.ask("YouTube cookies file for pitv_content on the Pi (optional)", c.PiTVContent.YoutubeCookiesFile)
	return c
}

// wizardUntilValid repeats the wizard, with the answers so far as defaults, until the
// configuration validates, so one mistake does not throw away every answer.
func wizardUntilValid(c Config) Config {
	for {
		c = Wizard(c)
		err := c.Validate()
		if err == nil {
			return c
		}
		fmt.Println("\nPlease correct:", err)
	}
}

func expandHome(p string) string {
	if strings.HasPrefix(p, "~/") {
		if h, err := os.UserHomeDir(); err == nil {
			return h + p[1:]
		}
	}
	return p
}
