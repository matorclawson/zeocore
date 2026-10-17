package client

import (
	"context"
	"encoding/json"
	"errors"
	"io/fs"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/zeroemployeeorg/zeocore/kit"
)

func env(m map[string]string) func(string) (string, bool) {
	return func(k string) (string, bool) { v, ok := m[k]; return v, ok }
}

func host(doc string, err error) func() ([]byte, error) {
	return func() ([]byte, error) { return []byte(doc), err }
}

func missing() ([]byte, error) { return nil, fs.ErrNotExist }

func hostDoc(services string) string {
	return `{"schema":"zeo.endpoints/v1","services":` + services + `}`
}

// Vector 1: a set variable wins over the host file.
func TestVector01EnvWinsOverHost(t *testing.T) {
	ep, err := Resolve("zmsg", env(map[string]string{"ZEO_ZMSG_ENDPOINT": "https://zmsg.zeo.ac"}),
		host(hostDoc(`{"zmsg":"https://other.zeo.ac"}`), nil))
	if err != nil || ep.URL != "https://zmsg.zeo.ac" || ep.Source != SourceEnv {
		t.Fatalf("got %+v, %v", ep, err)
	}
}

// Vector 2: with no variable the host file's local is local.
func TestVector02HostLocal(t *testing.T) {
	ep, err := Resolve("zmsg", env(nil), host(hostDoc(`{"zmsg":"local"}`), nil))
	if err != nil || !ep.Local || ep.Source != SourceHost {
		t.Fatalf("got %+v, %v", ep, err)
	}
}

// Vector 3: an empty variable is malformed; it does not fall through to the file.
func TestVector03EmptyVariableRefuses(t *testing.T) {
	_, err := Resolve("zmsg", env(map[string]string{"ZEO_ZMSG_ENDPOINT": ""}),
		host(hostDoc(`{"zmsg":"https://x.zeo.ac"}`), nil))
	assertRefusal(t, err, "endpoint")
}

// Vector 4: http, LOWER-case only local, and padding are malformed.
func TestVector04MalformedValues(t *testing.T) {
	for _, v := range []string{"http://zmsg.zeo.ac", "LOCAL", " local", "local ", "https://", "https://h/a b", "https://h/?q=1", "https://h/#f", "ftp://h", "https://h\x00", "https://héllo.example"} {
		_, err := Resolve("zmsg", env(map[string]string{"ZEO_ZMSG_ENDPOINT": v}), missing)
		assertRefusal(t, err, "endpoint")
	}
}

// Vector 5: userinfo refuses, and the message never holds the value.
func TestVector05UserinfoRefusesWithoutEchoing(t *testing.T) {
	v := "https://user:hunter2@h.example/"
	_, err := Resolve("zmsg", env(map[string]string{"ZEO_ZMSG_ENDPOINT": v}), missing)
	assertRefusal(t, err, "endpoint")
	if strings.Contains(err.Error(), "hunter2") || strings.Contains(err.Error(), "h.example") {
		t.Fatalf("the message echoes the value: %q", err)
	}
	if !strings.Contains(err.Error(), "ZEO_ZMSG_ENDPOINT") {
		t.Fatalf("the message should name the variable: %q", err)
	}
}

// Vector 6: nothing configured is a refusal, never a local run.
func TestVector06NotConfigured(t *testing.T) {
	ep, err := Resolve("zmsg", env(nil), missing)
	assertRefusal(t, err, "not_configured")
	if ep.Local {
		t.Fatal("an unconfigured service must never resolve to local")
	}
	_, err = Resolve("zmsg", env(nil), host(hostDoc(`{"other":"local"}`), nil))
	assertRefusal(t, err, "not_configured")
}

// Vector 7: a malformed host file refuses, but a valid variable still resolves.
func TestVector07MalformedHostFile(t *testing.T) {
	for _, doc := range []string{`{`, `{"schema":"x","services":{}}`, hostDoc(`{"zmsg":"local"}`) + ` {}`,
		`{"schema":"zeo.endpoints/v1","services":{},"extra":1}`} {
		_, err := Resolve("zmsg", env(nil), host(doc, nil))
		assertRefusal(t, err, "endpoint")
	}
	_, err := Resolve("zmsg", env(nil), host("", errors.New("permission denied")))
	assertRefusal(t, err, "endpoint")
	if _, err := Resolve("zmsg", env(map[string]string{"ZEO_ZMSG_ENDPOINT": "https://z.zeo.ac"}), host(`{`, nil)); err != nil {
		t.Fatalf("a valid variable should not read the file: %v", err)
	}
}

