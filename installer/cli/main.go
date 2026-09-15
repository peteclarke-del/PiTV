// pitv-installer: writes the PiTV SD-card image (DietPi + PiTV + pitv_content) on Linux or
// Windows, configures the first boot, and upgrades a running Pi over the network.
//
//	pitv-installer                    guided: config wizard, pick a card, write
//	pitv-installer config [file]      run the wizard only, write pitv-install.json
//	pitv-installer disks              list the disks a card can be written to
//	pitv-installer write --image pitv.img.xz --disk /dev/sdb --config pitv-install.json [--yes]
//	pitv-installer upgrade --host pitv --user pete [--content-src ../PiTV_content]
package main

import (
	"errors"
	"flag"
	"fmt"
	"io/fs"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

// version is set by installer/build-cli.sh (-ldflags "-X main.version=...").
var version = "dev"

func main() {
	if len(os.Args) < 2 {
		guided()
		return
	}
	switch os.Args[1] {
	case "config":
		file := "pitv-install.json"
		if len(os.Args) > 2 {
			file = os.Args[2]
		}
		c, _ := loadForEditing(file)
		c = wizardUntilValid(c)
		must(c.Save(file))
		fmt.Println("saved", file)
	case "disks":
		disks, err := ListDisks()
		must(err)
		printDisks(disks)
	case "write":
		fs := flag.NewFlagSet("write", flag.ExitOnError)
		image := fs.String("image", "", "pitv-<version>.img.xz")
		disk := fs.String("disk", "", "device path (see: disks)")
		cfgPath := fs.String("config", "pitv-install.json", "install configuration")
		yes := fs.Bool("yes", false, "do not ask for confirmation")
		_ = fs.Parse(os.Args[2:])
		if *image == "" || *disk == "" {
			die("--image and --disk are required")
		}
		c, err := LoadConfig(*cfgPath)
		must(err)
		d, err := findDisk(*disk)
		must(err)
		must(writeCard(*image, d, c, *yes))
	case "upgrade":
		fs := flag.NewFlagSet("upgrade", flag.ExitOnError)
		o := UpgradeOpts{}
		fs.StringVar(&o.Host, "host", "pitv", "Pi hostname or IP")
		fs.StringVar(&o.User, "user", "", "maintenance user")
		fs.StringVar(&o.KeyFile, "key", "", "SSH private key (optional)")
		fs.StringVar(&o.PiTVSrc, "pitv-src", defaultPiTVSrc(), "local PiTV checkout")
		fs.StringVar(&o.ContentSrc, "content-src", "", "local PiTV_content checkout (optional)")
		fs.StringVar(&o.NASHost, "nas", "", "NAS host (default: keep the one the Pi was installed with)")
		_ = fs.Parse(os.Args[2:])
		if o.NASHost != "" && !nasHostRe.MatchString(o.NASHost) {
			die("--nas must be a host name or IP address")
		}
		if o.User == "" {
			o.User = console.ask("Maintenance user", "")
		}
		o.Password = console.askSecret("Password for "+o.User+" (sudo on the Pi asks for it)", false)
		must(Upgrade(o, os.Stdout))
	case "version", "--version", "-v":
		fmt.Println("pitv-installer", version)
	default:
		fmt.Println("usage: pitv-installer [config|disks|write|upgrade|version]")
		os.Exit(2)
	}
}

func guided() {
	fmt.Printf("PiTV installer %s\n", version)
	cfgPath := console.ask("Configuration file", "pitv-install.json")
	c, saved := loadForEditing(cfgPath)
	if saved {
		if err := c.Validate(); err != nil {
			fmt.Println("The saved configuration needs attention:", err)
			saved = false
		}
	}
	if !saved || !console.askBool("Use the saved configuration as-is", true) {
		c = wizardUntilValid(c)
	}
	must(c.Save(cfgPath))
	if c.Mode == "upgrade" {
		o := UpgradeOpts{Host: c.Hostname, User: c.MaintenanceUser.Name, Password: c.MaintenanceUser.Password,
			PiTVSrc: defaultPiTVSrc(), NASHost: c.NAS.Host}
		o.ContentSrc = console.ask("Local PiTV_content checkout (empty = skip)", guessContentSrc())
		must(Upgrade(o, os.Stdout))
		return
	}
	image := console.ask("Image file (.img.xz)", latestImage())
	disks, err := ListDisks()
	must(err)
	if len(disks) == 0 {
		die("no removable disk found; insert the SD card and try again")
	}
	printDisks(disks)
	choice := console.askInt("Write to disk number", 1)
	if choice < 1 || choice > len(disks) {
		die("no such disk")
	}
	must(writeCard(image, disks[choice-1], c, false))
}

// loadForEditing returns the saved configuration, or the defaults when there is none. A file
// that exists but does not parse stops the program: starting from the defaults would
// overwrite it on save.
func loadForEditing(path string) (Config, bool) {
	c, err := readConfig(path)
	if errors.Is(err, fs.ErrNotExist) {
		return c, false
	}
	must(err)
	return c, true
}

func writeCard(image string, d Disk, c Config, yes bool) error {
	if c.Mode == "upgrade" {
		return errors.New("mode upgrade updates a running Pi over SSH (pitv-installer upgrade); choose normal or clean to write a card")
	}
	if !IsAdmin() {
		return fmt.Errorf("writing to %s needs administrator rights: %s", d.Path, adminHint)
	}
	if d.SizeBytes < minCardBytes(c.SystemPartitionGB) {
		return fmt.Errorf("%s (%s) is too small for a %d GB system partition and a work partition", d.Path, humanSize(d.SizeBytes), c.SystemPartitionGB)
	}
	wantSHA, err := expectedSHA256(image)
	if err != nil {
		return err
	}
	if !yes {
		fmt.Printf("\nAbout to ERASE %s (%s, %s) and write %s\n", d.Path, d.Model, humanSize(d.SizeBytes), filepath.Base(image))
		fmt.Printf("mode=%s hostname=%s user=%s display=%s\n", c.Mode, c.Hostname, c.MaintenanceUser.Name, c.DisplayMode)
		if !console.askBool("Continue", false) {
			return errors.New("cancelled")
		}
	}
	if wantSHA == "" {
		fmt.Printf("    note: no %s.sha256 beside the image, so the download is not checked\n", filepath.Base(image))
	}
	work, err := os.MkdirTemp("", "pitv-installer-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(work)
	defer removeOnInterrupt(work)()

	fmt.Println("==> decompressing image")
	img, err := Decompress(image, work, wantSHA, d.SizeBytes, func(done int64) { fmt.Printf("\r    %d MB", done>>20) })
	fmt.Println()
	if errors.Is(err, errTooLarge) {
		return fmt.Errorf("%w (%s)", err, humanSize(d.SizeBytes))
	}
	if err != nil {
		return err
	}
	fmt.Println("==> writing configuration into the boot partition")
	files, err := bootFiles(c)
	if err != nil {
		return err
	}
	if err := PatchBoot(img, files); err != nil {
		return err
	}
	if err := AddWorkPartition(img, d.SizeBytes, c.SystemPartitionGB); err != nil {
		return err
	}
	dev, err := OpenDisk(d)
	if err != nil {
		return err
	}
	start := time.Now()
	err = WriteImage(img, dev, func(done, total int64, phase string) {
		fmt.Printf("\r==> %s %d / %d MB (%.0f%%)   ", phase, done>>20, total>>20, float64(done)*100/float64(total))
	})
	fmt.Println()
	// Closed before the rescan: on Windows closing releases the volume locks.
	if cerr := dev.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		return err
	}
	Rescan(d)
	fmt.Printf("==> done in %s. Put the card in the Pi and power on: the first boot takes several minutes\n", time.Since(start).Round(time.Second))
	fmt.Println("    (test card, then packages, then both apps), logs to /work/install/install.log, and reboots into PiTV.")
	return nil
}

// removeOnInterrupt deletes dir if the program is interrupted, since dir holds a copy of the
// image with every password of the install in its boot partition. Call the result to stop.
func removeOnInterrupt(dir string) (stop func()) {
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	done := make(chan struct{})
	go func() {
		select {
		case <-sig:
			os.RemoveAll(dir)
			fmt.Fprintln(os.Stderr, "\ninterrupted: the card is incomplete and must be written again")
			os.Exit(130)
		case <-done:
		}
	}()
	return func() { signal.Stop(sig); close(done) }
}

// bootFiles is everything written to the FAT partition: DietPi's automation, merged into the
// image's own dietpi.txt, and PiTV's configuration. DietPi's first boot imports dietpi.txt and
// dietpi-wifi.txt from there into /boot and deletes them; pitv-firstboot.sh deletes the rest.
func bootFiles(c Config) (map[string]bootEdit, error) {
	txt, err := DietpiTxt(c)
	if err != nil {
		return nil, err
	}
	cfg, err := c.JSON()
	if err != nil {
		return nil, err
	}
	files := map[string]bootEdit{
		"dietpi.txt":        func(old []byte) []byte { return mergeDietpiTxt(old, txt) },
		"pitv-install.json": replaceWith(cfg),
	}
	if c.Wifi.Enabled {
		wifi, err := DietpiWifiTxt(c)
		if err != nil {
			return nil, err
		}
		files["dietpi-wifi.txt"] = replaceWith([]byte(wifi))
	}
	return files, nil
}

func findDisk(path string) (Disk, error) {
	disks, err := ListDisks()
	if err != nil {
		return Disk{}, err
	}
	for _, d := range disks {
		if strings.EqualFold(d.Path, path) {
			return d, nil
		}
	}
	return Disk{}, fmt.Errorf("%s is not a removable disk free of system mounts (see: disks)", path)
}

func printDisks(disks []Disk) {
	if len(disks) == 0 {
		fmt.Println("no removable disks found")
		return
	}
	for i, d := range disks {
		mounts := ""
		if len(d.Mounts) > 0 {
			mounts = "  mounted: " + strings.Join(d.Mounts, ", ")
		}
		fmt.Printf("  %d) %-22s %-10s %s%s\n", i+1, d.Path, humanSize(d.SizeBytes), d.Model, mounts)
	}
}

// defaultPiTVSrc is the PiTV checkout the installer runs from (its dist/ or installer/cli/),
// else the current directory when that is a checkout; "" when none is found.
func defaultPiTVSrc() string {
	var cands []string
	if exe, err := os.Executable(); err == nil {
		dir := filepath.Dir(exe)
		cands = append(cands, filepath.Join(dir, ".."), filepath.Join(dir, "..", ".."))
	}
	for _, cand := range append(cands, ".") {
		if _, err := os.Stat(filepath.Join(cand, "pitv", "cli.py")); err == nil {
			if abs, err := filepath.Abs(cand); err == nil {
				return abs
			}
		}
	}
	return ""
}

func guessContentSrc() string {
	for _, cand := range []string{filepath.Join(defaultPiTVSrc(), "..", "..", "PiTV_content"), filepath.Join("..", "PiTV_content")} {
		if _, err := os.Stat(filepath.Join(cand, "setup", "install-on-pi.sh")); err == nil {
			if abs, err := filepath.Abs(cand); err == nil {
				return abs
			}
		}
	}
	return ""
}

// latestImage is the most recently built image in dist/ or the current directory.
func latestImage() string {
	built, _ := filepath.Glob(filepath.Join(defaultPiTVSrc(), "dist", "pitv-*.img.xz"))
	local, _ := filepath.Glob("pitv-*.img.xz")
	newest, newestTime := "", time.Time{}
	for _, m := range append(built, local...) {
		if st, err := os.Stat(m); err == nil && st.ModTime().After(newestTime) {
			newest, newestTime = m, st.ModTime()
		}
	}
	return newest
}

func must(err error) {
	if err != nil {
		die(err.Error())
	}
}

func die(msg string) {
	fmt.Fprintln(os.Stderr, "error:", msg)
	os.Exit(1)
}
