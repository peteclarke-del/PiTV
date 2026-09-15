// Network upgrade: sync the source trees to the Pi over SSH and re-run both install scripts.
// The card stays in the Pi; the work partition and the USB drive are untouched.
package main

import (
	"archive/tar"
	"compress/gzip"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"golang.org/x/crypto/ssh"
)

type UpgradeOpts struct {
	Host       string
	User       string
	Password   string
	KeyFile    string
	PiTVSrc    string // local checkout of PiTV
	ContentSrc string // local checkout of PiTV_content (optional)
	NASHost    string // empty keeps the NAS host the Pi was installed with
}

const installLog = "/work/install/install.log"

// Never uploaded: version control, virtual environments, build output and local state.
var skipDirs = map[string]bool{".git": true, ".venv": true, ".dev": true, "node_modules": true, "dist": true,
	"__pycache__": true, ".pytest_cache": true, ".ruff_cache": true}

// skipFile leaves out the install configuration, which holds every password of the install
// and would otherwise land in the world-readable /opt trees on the Pi, and disk images.
func skipFile(name string) bool {
	return name == "pitv-install.json" || strings.HasSuffix(name, ".img") || strings.HasSuffix(name, ".img.xz")
}

// tarTree writes root as a gzipped tar. Symlinks are kept as links; sockets, FIFOs and
// devices are left out.
func tarTree(w io.Writer, root string) error {
	gz := gzip.NewWriter(w)
	tw := tar.NewWriter(gz)
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(root, p)
		if err != nil || rel == "." {
			return err
		}
		if d.IsDir() && skipDirs[d.Name()] {
			return filepath.SkipDir
		}
		if skipFile(d.Name()) {
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		link := ""
		switch mode := info.Mode(); {
		case mode&fs.ModeSymlink != 0:
			if link, err = os.Readlink(p); err != nil {
				return err
			}
		case !mode.IsRegular() && !mode.IsDir():
			return nil
		}
		hdr, err := tar.FileInfoHeader(info, link)
		if err != nil {
			return err
		}
		hdr.Name = filepath.ToSlash(rel)
		if err := tw.WriteHeader(hdr); err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return nil
		}
		f, err := os.Open(p)
		if err != nil {
			return err
		}
		_, err = io.Copy(tw, f)
		f.Close()
		return err
	})
	if err != nil {
		return err
	}
	if err := tw.Close(); err != nil {
		return err
	}
	return gz.Close()
}

func sshClient(o UpgradeOpts) (*ssh.Client, error) {
	var auth []ssh.AuthMethod
	if o.KeyFile != "" {
		key, err := os.ReadFile(expandHome(o.KeyFile))
		if err != nil {
			return nil, err
		}
		signer, err := ssh.ParsePrivateKey(key)
		var missing *ssh.PassphraseMissingError
		if errors.As(err, &missing) {
			signer, err = ssh.ParsePrivateKeyWithPassphrase(key, []byte(console.askSecret("Passphrase for "+o.KeyFile, false)))
		}
		if err != nil {
			return nil, fmt.Errorf("%s: %w", o.KeyFile, err)
		}
		auth = append(auth, ssh.PublicKeys(signer))
	}
	if o.Password != "" {
		auth = append(auth, ssh.Password(o.Password))
	}
	khPath, err := knownHostsPath()
	if err != nil {
		return nil, err
	}
	known, err := openKnownHosts(khPath, func(host, fingerprint string) bool {
		fmt.Printf("The authenticity of host %s cannot be established.\n  %s\n", host, fingerprint)
		return console.askBool("Trust this host and remember its key", false)
	})
	if err != nil {
		return nil, err
	}
	addr := o.Host
	if _, _, err := net.SplitHostPort(addr); err != nil {
		addr = net.JoinHostPort(addr, "22")
	}
	cfg := &ssh.ClientConfig{User: o.User, Auth: auth, HostKeyCallback: known.Callback,
		HostKeyAlgorithms: known.Algorithms(addr), Timeout: 15 * time.Second}
	return ssh.Dial("tcp", addr, cfg)
}

func runRemote(c *ssh.Client, cmd string, stdin io.Reader, out io.Writer) error {
	s, err := c.NewSession()
	if err != nil {
		return err
	}
	defer s.Close()
	s.Stdin, s.Stdout, s.Stderr = stdin, out, out
	return s.Run(cmd)
}