func TestEnvNameAndServiceNames(t *testing.T) {
	if got := EnvName("zeo-messenger"); got != "ZEO_ZEO_MESSENGER_ENDPOINT" {
		t.Fatal(got)
	}
	anyValue := func(string) (string, bool) { return "https://x.zeo.ac", true }
	for _, bad := range []string{"", "Zmsg", "-x", "a b", strings.Repeat("a", 33), "a.b"} {
		_, err := Resolve(bad, anyValue, missing)
		assertRefusal(t, err, "endpoint")
	}
}

func assertRefusal(t *testing.T, err error, reason string) {
	t.Helper()
	var r *Refusal
	if !errors.As(err, &r) || r.Reason != reason {
		t.Fatalf("want refusal %q, got %v", reason, err)
	}
}

// --- calls -----------------------------------------------------------------

const secret = "Bearer s3cret-token"

type fake struct {
	srv  *httptest.Server
	mu   sync.Mutex
	hits int
	seen map[string]int // effects by idempotency key
	mode func(w http.ResponseWriter, r *http.Request) bool
}

func newFake(t *testing.T, mode func(w http.ResponseWriter, r *http.Request) bool) *fake {
	f := &fake{seen: map[string]int{}, mode: mode}
	f.srv = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		f.hits++
		f.mu.Unlock()
		if f.mode != nil && f.mode(w, r) {
			return
		}
		if k := r.Header.Get("Idempotency-Key"); k != "" {
			f.mu.Lock()
			f.seen[k]++
			f.mu.Unlock()
		}
		w.WriteHeader(200)
	}))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fake) client(t *testing.T, ob *Outbox) *Client {
	return &Client{
		Endpoint:   Endpoint{URL: f.srv.URL, Source: SourceEnv},
		HTTP:       f.srv.Client(),
		Credential: func() (string, error) { return secret, nil },
		Outbox:     ob,
		Sleep:      func(time.Duration) {},
	}
}

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

func write(id string, spool bool) Request {
	return Request{Service: "zmsg", Verb: "send", Method: "POST", Path: "/v1/send", Body: []byte(`{"to":"architect","body":"the-private-body"}`), OperationID: id, Spoolable: spool}
}

func deadClient(t *testing.T, ob *Outbox) *Client {
	f := newFake(t, nil)
	c := f.client(t, ob)
	f.srv.Close()
	return c
}

// Vector 8: an unreachable read fails closed, nothing sent, exit 6.
func TestVector08UnreachableReadFailsClosed(t *testing.T) {
	c := deadClient(t, nil)
	res := c.Call(context.Background(), Request{Service: "zmsg", Verb: "inbox", Method: "GET", Path: "/v1/inbox", Read: true})
	if res.Outcome != OutcomeUnavailable || res.Sent || res.Exit() != kit.ExitUnavailable {
		t.Fatalf("got %+v exit %d", res, res.Exit())
	}
}

func TestAReadRetriesTwiceThenGivesUp(t *testing.T) {
	c := deadClient(t, nil)
	var slept []time.Duration
	c.Sleep = func(d time.Duration) { slept = append(slept, d) }
	c.Call(context.Background(), Request{Service: "zmsg", Verb: "inbox", Method: "GET", Path: "/v1/inbox", Read: true})
	if len(slept) != 2 || slept[0] != 200*time.Millisecond || slept[1] != 600*time.Millisecond {
		t.Fatalf("backoff %v", slept)
	}
}

func TestAWriteIsNeverRetriedInsideOneCall(t *testing.T) {
	c := deadClient(t, nil)
	n := 0
	c.Sleep = func(time.Duration) { n++ }
	c.Call(context.Background(), write("op-1", false))
	if n != 0 {
		t.Fatalf("a write slept %d times", n)
	}
}

