package client

import (
	"crypto/x509"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"syscall"
)

// MaxHostFile bounds the host configuration.
const MaxHostFile = 64 << 10

// HostEntry is one service in the host file.
type HostEntry struct {
	Origin   *string `json:"origin"`
	Local    bool    `json:"local"`
	Audience string  `json:"audience"`
	// CredentialFile is the root-owned file that holds the per-host credential.
	CredentialFile *string  `json:"credential_file"`
	SPKI           *string  `json:"spki_sha256"`
	AllowEnv       bool     `json:"allow_env"`
	EnvOrigins     []string `json:"env_origins"`
	EnvLocal       bool     `json:"env_local"`
}

// HostConfig is the root-owned, authoritative endpoint configuration.
type HostConfig struct {
	Schema   string               `json:"schema"`
	CABundle *string              `json:"ca_bundle"`
	Services map[string]HostEntry `json:"services"`
}

// FS is the part of the file system the host-file checks read; tests inject
// their own so that "owned by root" can be exercised without being root.
type FS struct {
	Lstat    func(string) (fs.FileInfo, error)
	ReadFile func(string) ([]byte, error)
	Owner    func(fs.FileInfo) (uid int, ok bool)
}

// SystemFS reads the real file system.
func SystemFS() FS {
	return FS{
		Lstat:    os.Lstat,
		ReadFile: os.ReadFile,
		Owner: func(fi fs.FileInfo) (int, bool) {
			st, ok := fi.Sys().(*syscall.Stat_t)
			if !ok {
				return 0, false
			}
			return int(st.Uid), true
		},
	}
}

// HostPath is the compiled-in path of the host file. It is not overridable by
// environment or flag. The Darwin path avoids /etc, which is a symlink to
// /private/etc and so fails the no-symlink rule. Neither path is claimed to
// exist today.
func HostPath() string {
	if runtime.GOOS == "darwin" {
		return "/Library/Application Support/Zero Employee/managed-endpoints-v1.json"
	}
	return "/etc/zeo/endpoints-v1.json"
}

func hostRefusal(msg string) error { return &Refusal{"host_config", msg} }

// checkPath enforces, for a path and every parent: no symbolic link, owner
// uid 0, no group or other write. The last element must be a regular file no
// larger than max.
func (f FS) checkPath(path string, max int64) error {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return hostRefusal("the host config path is not absolute and clean")
	}
	parts := strings.Split(strings.TrimPrefix(path, "/"), "/")
	cur := "/"
	for i := 0; i <= len(parts); i++ {
		if i > 0 {
			cur = filepath.Join(cur, parts[i-1])
		}
		fi, err := f.Lstat(cur)
		if err != nil {
			if i == len(parts) && errors.Is(err, fs.ErrNotExist) {
				return err
			}
			return hostRefusal("the host config path cannot be checked")
		}
		last := i == len(parts)
		if fi.Mode()&fs.ModeSymlink != 0 {
			return hostRefusal("the host config path holds a symbolic link")
		}
		if uid, ok := f.Owner(fi); !ok || uid != 0 {
			return hostRefusal("the host config path is not owned by root")
		}
		if fi.Mode().Perm()&0o022 != 0 {
			return hostRefusal("the host config path is writable by group or other")
		}
		if last {
			if !fi.Mode().IsRegular() || fi.Size() > max {
				return hostRefusal("the host config is not a regular file within its bound")
			}
		} else if !fi.IsDir() {
			return hostRefusal("a parent of the host config is not a directory")
		}
	}
	return nil
}

var originRe = regexp.MustCompile(`^https://[A-Za-z0-9.\-\[\]:]+$`)

// LoadHost reads and validates the host file at path. It returns (nil, nil)
// only when the file is absent; any other problem, including a wrong owner
// or mode, refuses with reason host_config: it never counts as "missing".
func LoadHost(f FS, path string) (*HostConfig, error) {
	if err := f.checkPath(path, MaxHostFile); err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	raw, err := f.ReadFile(path)
	if err != nil {
		return nil, hostRefusal("the host config cannot be read")
	}
	var cfg HostConfig
	if err := strictDecode(raw, &cfg); err != nil || cfg.Schema != EndpointsSchema {
		return nil, hostRefusal("the host config is malformed")
	}
	// The service grammar admits neither "_" nor ".", so two valid names can
	// never give one variable name: a collision cannot be written down.
	for name, e := range cfg.Services {
		if !validService(name) {
			return nil, hostRefusal("the host config names an invalid service")
		}
		if (e.Origin == nil) == !e.Local {
			return nil, hostRefusal("each service has exactly one of origin and local")
		}
		if e.Origin != nil {
			if _, err := parseOrigin(*e.Origin); err != nil || e.Audience == "" {
				return nil, hostRefusal("the host config entry for " + name + " is malformed")
			}
		}
		if e.Origin != nil && e.SPKI == nil && cfg.CABundle == nil {
			return nil, hostRefusal("a credentialed entry needs a ca_bundle or an spki_sha256")
		}
		if e.CredentialFile != nil {
			if err := f.checkPath(*e.CredentialFile, MaxHostFile); err != nil {
				return nil, hostRefusal("a credential file is not a root-owned regular file")
			}
		}
		for _, o := range e.EnvOrigins {
			if _, err := parseOrigin(o); err != nil {
				return nil, hostRefusal("the host config entry for " + name + " is malformed")
			}
		}
	}
	if cfg.CABundle != nil {
		if err := f.checkPath(*cfg.CABundle, MaxHostFile*64); err != nil {
			return nil, hostRefusal("the CA bundle is not a root-owned regular file")
		}
	} else if !systemRootsIgnoreEnv() {
		return nil, hostRefusal("ca_bundle is required on this platform")
	}
	return &cfg, nil
}

// CredentialFromFile reads the per-host credential from its root-owned file,
// checking the path again at read time. The credential proves the host, not a
// seat; it is never printed.
func CredentialFromFile(f FS, path string) func() (string, error) {
	return func() (string, error) {
		if err := f.checkPath(path, MaxHostFile); err != nil {
			return "", err
		}
		raw, err := f.ReadFile(path)
		if err != nil {
			return "", hostRefusal("the credential cannot be read")
		}
		return strings.TrimSpace(string(raw)), nil
	}
}

// systemRootsIgnoreEnv says whether the platform's verifier is immune to
// SSL_CERT_FILE and SSL_CERT_DIR. Go's Unix verifier reads them; Darwin's
// Security framework does not.
func systemRootsIgnoreEnv() bool { return platformIgnoresEnv }

// platformIgnoresEnv is a variable so a test can stand on the other platform.
var platformIgnoresEnv = runtime.GOOS == "darwin"

// Roots builds the trust roots for the host file: the root-owned bundle, or
// nil to use the platform verifier where that ignores the environment.
func (c *HostConfig) Roots(f FS) (*x509.CertPool, error) {
	if c.CABundle == nil {
		return nil, nil
	}
	raw, err := f.ReadFile(*c.CABundle)
	if err != nil {
		return nil, hostRefusal("the CA bundle cannot be read")
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(raw) {
		return nil, hostRefusal("the CA bundle holds no certificate")
	}
	return pool, nil
}
