package client

import (
	"context"
	"crypto/x509"
	"encoding/json"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/zeroemployeeorg/zeocore/kit"
)

func valid(t *testing.T, r Result) Result {
	t.Helper()
	if !r.Valid() {
		t.Fatalf("a result outside the closed reason set: %s", r.JSON())
	}
	return r
}

func reason(r Result) string {
	if r.Reason == nil {
		return ""
	}
	return *r.Reason
}

// --- transport -----------------------------------------------------------

// T1: a redirect is not followed; the second origin receives nothing.
func TestT1RedirectIsNotFollowed(t *testing.T) {
	second := newFake(t, nil)
	first := newFake(t, func(w http.ResponseWriter, r *http.Request) bool {
		http.Redirect(w, r, second.srv.URL+"/v1/send", http.StatusTemporaryRedirect)
		return true
	})
	res := valid(t, call(first.client(outbox(t)), writeReq("op-1", false)))
	if res.Outcome != OutcomeDispatchUnknown || reason(res) != "redirect" || !res.Sent || res.Exit() != kit.ExitFailed {
		t.Fatalf("%s", res.JSON())
	}
	if second.count() != 0 {
		t.Fatal("the redirect target received the write")
	}
	if r := valid(t, call(first.client(nil), readReq())); r.Outcome != OutcomeUnavailable || reason(r) != "response_invalid" {
		t.Fatalf("%s", r.JSON())
	}
}

// T2: a proxy in the environment is never used.
func TestT2NoProxyFromTheEnvironment(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	got := make(chan struct{}, 1)
	go func() {
		if c, err := ln.Accept(); err == nil {
			c.Close()
			got <- struct{}{}
		}
	}()
	for _, k := range []string{"HTTPS_PROXY", "ALL_PROXY", "HTTP_PROXY", "https_proxy"} {
		t.Setenv(k, "http://"+ln.Addr().String())
	}
	f := newFake(t, nil)
	if res := call(f.client(nil), readReq()); res.Outcome != OutcomeOK {
		t.Fatalf("%s", res.JSON())
	}
	// net/http never proxies loopback, so the call above cannot show a proxy:
	// the transport itself must have none.
	if tr, ok := f.client(nil).httpClient(Read).Transport.(*http.Transport); !ok || tr.Proxy != nil {
		t.Fatal("the transport may consult the environment for a proxy")
	}
	select {
	case <-got:
		t.Fatal("the client used the proxy from the environment")
	case <-time.After(100 * time.Millisecond):
	}
}