// Vector 9: a spooling tool's offline write is spooled, exit 0.
func TestVector09OfflineSpoolableWriteIsSpooled(t *testing.T) {
	ob := outbox(t)
	res := deadClient(t, ob).Call(context.Background(), write("op-1", true))
	recs, _ := ob.List()
	if res.Outcome != OutcomeSpooled || res.Sent || res.Exit() != kit.ExitOK || res.SpoolID == nil || len(recs) != 1 || recs[0].State != StateQueued {
		t.Fatalf("got %+v %+v", res, recs)
	}
}

// Vector 10: a non-spoolable write refuses offline and leaves no record.
func TestVector10NonSpoolableWriteRefuses(t *testing.T) {
	ob := outbox(t)
	res := deadClient(t, ob).Call(context.Background(), write("op-1", false))
	recs, _ := ob.List()
	if res.Outcome != OutcomeRefused || *res.Reason != "offline" || res.Exit() != kit.ExitRefused || len(recs) != 0 {
		t.Fatalf("got %+v %+v", res, recs)
	}
}

func lostAnswer(w http.ResponseWriter, r *http.Request) bool {
	h, _, _ := w.(http.Hijacker).Hijack()
	h.Close()
	return true
}

// Vector 11: bytes sent, no complete answer: dispatch_unknown, retained, exit 1, never resent by a drain.
func TestVector11DispatchUnknownIsRetainedAndNeverResent(t *testing.T) {
	f := newFake(t, lostAnswer)
	ob := outbox(t)
	res := f.client(t, ob).Call(context.Background(), write("op-1", true))
	recs, _ := ob.List()
	if res.Outcome != OutcomeDispatchUnknown || !res.Sent || res.Exit() != kit.ExitFailed || *res.OperationID != "op-1" || len(recs) != 1 || recs[0].State != StateUnknown {
		t.Fatalf("got %+v %+v", res, recs)
	}
	hits := f.hits
	c := f.client(t, ob)
	if n, _ := ob.Drain(func(r Record) Result { return c.Call(context.Background(), write(r.OperationID, true)) }); n != 0 || f.hits != hits {
		t.Fatalf("a drain resent an unknown dispatch: drained %d, hits %d->%d", n, hits, f.hits)
	}
}

// A 5xx answer to a write may mean the service acted: dispatch_unknown.
func TestAWriteThatGetsA503IsUnknown(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, r *http.Request) bool { w.WriteHeader(503); return true })
	res := f.client(t, nil).Call(context.Background(), write("op-1", false))
	if res.Outcome != OutcomeDispatchUnknown {
		t.Fatalf("got %+v", res)
	}
}

// Vector 12: a drain interrupted after the server accepted resends the same key; one effect.
func TestVector12InterruptedDrainIsOneEffect(t *testing.T) {
	f := newFake(t, nil)
	ob := outbox(t)
	if _, err := ob.Enqueue("zmsg", "send", "op-1", []byte(`{"a":1}`)); err != nil {
		t.Fatal(err)
	}
	c := f.client(t, ob)
	send := func(r Record) Result { return c.Call(context.Background(), write(r.OperationID, true)) }
	recs, _ := ob.List()
	if send(recs[0]).Outcome != OutcomeOK { // the server accepted; the crash comes before Remove
		t.Fatal("first send failed")
	}
	if n, err := ob.Drain(send); err != nil || n != 1 {
		t.Fatalf("drain %d %v", n, err)
	}
	left, _ := ob.List()
	if len(left) != 0 || f.seen["op-1"] != 2 {
		t.Fatalf("left %v; the server saw the key %d times (it deduplicates by key)", left, f.seen["op-1"])
	}
}

// Vector 13: records drain in sequence order and a failure stops the later ones.
func TestVector13OrderIsKeptAndAFailureStops(t *testing.T) {
	ob := outbox(t)
	for _, id := range []string{"op-1", "op-2", "op-3"} {
		if _, err := ob.Enqueue("zmsg", "send", id, []byte(`{}`)); err != nil {
			t.Fatal(err)
		}
	}
	var order []string
	n, _ := ob.Drain(func(r Record) Result {
		order = append(order, r.OperationID)
		if r.OperationID == "op-2" {
			return Result{Outcome: OutcomeUnavailable}
		}
		return Result{Outcome: OutcomeOK}
	})
	left, _ := ob.List()
	if n != 1 || strings.Join(order, ",") != "op-1,op-2" || len(left) != 2 || left[0].OperationID != "op-2" {
		t.Fatalf("n=%d order=%v left=%v", n, order, left)
	}
}

