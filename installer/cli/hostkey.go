// SSH host key verification against the user's known_hosts file, trust-on-first-use style:
// an unknown Pi is shown with its fingerprint and recorded once accepted; a key that differs
// from the recorded one is refused, because that is what an impersonated Pi looks like.
package main

import (
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"

	"golang.org/x/crypto/ssh"
	"golang.org/x/crypto/ssh/knownhosts"
)

func knownHostsPath() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".ssh", "known_hosts"), nil
}

// hostKeyCallback verifies against `path`, asking `trust` before recording a new host.
func hostKeyCallback(path string, trust func(host, fingerprint string) bool) (ssh.HostKeyCallback, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDONLY, 0o600)
	if err != nil {
		return nil, err
	}
	f.Close()
	check, err := knownhosts.New(path)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return func(hostname string, remote net.Addr, key ssh.PublicKey) error {
		err := check(hostname, remote, key)
		if err == nil {
			return nil
		}
		var keyErr *knownhosts.KeyError
		if !errors.As(err, &keyErr) || len(keyErr.Want) > 0 {
			return fmt.Errorf("host key for %s does not match the one recorded in %s (if the Pi was reinstalled, remove its line there): %w", hostname, path, err)
		}
		fingerprint := ssh.FingerprintSHA256(key)
		if trust == nil || !trust(hostname, fingerprint) {
			return fmt.Errorf("host key for %s (%s) not trusted", hostname, fingerprint)
		}
		out, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o600)
		if err != nil {
			return err
		}
		defer out.Close()
		_, err = out.WriteString(knownhosts.Line([]string{knownhosts.Normalize(hostname)}, key) + "\n")
		return err
	}, nil
}