// T3: SSL_CERT_FILE does not decide trust; only the given roots do.
func TestT3TrustComesOnlyFromTheGivenRoots(t *testing.T) {
	f := newFake(t, nil)
	file := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(file, pemOf(f), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("SSL_CERT_FILE", file)
	t.Setenv("SSL_CERT_DIR", filepath.Dir(file))
	c := f.client(nil)
	c.Roots = x509.NewCertPool() // empty: nothing is trusted
	res := valid(t, call(c, readReq()))
	if res.Outcome != OutcomeUnavailable || reason(res) != "tls" || res.Sent || f.count() != 0 {
		t.Fatalf("%s", res.JSON())
	}
	c.Roots = f.roots
	if res := call(c, readReq()); res.Outcome != OutcomeOK {
		t.Fatalf("%s", res.JSON())
	}
}

// T4: a pin that does not match refuses the connection, with no retry.
func TestT4SPKIPin(t *testing.T) {
	f := newFake(t, nil)
	c := f.client(nil)
	c.Endpoint.SPKI = strings.Repeat("0", 64)
	sleeps := 0
	c.Sleep = func(time.Duration) { sleeps++ }
	res := valid(t, call(c, readReq()))
	if res.Outcome != OutcomeUnavailable || reason(res) != "tls" || res.Sent || sleeps != 0 || f.count() != 0 {
		t.Fatalf("%s sleeps %d hits %d", res.JSON(), sleeps, f.count())
	}
	c.Endpoint.SPKI = f.spki()
	if res := call(c, readReq()); res.Outcome != OutcomeOK {
		t.Fatalf("%s", res.JSON())
	}
}

// T5: exactly one request reaches the server for a write whose answer is lost.
func TestT5NoTransparentReplay(t *testing.T) {
	f := newFake(t, lostAnswer)
	valid(t, call(f.client(outbox(t)), writeReq("op-1", false)))
	if f.count() != 1 {
		t.Fatalf("%d requests reached the server", f.count())
	}
}

// T6: the credential goes only to the pinned or listed origin.
func TestT6CredentialOnlyToAPinnedOrigin(t *testing.T) {
	f := newFake(t, nil)
	c := f.client(nil)
	if call(c, readReq()); f.auth[0] != secret {
		t.Fatalf("the pinned origin should get the credential: %q", f.auth[0])
	}
	c.Endpoint.Credential = false
	call(c, readReq())
	if f.auth[1] != "" {
		t.Fatalf("an origin the host file does not pin got the credential: %q", f.auth[1])
	}
}

// T7: size bounds.
func TestT7Sizes(t *testing.T) {
	f := newFake(t, nil)
	r := writeReq("op-1", false)
	r.Body = []byte(`"` + strings.Repeat("a", MaxRequest) + `"`)
	if res := call(f.client(outbox(t)), r); res.Outcome != OutcomeInvalid || f.count() != 0 {
		t.Fatalf("%s", res.JSON())
	}
	exact := func(n int) string {
		head, tail := `{"schema":"fake/v1","pad":"`, `"}`
		return head + strings.Repeat("a", n-len(head)-len(tail)) + tail
	}
	big := newFake(t, func(w http.ResponseWriter, r *http.Request) bool {
		_, _ = w.Write([]byte(exact(MaxResponse + 1)))
		return true
	})
	if res := valid(t, call(big.client(nil), readReq())); res.Outcome != OutcomeUnavailable || reason(res) != "response_invalid" {
		t.Fatalf("one byte over: %s", res.JSON())
	}
	fits := newFake(t, func(w http.ResponseWriter, r *http.Request) bool {
		_, _ = w.Write([]byte(exact(MaxResponse)))
		return true
	})
	if res := valid(t, call(fits.client(nil), readReq())); res.Outcome != OutcomeOK {
		t.Fatalf("exactly the bound: %s", res.JSON())
	}
	e := readReq()
	e.Path = "/v1/x"
	c := f.client(nil)
	if res := call(c, Request{Service: "zmsg", Verb: "v", Method: "GET", Path: "/ok", Read: true, Audience: "aud", Envelope: &Envelope{Schema: "s/v1", Audience: "a", Body: make([]byte, MaxEnvelope+1)}}); res.Outcome != OutcomeInvalid {
		t.Fatalf("%s", res.JSON())
	}
}

// T8: one deadline covers every attempt and wait; jitter stays in bounds.
func TestT8OneDeadlineAndJitter(t *testing.T) {
	old := Read
	t.Cleanup(func() { Read = old })
	Read = Policy{Connect: time.Second, Total: 100 * time.Millisecond, Backoff: []time.Duration{200 * time.Millisecond, 600 * time.Millisecond}}
	c := dead(t, nil)
	slept := 0
	c.Sleep = func(time.Duration) { slept++ }
	if res := valid(t, call(c, readReq())); res.Outcome != OutcomeUnavailable {
		t.Fatalf("%s", res.JSON())
	}
	if slept != 0 {
		t.Fatalf("a backoff longer than the time left was waited %d times", slept)
	}
	seen := map[time.Duration]bool{}
	for i := 0; i < 200; i++ {
		j := Jitter(200 * time.Millisecond)
		if j < 200*time.Millisecond || j > 300*time.Millisecond {
			t.Fatalf("jitter %v", j)
		}
		seen[j] = true
	}
	if len(seen) < 2 {
		t.Fatal("jitter does not vary")
	}
	if Jitter(0) != 0 {
		t.Fatal("jitter of zero")
	}
}

func TestAReadRetriesTwiceThenGivesUp(t *testing.T) {
	c := dead(t, nil)
	var slept []time.Duration
	c.Sleep = func(d time.Duration) { slept = append(slept, d) }
	valid(t, call(c, readReq()))
	if len(slept) != 2 || slept[0] != 200*time.Millisecond || slept[1] != 600*time.Millisecond {
		t.Fatalf("backoff %v", slept)
	}
}

// T9: a config can lower a policy and never raise it.
func TestT9ClampNeverRaises(t *testing.T) {
	if raised := Read.Clamp(Policy{Connect: time.Hour, Total: time.Hour}); raised.Connect != Read.Connect || raised.Total != Read.Total {
		t.Fatalf("raised: %+v", raised)
	}
	got := Read.Clamp(Policy{Connect: time.Hour, Total: time.Second, Backoff: []time.Duration{time.Hour, time.Hour, time.Hour}})
	if got.Connect != Read.Connect || got.Total != time.Second || len(got.Backoff) != 2 {
		t.Fatalf("%+v", got)
	}
	if lowered := Read.Clamp(Policy{Backoff: []time.Duration{}}); len(lowered.Backoff) != 0 {
		t.Fatalf("%+v", lowered)
	}
}

// T10 is checked with C-vectors below: nothing secret in any output.
func TestT10NothingSecretInAnyOutput(t *testing.T) {
	f := newFake(t, nil)
	f.srv.Close()
	c := f.client(outbox(t))
	var outs []string
	for _, r := range []Request{writeReq("op-1", true), writeReq("op-2", false), readReq()} {
		outs = append(outs, string(call(c, r).JSON()))
	}
	_, err := Resolve("a", env(map[string]string{"ZEO_A_ENDPOINT": "https://u:p@leak.example"}), nil)
	outs = append(outs, err.Error())
	d := c.Doctor(context.Background(), "zmsg", "/v1/health", "aud")
	b, _ := json.Marshal(d)
	outs = append(outs, string(b))
	host := strings.TrimPrefix(f.origin(), "https://")
	for _, o := range outs {
		for _, bad := range []string{"s3cret-token", "the-private-body", "leak.example", "/v1/send", "/v1/inbox", "/v1/health"} {
			if strings.Contains(o, bad) {
				t.Fatalf("output %q holds %q", o, bad)
			}
		}
		if strings.Contains(o, host) && !strings.Contains(o, `"Origin"`) {
			t.Fatalf("only doctor may print the origin: %q", o)
		}
	}
}

// --- calls ---------------------------------------------------------------

// C1: a plain read; the service's own document is carried, not replaced.
func TestC1PlainRead(t *testing.T) {
	f := newFake(t, nil)
	res := valid(t, call(f.client(nil), readReq()))
	if res.Outcome != OutcomeOK || res.Exit() != kit.ExitOK || !res.Sent || res.Result == nil || res.Result.Schema != "fake/v1" ||
		!strings.Contains(string(res.Result.Value), `"ok":true`) {
		t.Fatalf("%s", res.JSON())
	}
}

// C2: an unreachable read fails closed.
func TestC2UnreachableRead(t *testing.T) {
	res := valid(t, call(dead(t, nil), readReq()))
	if res.Outcome != OutcomeUnavailable || res.Sent || res.Exit() != kit.ExitUnavailable || res.Result != nil {
		t.Fatalf("%s", res.JSON())
	}
}

// C3: a read that lost its answer after send is not retried.
func TestC3ReadLostAnswerNotRetried(t *testing.T) {
	f := newFake(t, lostAnswer)
	res := valid(t, call(f.client(nil), readReq()))
	if res.Outcome != OutcomeUnavailable || !res.Sent || f.count() != 1 {
		t.Fatalf("%s hits %d", res.JSON(), f.count())
	}
}

// C4: deadlines: before any byte, and after the send.
func TestC4Deadlines(t *testing.T) {
	old := Read
	t.Cleanup(func() { Read = old })
	Read = Policy{Connect: time.Second, Total: 80 * time.Millisecond}
	slow := newFake(t, func(w http.ResponseWriter, r *http.Request) bool { time.Sleep(400 * time.Millisecond); return false })
	res := valid(t, call(slow.client(nil), readReq()))
	if res.Outcome != OutcomeTimeout || !res.Sent || res.Exit() != kit.ExitTimeout {
		t.Fatalf("after send: %s", res.JSON())
	}
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	defer ln.Close()
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			_ = c // hold the connection: the TLS handshake never completes
		}
	}()
	c := slow.client(nil)
	c.Endpoint.Origin = "https://" + ln.Addr().String()
	res = valid(t, call(c, readReq()))
	if res.Outcome != OutcomeTimeout || res.Sent {
		t.Fatalf("before send: %s", res.JSON())
	}
}