func TestADrainMarksARefusalAndGoesOn(t *testing.T) {
	ob := outbox(t)
	for _, id := range []string{"op-1", "op-2"} {
		ob.Enqueue("zmsg", "send", id, []byte(`{}`))
	}
	n, _ := ob.Drain(func(r Record) Result {
		if r.OperationID == "op-1" {
			return Result{Outcome: OutcomeRefused}
		}
		return Result{Outcome: OutcomeOK}
	})
	left, _ := ob.List()
	if n != 1 || len(left) != 1 || left[0].State != StateRefused {
		t.Fatalf("n=%d left=%v", n, left)
	}
}

// Vector 14: a 401 and a stop are refused and never retried.
func TestVector14AuthAndStopAreNeverRetried(t *testing.T) {
	for code, reason := range map[int]string{401: "auth", 403: "stopped"} {
		f := newFake(t, func(w http.ResponseWriter, r *http.Request) bool { w.WriteHeader(code); return true })
		res := f.client(t, nil).Call(context.Background(), Request{Service: "zmsg", Verb: "inbox", Method: "GET", Path: "/v1/inbox", Read: true})
		if res.Outcome != OutcomeRefused || *res.Reason != reason || f.hits != 1 {
			t.Fatalf("%d: got %+v hits %d", code, res, f.hits)
		}
	}
}

// Vector 15: a full outbox refuses the next write and evicts nothing.
func TestVector15FullOutboxRefusesAndEvictsNothing(t *testing.T) {
	ob := outbox(t)
	ob.maxRecords = 2
	c := deadClient(t, ob)
	for _, id := range []string{"op-1", "op-2"} {
		if r := c.Call(context.Background(), write(id, true)); r.Outcome != OutcomeSpooled {
			t.Fatalf("%+v", r)
		}
	}
	res := c.Call(context.Background(), write("op-3", true))
	recs, _ := ob.List()
	if res.Outcome != OutcomeRefused || *res.Reason != "outbox_full" || len(recs) != 2 {
		t.Fatalf("got %+v %v", res, recs)
	}
}

func TestTheByteBoundAlsoRefuses(t *testing.T) {
	ob := outbox(t)
	ob.maxBytes = 10
	if _, err := ob.Enqueue("zmsg", "send", "op-1", []byte(`{"a":"0123456789"}`)); !errors.Is(err, ErrFull) {
		t.Fatalf("got %v", err)
	}
}

// Vector 16: the same operation id spooled twice is one record.
func TestVector16SameKeyIsOneRecord(t *testing.T) {
	ob := outbox(t)
	a, _ := ob.Enqueue("zmsg", "reply", "reply-msg-9", []byte(`{"x":1}`))
	b, _ := ob.Enqueue("zmsg", "reply", "reply-msg-9", []byte(`{"x":1}`))
	recs, _ := ob.List()
	if a.Seq != b.Seq || len(recs) != 1 {
		t.Fatalf("%v %v", a, recs)
	}
}

// Vector 17: no result and no refusal holds the credential, the endpoint or the body.
func TestVector17NothingSecretAppearsInAnyOutput(t *testing.T) {
	f := newFake(t, nil)
	f.srv.Close()
	ob := outbox(t)
	c := f.client(t, ob)
	var outs []string
	for _, r := range []Request{write("op-1", true), write("op-2", false), {Service: "zmsg", Verb: "inbox", Method: "GET", Path: "/v1/inbox", Read: true}} {
		outs = append(outs, string(c.Call(context.Background(), r).JSON()))
	}
	_, err := Resolve("zmsg", env(map[string]string{"ZEO_ZMSG_ENDPOINT": "https://u:p@leak.example"}), missing)
	outs = append(outs, err.Error())
	host := strings.TrimPrefix(f.srv.URL, "https://")
	for _, o := range outs {
		for _, bad := range []string{"s3cret-token", host, "the-private-body", "leak.example", "/v1/"} {
			if strings.Contains(o, bad) {
				t.Fatalf("output %q holds %q", o, bad)
			}
		}
	}
}

