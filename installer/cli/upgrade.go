// Network upgrade: sync the source trees to the Pi over SSH and re-run both install scripts.
// The card stays in the Pi; the work partition and the USB drive are untouched.
package main

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"fmt"
	"io"
	"io/fs"
	"net"
	"os"
	"path/filepath"
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
	NASHost    string
}

var skipDirs = map[string]bool{".git": true, ".venv": true, ".dev": true, "node_modules": true, "dist": true, "__pycache__": true, ".pytest_cache": true}

func tarTree(root string) ([]byte, error) {
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(root, p)
		if rel == "." {
			return nil
		}
		if d.IsDir() && skipDirs[d.Name()] {
			return filepath.SkipDir
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		hdr, err := tar.FileInfoHeader(info, "")
		if err != nil {
			return err
		}
		hdr.Name = filepath.ToSlash(rel)
		if err := tw.WriteHeader(hdr); err != nil {
			return err
		}
		if !d.IsDir() {
			f, err := os.Open(p)
			if err != nil {
				return err
			}
			_, err = io.Copy(tw, f)
			f.Close()
			return err
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	tw.Close()
	gz.Close()
	return buf.Bytes(), nil
}

func sshClient(o UpgradeOpts) (*ssh.Client, error) {
	var auth []ssh.AuthMethod
	if o.KeyFile != "" {
		key, err := os.ReadFile(expandHome(o.KeyFile))
		if err != nil {
			return nil, err
		}
		signer, err := ssh.ParsePrivateKey(key)
		if err != nil {
			return nil, err
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
	hostKey, err := hostKeyCallback(khPath, func(host, fingerprint string) bool {
		fmt.Printf("The authenticity of host %s cannot be established.\n  %s\n", host, fingerprint)
		return newPrompter().askBool("Trust this host and remember its key", false)
	})
	if err != nil {
		return nil, err
	}
	cfg := &ssh.ClientConfig{User: o.User, Auth: auth, HostKeyCallback: hostKey, Timeout: 15 * time.Second}
	addr := o.Host
	if _, _, err := net.SplitHostPort(addr); err != nil {
		addr = net.JoinHostPort(addr, "22")
	}
	return ssh.Dial("tcp", addr, cfg)
}

func runRemote(c *ssh.Client, cmd string, stdin []byte, out io.Writer) error {
	s, err := c.NewSession()
	if err != nil {
		return err
	}
	defer s.Close()
	if stdin != nil {
		s.Stdin = bytes.NewReader(stdin)
	}
	s.Stdout, s.Stderr = out, out
	return s.Run(cmd)
}

// Upgrade uploads the trees and runs the install scripts with sudo (the maintenance user
// is in the sudo group; the password is passed on stdin, never on the command line, and
// -k makes sudo read it every time so it never lands on a script's stdin instead).
func Upgrade(o UpgradeOpts, out io.Writer) error {
	client, err := sshClient(o)
	if err != nil {
		return fmt.Errorf("ssh %s@%s: %w", o.User, o.Host, err)
	}
	defer client.Close()
	sudo := "sudo -S -k -p '' "
	pw := []byte(o.Password + "\n")
	fmt.Fprintln(out, "==> uploading PiTV sources")
	tree, err := tarTree(o.PiTVSrc)
	if err != nil {
		return err
	}
	if err := runRemote(client, "mkdir -p /tmp/pitv-up && tar -xzf - -C /tmp/pitv-up", tree, out); err != nil {
		return fmt.Errorf("upload failed: %w", err)
	}
	script := strings.Join([]string{
		"set -e",
		"mkdir -p /work/install",
		"echo \"$(date '+%Y-%m-%d %H:%M:%S') upgrade: PiTV sources synced from installer\" >> /work/install/install.log",
		"rsync -a --delete /tmp/pitv-up/ /opt/pitv-src/",
		"cd /opt/pitv-src && NAS_HOST=" + shellQuote(o.NASHost) + " ./setup/install.sh >> /work/install/install.log 2>&1",
		"systemctl restart pitv-player pitv-web",
	}, " && ")
	if err := runRemote(client, sudo+"bash -c "+shellQuote(script), pw, out); err != nil {
		return fmt.Errorf("PiTV upgrade failed: %w (see /work/install/install.log on the Pi)", err)
	}
	if o.ContentSrc != "" {
		fmt.Fprintln(out, "==> uploading pitv_content sources")
		tree, err := tarTree(o.ContentSrc)
		if err != nil {
			return err
		}
		if err := runRemote(client, "mkdir -p /tmp/pitv-content-up && tar -xzf - -C /tmp/pitv-content-up", tree, out); err != nil {
			return fmt.Errorf("upload failed: %w", err)
		}
		script := strings.Join([]string{
			"set -e",
			"rsync -a --delete /tmp/pitv-content-up/ /opt/pitv-content-src/",
			"cd /opt/pitv-content-src && INSTALL_LOG=/work/install/install.log ./setup/install-on-pi.sh >> /work/install/install.log 2>&1",
		}, " && ")
		if err := runRemote(client, sudo+"bash -c "+shellQuote(script), pw, out); err != nil {
			return fmt.Errorf("pitv_content upgrade failed: %w", err)
		}
	}
	_ = runRemote(client, "rm -rf /tmp/pitv-up /tmp/pitv-content-up", nil, io.Discard)
	fmt.Fprintln(out, "==> upgrade complete")
	return nil
}

func shellQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'" }