// C5: an offline write on a tool that does not spool is unavailable, nothing queued.
func TestC5OfflineNonSpoolWrite(t *testing.T) {
	ob := outbox(t)
	res := valid(t, call(dead(t, ob), writeReq("op-1", false)))
	recs, _ := ob.List()
	if res.Outcome != OutcomeUnavailable || reason(res) != "offline" || res.Sent || res.Exit() != kit.ExitUnavailable || len(recs) != 0 {
		t.Fatalf("%s %v", res.JSON(), recs)
	}
}

// C6: an offline write on a spooling tool is spooled and durable before exit 0.
func TestC6OfflineSpoolIsDurable(t *testing.T) {
	ob := outbox(t)
	files, dirs := 0, 0
	ob.syncFile = func(f *os.File) error { files++; return f.Sync() }
	ob.syncDir = func(d string) error { dirs++; return nil }
	res := valid(t, call(dead(t, ob), writeReq("op-1", true)))
	recs, _ := ob.List()
	if res.Outcome != OutcomeSpooled || res.Sent || res.Exit() != kit.ExitOK || res.SpoolID == nil || len(recs) != 1 || recs[0].State != StateQueued {
		t.Fatalf("%s %v", res.JSON(), recs)
	}
	if files == 0 || dirs == 0 {
		t.Fatalf("not durable: file syncs %d, directory syncs %d", files, dirs)
	}
}