func TestAnInvalidCallSendsNothing(t *testing.T) {
	f := newFake(t, nil)
	c := f.client(t, nil)
	for _, r := range []Request{
		{Service: "zmsg", Verb: "v", Method: "POST", Path: "no-slash", OperationID: "op-1"},
		{Service: "zmsg", Verb: "v", Method: "POST", Path: "/a/../b", OperationID: "op-1"},
		{Service: "zmsg", Verb: "v", Method: "POST", Path: "/ok"},
		{Service: "zmsg", Verb: "v", Method: "POST", Path: "/ok", OperationID: "has space"},
	} {
		if res := c.Call(context.Background(), r); res.Outcome != OutcomeInvalid || res.Sent || res.Exit() != kit.ExitUsage {
			t.Fatalf("%+v -> %+v", r, res)
		}
	}
	if f.hits != 0 {
		t.Fatal("an invalid call reached the server")
	}
}

func TestALocalEndpointIsNotCalledOverHTTP(t *testing.T) {
	c := &Client{Endpoint: Endpoint{Local: true, Source: SourceHost}}
	if res := c.Call(context.Background(), write("op-1", true)); res.Outcome != OutcomeRefused || res.Sent {
		t.Fatalf("%+v", res)
	}
}

func TestTheResultIsOneDocumentWithNullFields(t *testing.T) {
	var m map[string]any
	if err := json.Unmarshal(Result{Service: "zmsg", Verb: "inbox", Outcome: OutcomeOK, EndpointSource: SourceEnv}.JSON(), &m); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"schema", "service", "verb", "outcome", "sent", "endpoint_source", "operation_id", "spool_id", "reason"} {
		if _, ok := m[k]; !ok {
			t.Fatalf("missing %s", k)
		}
	}
	if m["schema"] != ResultSchema || m["operation_id"] != nil {
		t.Fatalf("%v", m)
	}
}

func TestExitMapping(t *testing.T) {
	want := map[Outcome]kit.ExitCode{
		OutcomeOK: 0, OutcomeSpooled: 0, OutcomeInvalid: 2, OutcomeRefused: 3,
		OutcomeTimeout: 5, OutcomeUnavailable: 6, OutcomeDispatchUnknown: 1,
	}
	for o, code := range want {
		if got := (Result{Outcome: o}).Exit(); got != code {
			t.Errorf("%s: %d, want %d", o, got, code)
		}
	}
}

func TestAConfigCanLowerAPolicyAndNeverRaiseIt(t *testing.T) {
	got := Read.Clamp(Policy{Connect: time.Hour, Total: time.Second, Backoff: []time.Duration{time.Hour, time.Hour, time.Hour}})
	if got.Connect != Read.Connect || got.Total != time.Second || len(got.Backoff) != 2 {
		t.Fatalf("%+v", got)
	}
	if lowered := Read.Clamp(Policy{Backoff: []time.Duration{}}); len(lowered.Backoff) != 0 {
		t.Fatalf("%+v", lowered)
	}
}

func TestTheOutboxDirectoryIsChecked(t *testing.T) {
	base, _ := filepath.EvalSymlinks(t.TempDir())
	if _, err := OpenOutbox("relative/dir", "s", nil); err == nil {
		t.Fatal("a relative path should refuse")
	}
	if _, err := OpenOutbox(base+"/a/../b", "s", nil); err == nil {
		t.Fatal("an unclean path should refuse")
	}
	link := filepath.Join(base, "link")
	os.Mkdir(filepath.Join(base, "real"), 0o700)
	os.Symlink(filepath.Join(base, "real"), link)
	if _, err := OpenOutbox(filepath.Join(link, "ob"), "s", nil); err == nil {
		t.Fatal("a symlink in the path should refuse")
	}
	loose := filepath.Join(base, "loose")
	os.Mkdir(loose, 0o755)
	os.Chmod(loose, 0o755)
	if _, err := OpenOutbox(loose, "s", nil); err == nil {
		t.Fatal("mode 0755 should refuse")
	}
	if _, err := OpenOutbox(filepath.Join(base, "ok"), "bad sender", nil); err == nil {
		t.Fatal("a bad sender should refuse")
	}
}

