// pitv-installer: writes the PiTV SD-card image (DietPi + PiTV + pitv_content) on Linux or
// Windows, configures the first boot, and upgrades a running Pi over the network.
//
//	pitv-installer                    guided: config wizard, pick a card, write
//	pitv-installer config [file]      run the wizard only, write pitv-install.json
//	pitv-installer disks              list removable disks
//	pitv-installer write --image pitv.img.xz --disk /dev/sdb --config pitv-install.json [--yes]
//	pitv-installer upgrade --host pitv --user pete [--content-src ../PiTV_content]
package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const version = "0.1.0"

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
		c := DefaultConfig()
		if loaded, err := LoadConfig(file); err == nil {
			c = loaded
		}
		c = Wizard(c)
		if err := c.Validate(); err != nil {
			die(err.Error())
		}
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
		c, err := LoadConfig(*cfgPath)
		must(err)
		if *image == "" || *disk == "" {
			die("--image and --disk are required")
		}
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
		fs.StringVar(&o.NASHost, "nas", "synologynas", "NAS host")
		_ = fs.Parse(os.Args[2:])
		p := newPrompter()
		if o.User == "" {
			o.User = p.ask("Maintenance user", "")
		}
		o.Password = p.askSecret("Password for "+o.User+" (also used for sudo)", false)
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
	p := newPrompter()
	cfgPath := p.ask("Configuration file", "pitv-install.json")
	c := DefaultConfig()
	if loaded, err := LoadConfig(cfgPath); err == nil {
		c = loaded
		if !p.askBool("Use the saved configuration as-is", true) {
			c = Wizard(c)
		}
	} else {
		c = Wizard(c)
	}
	must(c.Validate())
	must(c.Save(cfgPath))
	if c.Mode == "upgrade" {
		o := UpgradeOpts{Host: c.Hostname, User: c.MaintenanceUser.Name, Password: c.MaintenanceUser.Password,
			PiTVSrc: defaultPiTVSrc(), NASHost: c.NAS.Host}
		o.ContentSrc = p.ask("Local PiTV_content checkout (empty = skip)", guessContentSrc())
		must(Upgrade(o, os.Stdout))
		return
	}
	image := p.ask("Image file (.img.xz)", latestImage())
	disks, err := ListDisks()
	must(err)
	if len(disks) == 0 {
		die("no removable disk found; insert the SD card and try again")
	}
	printDisks(disks)
	choice := p.askInt("Write to disk number", 1)
	if choice < 1 || choice > len(disks) {
		die("no such disk")
	}
	must(writeCard(image, disks[choice-1], c, false))
}

func writeCard(image string, d Disk, c Config, yes bool) error {
	if !yes {
		fmt.Printf("\nAbout to ERASE %s (%s, %s) and write %s\n", d.Path, d.Model, humanSize(d.SizeBytes), filepath.Base(image))
		fmt.Printf("mode=%s hostname=%s user=%s display=%s\n", c.Mode, c.Hostname, c.MaintenanceUser.Name, c.DisplayMode)
		if !newPrompter().askBool("Continue", false) {
			return fmt.Errorf("cancelled")
		}
	}
	work, err := os.MkdirTemp("", "pitv-installer-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(work)
	fmt.Println("==> decompressing image")
	img, err := Decompress(image, work, func(done int64) { fmt.Printf("\r    %d MB", done>>20) })
	fmt.Println()
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
	dev, err := OpenDisk(d)
	if err != nil {
		return err
	}
	defer dev.Close()
	start := time.Now()
	err = WriteImage(img, dev, func(done, total int64, phase string) {
		fmt.Printf("\r==> %s %d / %d MB (%.0f%%)   ", phase, done>>20, total>>20, float64(done)*100/float64(total))
	})
	fmt.Println()
	if err != nil {
		return err
	}
	Rescan(d)
	fmt.Printf("==> done in %s. Put the card in the Pi and power on: the first boot takes several minutes\n", time.Since(start).Round(time.Second))
	fmt.Println("    (test card, then packages, then both apps), logs to /work/install/install.log, and reboots into PiTV.")
	return nil
}

// bootFiles is everything dropped on the FAT partition: DietPi's automation and PiTV's config.
func bootFiles(c Config) (map[string][]byte, error) {
	txt, err := DietpiTxt(c)
	if err != nil {
		return nil, err
	}
	cfg, err := jsonBytes(c)
	if err != nil {
		return nil, err
	}
	files := map[string][]byte{"dietpi.txt": []byte(txt), "pitv-install.json": cfg}
	if c.Wifi.Enabled {
		wifi, err := DietpiWifiTxt(c)
		if err != nil {
			return nil, err
		}
		files["dietpi-wifi.txt"] = []byte(wifi)
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
	return Disk{}, fmt.Errorf("%s is not a removable disk (see: disks)", path)
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

func defaultPiTVSrc() string {
	exe, _ := os.Executable()
	for _, cand := range []string{filepath.Join(filepath.Dir(exe), "..", ".."), filepath.Join(filepath.Dir(exe), "..", "..", ".."), "."} {
		if _, err := os.Stat(filepath.Join(cand, "pitv", "cli.py")); err == nil {
			abs, _ := filepath.Abs(cand)
			return abs
		}
	}
	return "."
}

func guessContentSrc() string {
	for _, cand := range []string{filepath.Join(defaultPiTVSrc(), "..", "..", "PiTV_content"), "../PiTV_content"} {
		if _, err := os.Stat(filepath.Join(cand, "setup", "install-on-pi.sh")); err == nil {
			abs, _ := filepath.Abs(cand)
			return abs
		}
	}
	return ""
}

func latestImage() string {
	matches, _ := filepath.Glob(filepath.Join(defaultPiTVSrc(), "dist", "pitv-*.img.xz"))
	if len(matches) == 0 {
		matches, _ = filepath.Glob("pitv-*.img.xz")
	}
	if len(matches) == 0 {
		return ""
	}
	return matches[len(matches)-1]
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