// C7: --require-delivery turns an offline write into a visible failure.
func TestC7RequireDelivery(t *testing.T) {
	ob := outbox(t)
	r := writeReq("op-1", true)
	r.RequireDelivery = true
	res := valid(t, call(dead(t, ob), r))
	recs, _ := ob.List()
	if res.Outcome != OutcomeUnavailable || reason(res) != "require_delivery" || res.Exit() != kit.ExitUnavailable || len(recs) != 0 {
		t.Fatalf("%s %v", res.JSON(), recs)
	}
}

// C8: a lost answer after send is dispatch_unknown, retained, exit 1.
func TestC8DispatchUnknown(t *testing.T) {
	f := newFake(t, lostAnswer)
	ob := outbox(t)
	res := valid(t, call(f.client(ob), writeReq("op-1", true)))
	recs, _ := ob.List()
	if res.Outcome != OutcomeDispatchUnknown || !res.Sent || res.Exit() != kit.ExitFailed || *res.OperationID != "op-1" || res.SpoolID == nil ||
		len(recs) != 1 || recs[0].State != StateUnknown {
		t.Fatalf("%s %v", res.JSON(), recs)
	}
	// Calling again with the same key does not send again.
	hits := f.count()
	again := valid(t, call(f.client(ob), writeReq("op-1", true)))
	if again.Outcome != OutcomeDispatchUnknown || f.count() != hits {
		t.Fatalf("%s hits %d -> %d", again.JSON(), hits, f.count())
	}
}

// C9: a write deadline after send is dispatch_unknown.
func TestC9WriteDeadlineAfterSend(t *testing.T) {
	old := Write
	t.Cleanup(func() { Write = old })
	Write = Policy{Connect: time.Second, Total: 80 * time.Millisecond}
	f := newFake(t, func(w http.ResponseWriter, r *http.Request) bool { time.Sleep(400 * time.Millisecond); return false })
	res := valid(t, call(f.client(outbox(t)), writeReq("op-1", false)))
	if res.Outcome != OutcomeDispatchUnknown || reason(res) != "timeout_after_send" || !res.Sent {
		t.Fatalf("%s", res.JSON())
	}
}

// C10: any non-typed non-2xx answer to a write, after send, is dispatch_unknown.
func TestC10ServerErrorOnAWriteIsUnknown(t *testing.T) {
	for _, code := range []int{500, 502, 503, 418, 429} {
		f := newFake(t, status(code))
		res := valid(t, call(f.client(outbox(t)), writeReq("op-1", false)))
		if res.Outcome != OutcomeDispatchUnknown || reason(res) != "server_error" || res.SpoolID == nil {
			t.Fatalf("%d: %s", code, res.JSON())
		}
	}
	f := newFake(t, status(503))
	if res := valid(t, call(f.client(nil), readReq())); res.Outcome != OutcomeUnavailable || reason(res) != "server_error" {
		t.Fatalf("%s", res.JSON())
	}
}

