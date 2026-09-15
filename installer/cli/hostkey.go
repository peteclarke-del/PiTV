// SSH host key verification against the user's known_hosts file, trust-on-first-use style:
// an unknown Pi is shown with its fingerprint and recorded once accepted; a key that differs
// from the recorded one is refused, because that is what an impersonated Pi looks like.
package main

import (
	"crypto/ed25519"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"slices"

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

type knownHosts struct {
	path  string
	check ssh.HostKeyCallback
	trust func(host, fingerprint string) bool
}

// openKnownHosts loads path (creating it if missing); trust is asked before a new host is recorded.
func openKnownHosts(path string, trust func(host, fingerprint string) bool) (*knownHosts, error) {
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
	return &knownHosts{path: path, check: check, trust: trust}, nil
}

// Callback is the ssh.HostKeyCallback.
func (k *knownHosts) Callback(hostname string, remote net.Addr, key ssh.PublicKey) error {
	err := k.check(hostname, remote, key)
	if err == nil {
		return nil
	}
	var keyErr *knownhosts.KeyError
	if !errors.As(err, &keyErr) {
		return fmt.Errorf("host key for %s refused: %w", hostname, err) // revoked, or an unreadable entry
	}
	if len(keyErr.Want) > 0 {
		return fmt.Errorf("host key for %s does not match the one recorded in %s (if the Pi was reinstalled, remove its line there): %w", hostname, k.path, err)
	}
	fingerprint := ssh.FingerprintSHA256(key)
	if k.trust == nil || !k.trust(hostname, fingerprint) {
		return fmt.Errorf("host key for %s (%s) not trusted", hostname, fingerprint)
	}
	out, err := os.OpenFile(k.path, os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	_, err = out.WriteString(knownhosts.Line([]string{knownhosts.Normalize(hostname)}, key) + "\n")
	if cerr := out.Close(); err == nil {
		err = cerr
	}
	return err
}

// probeKey matches no real host, so checking it returns every key recorded for an address.
var probeKey = func() ssh.PublicKey {
	k, err := ssh.NewPublicKey(ed25519.PublicKey(make([]byte, ed25519.PublicKeySize)))
	if err != nil {
		panic(err) // unreachable: any 32-byte slice is an Ed25519 public key
	}
	return k
}()

// Algorithms lists the host key algorithms recorded for addr ("host:port"), for
// ssh.ClientConfig.HostKeyAlgorithms. Without it Go's preference (ECDSA before Ed25519) can
// fetch a key type that OpenSSH never recorded, which would then read as a changed key and
// teach the user to delete known_hosts lines. Nil for an unknown host.
func (k *knownHosts) Algorithms(addr string) []string {
	var keyErr *knownhosts.KeyError
	if err := k.check(addr, &net.TCPAddr{}, probeKey); !errors.As(err, &keyErr) {
		return nil
	}
	var algos []string
	for _, known := range keyErr.Want {
		switch t := known.Key.Type(); t {
		case ssh.KeyAlgoRSA: // an RSA key is offered with SHA-2 signatures; plain ssh-rsa is SHA-1
			algos = append(algos, ssh.KeyAlgoRSASHA512, ssh.KeyAlgoRSASHA256)
		default:
			algos = append(algos, t)
		}
	}
	slices.Sort(algos)
	return slices.Compact(algos)
}
