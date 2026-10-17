package client

import (
	"context"
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"io/fs"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

const secret = "Bearer s3cret-token"

func env(m map[string]string) func(string) (string, bool) {
	return func(k string) (string, bool) { v, ok := m[k]; return v, ok }
}

func assertRefusal(t *testing.T, err error, reason string) {
	t.Helper()
	r, ok := err.(*Refusal)
	if !ok || r.Reason != reason {
		t.Fatalf("want refusal %q, got %#v", reason, err)
	}
}

// --- a fake file system for the host-file checks ----------------------------

type fent struct {
	mode fs.FileMode
	uid  int
	data string
	dir  bool
}

type finfo struct {
	name string
	e    fent
}

func (f finfo) Name() string { return f.name }
func (f finfo) Size() int64  { return int64(len(f.e.data)) }
func (f finfo) Mode() fs.FileMode {
	if f.e.dir {
		return f.e.mode | fs.ModeDir
	}
	return f.e.mode
}
func (f finfo) ModTime() time.Time { return time.Time{} }
func (f finfo) IsDir() bool        { return f.e.dir }
func (f finfo) Sys() any           { return f.e.uid }

func fakeFS(files map[string]fent) FS {
	return FS{
		Lstat: func(p string) (fs.FileInfo, error) {
			e, ok := files[p]
			if !ok {
				return nil, fs.ErrNotExist
			}
			return finfo{filepath.Base(p), e}, nil
		},
		ReadFile: func(p string) ([]byte, error) {
			e, ok := files[p]
			if !ok {
				return nil, fs.ErrNotExist
			}
			return []byte(e.data), nil
		},
		Owner: func(fi fs.FileInfo) (int, bool) { u, ok := fi.Sys().(int); return u, ok },
	}
}

const hostPath = "/etc/zeo/endpoints-v1.json"

// goodTree is a root-owned tree holding the host file doc.
func goodTree(doc string) map[string]fent {
	return map[string]fent{
		"/":               {mode: 0o755, dir: true},
		"/etc":            {mode: 0o755, dir: true},
		"/etc/zeo":        {mode: 0o755, dir: true},
		hostPath:          {mode: 0o644, data: doc},
		"/etc/zeo/ca.pem": {mode: 0o644, data: "PEM"},
	}
}

func hostDoc(services string) string {
	return `{"schema":"zeo.endpoints/v1","ca_bundle":"/etc/zeo/ca.pem","services":` + services + `}`
}

func entry(origin string, extra string) string {
	return `{"origin":"` + origin + `","local":false,"audience":"aud","spki_sha256":null,"allow_env":false,"env_origins":[],"env_local":false` + extra + `}`
}

func loadHost(t *testing.T, doc string) *HostConfig {
	t.Helper()
	h, err := LoadHost(fakeFS(goodTree(doc)), hostPath)
	if err != nil || h == nil {
		t.Fatalf("host: %v %v", h, err)
	}
	return h
}

// --- a fake service over loopback TLS ---------------------------------------

type fake struct {
	srv   *httptest.Server
	mu    sync.Mutex
	hits  int
	seen  map[string]int // effects by idempotency key
	auth  []string
	roots *x509.CertPool
	mode  func(w http.ResponseWriter, r *http.Request) bool
}

func newFake(t *testing.T, mode func(w http.ResponseWriter, r *http.Request) bool) *fake {
	t.Helper()
	f := &fake{seen: map[string]int{}, mode: mode}
	f.srv = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		f.hits++
		f.auth = append(f.auth, r.Header.Get("Authorization"))
		f.mu.Unlock()
		if f.mode != nil && f.mode(w, r) {
			return
		}
		if k := r.Header.Get("Idempotency-Key"); k != "" {
			f.mu.Lock()
			f.seen[k]++
			f.mu.Unlock()
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"schema":"fake/v1","ok":true}`))
	}))
	f.roots = x509.NewCertPool()
	f.roots.AddCert(f.srv.Certificate())
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fake) origin() string { return strings.ToLower(f.srv.URL) }

func (f *fake) spki() string {
	sum := sha256.Sum256(f.srv.Certificate().RawSubjectPublicKeyInfo)
	return hex.EncodeToString(sum[:])
}

func (f *fake) client(ob *Outbox) *Client {
	return &Client{
		Endpoint:   Endpoint{Origin: f.origin(), Source: SourceHost, Audience: "aud", Credential: true},
		Roots:      f.roots,
		Credential: func() (string, error) { return secret, nil },
		Outbox:     ob,
		Service:    "zmsg",
		Sleep:      func(time.Duration) {},
		Jitter:     func(d time.Duration) time.Duration { return d },
	}
}

func (f *fake) count() int { f.mu.Lock(); defer f.mu.Unlock(); return f.hits }

func outbox(t *testing.T) *Outbox {
	t.Helper()
	dir, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	ob, err := OpenOutbox(filepath.Join(dir, "outbox"), "seat-a", func() time.Time { return time.Date(2026, 10, 17, 3, 0, 0, 0, time.UTC) })
	if err != nil {
		t.Fatal(err)
	}
	return ob
}

func readReq() Request {
	return Request{Service: "zmsg", Verb: "inbox", Method: "GET", Path: "/v1/inbox", Read: true, Audience: "aud"}
}

func writeReq(id string, spool bool) Request {
	r := Request{Service: "zmsg", Verb: "send", Method: "POST", Path: "/v1/send", Audience: "aud", OperationID: id,
		Body: []byte(`{"to":"architect","body":"the-private-body"}`)}
	if spool {
		r.Spool, r.Dedup = true, "zmsg.dedup/v1"
	}
	return r
}

func lostAnswer(w http.ResponseWriter, r *http.Request) bool {
	h, _, _ := w.(http.Hijacker).Hijack()
	h.Close()
	return true
}

func status(code int) func(http.ResponseWriter, *http.Request) bool {
	return func(w http.ResponseWriter, r *http.Request) bool { w.WriteHeader(code); return true }
}

// dead is a client whose service has gone away: connection refused.
func dead(t *testing.T, ob *Outbox) *Client {
	f := newFake(t, nil)
	c := f.client(ob)
	f.srv.Close()
	return c
}

func call(c *Client, r Request) Result {
	res := c.Call(context.Background(), r)
	return res
}

var hitsCounter atomic.Int64

func pemOf(f *fake) []byte {
	return pemEncode(f.srv.Certificate().Raw)
}

// devMode stands the test in a binary built with the zeodev tag.
func devMode(t *testing.T) {
	t.Helper()
	old := DevBuild
	DevBuild = true
	t.Cleanup(func() { DevBuild = old })
}