// C11: a bad answer to a write is dispatch_unknown; to a read, unavailable.
func TestC11BadAnswers(t *testing.T) {
	bodies := map[string]string{
		"truncated": `{"schema":"fake/v1"`,
		"malformed": `not json`,
		"no schema": `{"ok":true}`,
		"dup key":   `{"schema":"fake/v1","a":1,"a":2}`,
		"empty":     ``,
		"wrong":     `{"schema":"other/v1"}`,
	}
	for name, body := range bodies {
		f := newFake(t, func(w http.ResponseWriter, r *http.Request) bool { _, _ = w.Write([]byte(body)); return true })
		w := writeReq("op-"+strings.ReplaceAll(name, " ", "-"), false)
		w.ExpectSchema = "fake/v1"
		if res := valid(t, call(f.client(outbox(t)), w)); res.Outcome != OutcomeDispatchUnknown || reason(res) != "response_invalid" {
			t.Errorf("write %s: %s", name, res.JSON())
		}
		r := readReq()
		r.ExpectSchema = "fake/v1"
		if res := valid(t, call(f.client(nil), r)); res.Outcome != OutcomeUnavailable || reason(res) != "response_invalid" {
			t.Errorf("read %s: %s", name, res.JSON())
		}
		if name == "wrong" {
			continue // valid without an expected schema
		}
		if res := valid(t, call(f.client(nil), readReq())); res.Outcome != OutcomeUnavailable || reason(res) != "response_invalid" {
			t.Errorf("read %s, no expected schema: %s", name, res.JSON())
		}
	}
	// A body cut short by the connection, though what arrived is valid JSON.
	cut := newFake(t, func(w http.ResponseWriter, r *http.Request) bool {
		conn, buf, _ := w.(http.Hijacker).Hijack()
		_, _ = buf.WriteString("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n" + `{"schema":"fake/v1"}`)
		_ = buf.Flush()
		conn.Close()
		return true
	})
	if res := valid(t, call(cut.client(nil), readReq())); res.Outcome != OutcomeUnavailable || reason(res) != "response_invalid" {
		t.Errorf("cut read: %s", res.JSON())
	}
	if res := valid(t, call(cut.client(outbox(t)), writeReq("op-cut", false))); res.Outcome != OutcomeDispatchUnknown {
		t.Errorf("cut write: %s", res.JSON())
	}
}

// C12 to C14: definite answers.
func TestC12ToC14DefiniteAnswers(t *testing.T) {
	cases := []struct {
		code    int
		outcome Outcome
		reason  string
		exit    kit.ExitCode
	}{
		{401, OutcomeRefused, "auth", kit.ExitRefused},
		{403, OutcomeRefused, "stopped", kit.ExitRefused},
		{404, OutcomeNotFound, "not_found", kit.ExitNotFound},
		{400, OutcomeInvalid, "request", kit.ExitUsage},
		{422, OutcomeInvalid, "request", kit.ExitUsage},
	}
	for _, c := range cases {
		f := newFake(t, status(c.code))
		res := valid(t, call(f.client(nil), readReq()))
		if res.Outcome != c.outcome || reason(res) != c.reason || res.Exit() != c.exit || f.count() != 1 {
			t.Fatalf("%d: %s hits %d", c.code, res.JSON(), f.count())
		}
		ob := outbox(t)
		w := valid(t, call(f.client(ob), writeReq("op-1", false)))
		recs, _ := ob.List()
		if w.Outcome != c.outcome || len(recs) != 0 {
			t.Fatalf("write %d: %s %v", c.code, w.JSON(), recs)
		}
	}
}

// C15 and C16: a TLS failure and a DNS failure send nothing.
func TestC15C16TLSAndDNS(t *testing.T) {
	f := newFake(t, nil)
	c := f.client(nil)
	c.Roots = x509.NewCertPool()
	if res := valid(t, call(c, readReq())); reason(res) != "tls" || res.Sent {
		t.Fatalf("%s", res.JSON())
	}
	c = f.client(nil)
	c.Endpoint.Origin = "https://no-such-host.invalid"
	if res := valid(t, call(c, readReq())); res.Outcome != OutcomeUnavailable || res.Sent {
		t.Fatalf("%s", res.JSON())
	}
}