func TestRecordsAreCanonicalPrivateFiles(t *testing.T) {
	ob := outbox(t)
	rec, err := ob.Enqueue("zmsg", "send", "op-1", []byte(`{"b":2,"a":1}`))
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(ob.dir, fileName(rec.Seq, rec.OperationID))
	st, _ := os.Stat(path)
	raw, _ := os.ReadFile(path)
	if st.Mode().Perm() != 0o600 || !strings.Contains(string(raw), `"request":{"a":1,"b":2}`) {
		t.Fatalf("mode %v doc %s", st.Mode(), raw)
	}
	if _, err := ob.Enqueue("zmsg", "send", "op-2", []byte(`not json`)); err == nil {
		t.Fatal("a non-JSON request should refuse")
	}
	left, _ := os.ReadDir(ob.dir)
	for _, e := range left {
		if strings.HasPrefix(e.Name(), ".tmp-") {
			t.Fatalf("a temporary file was left: %s", e.Name())
		}
	}
}

func TestStatsForDoctor(t *testing.T) {
	ob := outbox(t)
	ob.maxRecords = 10
	for i := 0; i < 9; i++ {
		ob.Enqueue("zmsg", "send", "op-"+string(rune('a'+i)), []byte(`{}`))
	}
	recs, _ := ob.List()
	ob.SetState(recs[0], StateUnknown)
	ob.now = func() time.Time { return time.Date(2026, 10, 17, 3, 0, 30, 0, time.UTC) }
	s, err := ob.Stats()
	if err != nil || s.Count != 9 || s.Unknown != 1 || s.Queued != 8 || !s.NearFull || s.OldestAge != 30*time.Second {
		t.Fatalf("%+v %v", s, err)
	}
}

func TestASlowReadTimesOutAndSaysSo(t *testing.T) {
	old := Read
	t.Cleanup(func() { Read = old })
	Read = Policy{Connect: time.Second, Total: 50 * time.Millisecond}
	f := newFake(t, func(w http.ResponseWriter, r *http.Request) bool { time.Sleep(400 * time.Millisecond); return false })
	res := f.client(t, nil).Call(context.Background(), Request{Service: "zmsg", Verb: "inbox", Method: "GET", Path: "/v1/inbox", Read: true})
	if res.Outcome != OutcomeTimeout || res.Exit() != kit.ExitTimeout {
		t.Fatalf("got %+v", res)
	}
}

func TestASlowWriteIsUnknownNotTimedOut(t *testing.T) {
	old := Write
	t.Cleanup(func() { Write = old })
	Write = Policy{Connect: time.Second, Total: 50 * time.Millisecond}
	f := newFake(t, func(w http.ResponseWriter, r *http.Request) bool { time.Sleep(400 * time.Millisecond); return false })
	res := f.client(t, nil).Call(context.Background(), write("op-1", false))
	if res.Outcome != OutcomeDispatchUnknown || !res.Sent {
		t.Fatalf("got %+v", res)
	}
}

// A read that lost its answer after the bytes went out is not retried.
func TestAReadIsNotRetriedOnceBytesWereSent(t *testing.T) {
	f := newFake(t, lostAnswer)
	res := f.client(t, nil).Call(context.Background(), Request{Service: "zmsg", Verb: "inbox", Method: "GET", Path: "/v1/inbox", Read: true})
	if res.Outcome != OutcomeUnavailable || !res.Sent || f.hits != 1 {
		t.Fatalf("got %+v hits %d", res, f.hits)
	}
}

func TestADrainMarksAnUnknownDispatchAndStops(t *testing.T) {
	ob := outbox(t)
	for _, id := range []string{"op-1", "op-2"} {
		ob.Enqueue("zmsg", "send", id, []byte(`{}`))
	}
	n, _ := ob.Drain(func(r Record) Result { return Result{Outcome: OutcomeDispatchUnknown} })
	left, _ := ob.List()
	if n != 0 || len(left) != 2 || left[0].State != StateUnknown || left[1].State != StateQueued {
		t.Fatalf("n=%d left=%v", n, left)
	}
}