// upload streams the tree at root into dest on the Pi without holding the archive in memory.
func upload(c *ssh.Client, root, dest string, out io.Writer) error {
	pr, pw := io.Pipe()
	tarErr := make(chan error, 1)
	go func() {
		err := tarTree(pw, root)
		pw.CloseWithError(err)
		tarErr <- err
	}()
	err := runRemote(c, "mkdir -- "+shellQuote(dest)+" && tar -xzf - -C "+shellQuote(dest), pr, out)
	pr.CloseWithError(io.ErrClosedPipe) // unblocks the writer if tar on the Pi stopped reading early
	if terr := <-tarErr; terr != nil && !errors.Is(terr, io.ErrClosedPipe) {
		return terr
	}
	return err
}

var remoteTmpRe = regexp.MustCompile(`^/tmp/pitv-upgrade\.[A-Za-z0-9]+$`)

// requireFile checks that dir is the checkout it claims to be before its contents are run as
// root on the Pi.
func requireFile(dir, rel, what string) error {
	if dir == "" {
		return fmt.Errorf("no %s checkout found; give its path", what)
	}
	if _, err := os.Stat(filepath.Join(dir, rel)); err != nil {
		return fmt.Errorf("%q is not a %s checkout (no %s)", dir, what, rel)
	}
	return nil
}

// Upgrade uploads the trees and runs the install scripts as root. The trees go to a private
// directory made by mktemp, not a fixed /tmp path, because root runs what is in it: a fixed
// name could be created in advance by any local account. sudo gets the password on stdin,
// never on the command line, and -k makes it read the password every time so the line never
// reaches the script instead; the scripts' own stdin is /dev/null.
func Upgrade(o UpgradeOpts, out io.Writer) error {
	if err := requireFile(o.PiTVSrc, "setup/install.sh", "PiTV"); err != nil {
		return err
	}
	if o.ContentSrc != "" {
		if err := requireFile(o.ContentSrc, "setup/install-on-pi.sh", "PiTV_content"); err != nil {
			return err
		}
	}
	client, err := sshClient(o)
	if err != nil {
		return fmt.Errorf("ssh %s@%s: %w", o.User, o.Host, err)
	}
	defer client.Close()

	s, err := client.NewSession()
	if err != nil {
		return err
	}
	raw, err := s.Output("mktemp -d /tmp/pitv-upgrade.XXXXXXXX")
	s.Close()
	dir := strings.TrimSpace(string(raw))
	if err != nil || !remoteTmpRe.MatchString(dir) {
		return fmt.Errorf("could not create a temporary directory on the Pi: %v %q", err, dir)
	}
	// Best effort: the directory is private to the maintenance user and /tmp is emptied at boot.
	defer func() { _ = runRemote(client, "rm -rf -- "+shellQuote(dir), nil, io.Discard) }()

	sudo := func(what string, lines ...string) error {
		script := strings.Join(append([]string{"set -e", "log=" + installLog, "mkdir -p \"$(dirname \"$log\")\""}, lines...), "\n")
		if err := runRemote(client, "sudo -S -k -p '' bash -c "+shellQuote(script), strings.NewReader(o.Password+"\n"), out); err != nil {
			return fmt.Errorf("%s upgrade failed: %w (see %s on the Pi)", what, err, installLog)
		}
		return nil
	}

	fmt.Fprintln(out, "==> uploading PiTV sources")
	if err := upload(client, o.PiTVSrc, dir+"/pitv", out); err != nil {
		return fmt.Errorf("upload failed: %w", err)
	}
	env := ""
	if o.NASHost != "" {
		env = "NAS_HOST=" + shellQuote(o.NASHost) + " "
	}
	fmt.Fprintln(out, "==> installing PiTV")
	if err := sudo("PiTV",
		`echo "$(date '+%Y-%m-%d %H:%M:%S') upgrade: PiTV sources synced from installer" >> "$log"`,
		"rsync -a --delete --chown=root:root "+shellQuote(dir+"/pitv/")+" /opt/pitv-src/",
		"cd /opt/pitv-src",
		env+`bash ./setup/install.sh < /dev/null >> "$log" 2>&1`,
		"systemctl restart pitv-player pitv-web",
	); err != nil {
		return err
	}
	if o.ContentSrc != "" {
		fmt.Fprintln(out, "==> uploading pitv_content sources")
		if err := upload(client, o.ContentSrc, dir+"/content", out); err != nil {
			return fmt.Errorf("upload failed: %w", err)
		}
		fmt.Fprintln(out, "==> installing pitv_content")
		if err := sudo("pitv_content",
			"rsync -a --delete --chown=root:root "+shellQuote(dir+"/content/")+" /opt/pitv-content-src/",
			"cd /opt/pitv-content-src",
			`INSTALL_LOG="$log" bash ./setup/install-on-pi.sh < /dev/null >> "$log" 2>&1`,
		); err != nil {
			return err
		}
	}
	fmt.Fprintln(out, "==> upgrade complete")
	return nil
}

func shellQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'" }