// C17 to C19: refusals before anything is sent.
func TestC17ToC19Refusals(t *testing.T) {
	f := newFake(t, nil)
	r := writeReq("op-1", true)
	r.Dedup = ""
	if res := valid(t, call(f.client(outbox(t)), r)); res.Outcome != OutcomeRefused || reason(res) != "not_spoolable" {
		t.Fatalf("%s", res.JSON())
	}
	if res := valid(t, call(f.client(nil), writeReq("op-1", false))); reason(res) != "no_outbox" || res.Exit() != kit.ExitRefused {
		t.Fatalf("%s", res.JSON())
	}
	for _, id := range []string{"has space", "../x", "a/b", strings.Repeat("a", 65), "a\x00b", "a.b"} {
		if res := valid(t, call(f.client(outbox(t)), writeReq(id, false))); res.Outcome != OutcomeInvalid || reason(res) != "operation_id" {
			t.Fatalf("%q: %s", id, res.JSON())
		}
	}
	if f.count() != 0 {
		t.Fatal("a refused call reached the server")
	}
}

func TestAnInvalidCallSendsNothing(t *testing.T) {
	f := newFake(t, nil)
	c := f.client(outbox(t))
	bad := []Request{
		{Service: "zmsg", Verb: "v", Method: "POST", Path: "no-slash", OperationID: "op-1", Body: []byte(`{}`)},
		{Service: "zmsg", Verb: "v", Method: "POST", Path: "/a/../b", OperationID: "op-1", Body: []byte(`{}`)},
		{Service: "zmsg", Verb: "v", Method: "TRACE", Path: "/ok", OperationID: "op-1", Body: []byte(`{}`)},
		{Service: "Zmsg", Verb: "v", Method: "GET", Path: "/ok", Read: true},
		{Service: "zmsg", Verb: "", Method: "GET", Path: "/ok", Read: true},
		{Service: "zmsg", Verb: "v", Method: "POST", Path: "/ok", OperationID: "op-1", Body: []byte(`not json`)},
	}
	for _, r := range bad {
		if res := valid(t, call(c, r)); res.Outcome != OutcomeInvalid || res.Sent || res.Exit() != kit.ExitUsage {
			t.Fatalf("%+v -> %s", r, res.JSON())
		}
	}
	if f.count() != 0 {
		t.Fatal("an invalid call reached the server")
	}
}

func TestAWriteWithoutAnIdGetsOne(t *testing.T) {
	f := newFake(t, nil)
	r := writeReq("", false)
	res := valid(t, call(f.client(outbox(t)), r))
	if res.Outcome != OutcomeOK || res.OperationID == nil || len(*res.OperationID) != 32 {
		t.Fatalf("%s", res.JSON())
	}
	if NewOperationID() == NewOperationID() {
		t.Fatal("ids must differ")
	}
}

func TestALocalEndpointIsNotCalledOverHTTP(t *testing.T) {
	f := newFake(t, nil)
	c := &Client{Endpoint: Endpoint{Local: true, Origin: f.origin(), Source: SourceHost}, Roots: f.roots, Outbox: outbox(t)}
	if res := valid(t, call(c, readReq())); res.Outcome != OutcomeRefused || f.count() != 0 {
		t.Fatalf("%s", res.JSON())
	}
	c.Endpoint.Origin = ""
	if res := valid(t, call(c, writeReq("op-1", true))); res.Outcome != OutcomeRefused || reason(res) != "endpoint" || res.Sent {
		t.Fatalf("%s", res.JSON())
	}
}

func TestACredentialErrorRefuses(t *testing.T) {
	f := newFake(t, nil)
	c := f.client(nil)
	c.Credential = func() (string, error) { return "", os.ErrNotExist }
	if res := valid(t, call(c, readReq())); reason(res) != "credential" || f.count() != 0 {
		t.Fatalf("%s", res.JSON())
	}
}

func TestTheEnvelopeIsCarriedUnchanged(t *testing.T) {
	var got string
	f := newFake(t, func(w http.ResponseWriter, r *http.Request) bool { got = r.Header.Get("Zeo-Envelope"); return false })
	r := readReq()
	r.Envelope = &Envelope{Schema: "ledger.envelope/v1", Audience: "ledger-1", Body: []byte("opaque")}
	call(f.client(nil), r)
	if got != "ledger.envelope/v1 ledger-1 b3BhcXVl" {
		t.Fatalf("%q", got)
	}
}

// --- result document and exit codes ---------------------------------------

func TestX1ToX8ExitCodes(t *testing.T) {
	want := map[Outcome]kit.ExitCode{
		OutcomeOK: 0, OutcomeSpooled: 0, OutcomeInvalid: 2, OutcomeRefused: 3, OutcomeNotFound: 4,
		OutcomeTimeout: 5, OutcomeUnavailable: 6, OutcomeNotReady: 7, OutcomeDispatchUnknown: 1,
	}
	if len(want) != len(Reasons) {
		t.Fatalf("an outcome has no exit: %d vs %d", len(want), len(Reasons))
	}
	for o, code := range want {
		if got := (Result{Outcome: o}).Exit(); got != code {
			t.Errorf("%s: %d, want %d", o, got, code)
		}
	}
	// Exit 1 only for after-send ambiguity: no pre-send outcome maps to it.
	for o := range Reasons {
		if o != OutcomeDispatchUnknown && (Result{Outcome: o}).Exit() == kit.ExitFailed {
			t.Errorf("%s must not exit 1", o)
		}
	}
}

func TestX9TheResultDocumentIsStrict(t *testing.T) {
	good := Result{Service: "zmsg", Verb: "inbox", Outcome: OutcomeOK, EndpointSource: SourceHost}.JSON()
	if _, err := ParseResult(good); err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(good, &m); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"schema", "service", "verb", "outcome", "reason", "sent", "transport", "endpoint_source", "operation_id", "spool_id", "result"} {
		if _, ok := m[k]; !ok {
			t.Fatalf("missing %s", k)
		}
	}
	bad := []string{
		strings.Replace(string(good), `"sent"`, `"extra":1,"sent"`, 1),
		strings.Replace(string(good), `"verb":"inbox"`, `"verb":"inbox","verb":"x"`, 1),
		strings.Replace(string(good), `"outcome":"ok"`, `"outcome":"ok","reason":"auth"`, 1),
		strings.Replace(string(good), `"reason":null`, `"reason":"made_up"`, 1),
		string(good) + `{}`,
	}
	for _, b := range bad {
		if _, err := ParseResult([]byte(b)); err == nil {
			t.Errorf("accepted %s", b)
		}
	}
}

func TestEveryReasonIsInItsClosedSet(t *testing.T) {
	if (Result{Outcome: OutcomeRefused}).Valid() || (Result{Outcome: OutcomeDispatchUnknown}).Valid() {
		t.Fatal("an outcome that needs a reason is valid without one")
	}
	if ok := "auth"; (Result{Outcome: OutcomeOK, Reason: &ok}).Valid() {
		t.Fatal("ok carries no reason")
	}
	for o, rs := range Reasons {
		for _, r := range rs {
			if !(Result{Outcome: o, Reason: &r}).Valid() {
				t.Errorf("%s/%s", o, r)
			}
			other := "zzz"
			if (Result{Outcome: o, Reason: &other}).Valid() {
				t.Errorf("%s accepted an unknown reason", o)
			}
		}
	}
	if (Result{Outcome: "made_up"}).Valid() {
		t.Fatal("an unknown outcome is valid")
	}
}

// X10: doctor prints the origin only.
func TestX10DoctorPrintsTheOriginOnly(t *testing.T) {
	f := newFake(t, nil)
	ob := outbox(t)
	d := f.client(ob).Doctor(context.Background(), "zmsg", "/v1/health", "aud")
	if d.Origin != f.origin() || strings.Count(d.Origin[len("https://"):], "/") != 0 || !d.Answers || !d.CredentialPins || d.Outbox == nil {
		t.Fatalf("%+v", d)
	}
	if d2 := dead(t, nil).Doctor(context.Background(), "zmsg", "/v1/health", "aud"); d2.Answers {
		t.Fatal("a dead service should not answer")
	}
}
