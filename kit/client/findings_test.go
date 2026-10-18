package client

import (
	"context"
	"crypto/tls"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// The council's P1-A on #141: a bad TLS record AFTER the request was handed to
// the connection is sent, so a write is dispatch_unknown and its record stays.
func TestATLSRecordErrorAfterTheSendIsDispatchUnknown(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, r *http.Request) bool {
		buf := make([]byte, 1<<16)
		for {
			if _, err := r.Body.Read(buf); err != nil {
				break
			}
		}
		conn, _, err := w.(http.Hijacker).Hijack()
		if err != nil {
			return true
		}
		raw := conn.(*tls.Conn).NetConn()
		_, _ = raw.Write([]byte{0x17, 0x99, 0x99, 0x00, 0x05, 1, 2, 3, 4, 5})
		_ = raw.Close()
		return true
	})
	ob := outbox(t)
	res := valid(t, call(f.client(ob), writeReq("op-1", false)))
	if res.Outcome != OutcomeDispatchUnknown || !res.Sent {
		t.Fatalf("a write whose answer was a bad record is %s sent=%v: %s", res.Outcome, res.Sent, res.JSON())
	}
	recs, _ := ob.List()
	if len(recs) != 1 || recs[0].State != StateUnknown {
		t.Fatalf("the record must stay unknown: %v", recs)
	}
	// A certificate failure BEFORE the request is still a refusal and sends nothing.
	g := newFake(t, nil)
	c := g.client(outbox(t))
	c.Roots = nil
	if r := valid(t, call(c, readReq())); r.Outcome != OutcomeRefused || reason(r) != "tls" || g.count() != 0 {
		t.Fatalf("%s hits %d", r.JSON(), g.count())
	}
}

// P2-B: the doctor probe holds the same pin as a call.
func TestDoctorKeepsThePinnedOriginAndAudience(t *testing.T) {
	pinned, other := newFake(t, nil), newFake(t, nil)
	c := pinned.client(outbox(t))
	for _, path := range []string{"@" + strings.TrimPrefix(other.origin(), "https://") + "/h", "health", "/h#x", "/a\\b"} {
		if d := c.Doctor(context.Background(), "zmsg", path, "aud"); d.Answers || pinned.count() != 0 || other.count() != 0 {
			t.Errorf("path %q probed: answers %v, pinned %d, other %d", path, d.Answers, pinned.count(), other.count())
		}
	}
	if d := c.Doctor(context.Background(), "zmsg", "/health", "wrong"); d.Answers || pinned.count() != 0 {
		t.Errorf("a wrong audience probed: %v %d", d.Answers, pinned.count())
	}
	if d := c.Doctor(context.Background(), "zmsg", "/health", ""); d.Answers || pinned.count() != 0 {
		t.Errorf("an empty audience probed: %v %d", d.Answers, pinned.count())
	}
	if d := c.Doctor(context.Background(), "zmsg", "/health", "aud"); !d.Answers || pinned.count() != 1 {
		t.Errorf("a valid probe was refused: %v %d", d.Answers, pinned.count())
	}
}

func TestACallPathCannotNameAnotherHost(t *testing.T) {
	pinned, other := newFake(t, nil), newFake(t, nil)
	c := pinned.client(outbox(t))
	r := readReq()
	r.Path = "@" + strings.TrimPrefix(other.origin(), "https://") + "/h"
	if res := valid(t, call(c, r)); res.Outcome != OutcomeInvalid || other.count() != 0 {
		t.Fatalf("%s other %d", res.JSON(), other.count())
	}
}

// P2-B: a drain names its service; a record goes only to the client of its own service.
func TestDrainNeedsAServiceAndReconcileKeepsTheServiceAndAudience(t *testing.T) {
	f := newFake(t, lostAnswer)
	ob := outbox(t)
	c := f.client(ob)
	enqueue(t, ob, "op-q")
	c.Service = ""
	if n, err := c.Drain(context.Background()); err == nil || n != 0 || f.count() != 0 {
		t.Fatalf("a drain with no service: %d %v hits %d", n, err, f.count())
	}
	c.Service = "zmsg"
	// A ledger record on the zmsg client.
	if _, err := ob.Enqueue("ledger", "send", "op-l", StoredRequest{Method: "POST", Path: "/v1/send", Audience: "aud", Dedup: "zmsg.dedup/v1", Body: []byte(`{"n":1}`)}); err != nil {
		t.Fatal(err)
	}
	recs, _ := ob.List()
	for _, r := range recs {
		_ = ob.SetState(r, StateUnknown)
	}
	before := f.count()
	res := valid(t, c.Reconcile(context.Background(), "op-l"))
	if res.Outcome != OutcomeRefused || reason(res) != "origin" || f.count() != before {
		t.Fatalf("a ledger record went to the zmsg client: %s hits %d", res.JSON(), f.count()-before)
	}
	// A stored audience that is not this client's.
	if _, err := ob.Enqueue("zmsg", "send", "op-a", StoredRequest{Method: "POST", Path: "/v1/send", Audience: "elsewhere", Dedup: "zmsg.dedup/v1", Body: []byte(`{"n":2}`)}); err != nil {
		t.Fatal(err)
	}
	recs, _ = ob.List()
	for _, r := range recs {
		if r.OperationID == "op-a" {
			_ = ob.SetState(r, StateUnknown)
		}
	}
	before = f.count()
	res = valid(t, c.Reconcile(context.Background(), "op-a"))
	if res.Outcome != OutcomeRefused || reason(res) != "origin" || f.count() != before {
		t.Fatalf("%s hits %d", res.JSON(), f.count()-before)
	}
}

func TestACallForAnotherServiceIsRefused(t *testing.T) {
	f := newFake(t, nil)
	r := readReq()
	r.Service = "ledger"
	if res := valid(t, call(f.client(outbox(t)), r)); res.Outcome != OutcomeRefused || reason(res) != "origin" || f.count() != 0 {
		t.Fatalf("%s hits %d", res.JSON(), f.count())
	}
}

// P2-C: a second handle on the same key does not send while the first holds the record.
func TestASecondHandleCannotSendARecordThatIsHeld(t *testing.T) {
	f := newFake(t, nil)
	ob := outbox(t)
	rec, lk, err := ob.EnqueueLocked("zmsg", "send", "op-1", StoredRequest{Method: "POST", Path: "/v1/send", Audience: "aud", Body: []byte(`{"to":"architect","body":"the-private-body"}`)})
	if err != nil {
		t.Fatal(err)
	}
	defer lk.Release()
	_ = rec
	ob2, err := OpenOutbox(ob.dir, "seat-a", nil)
	if err != nil {
		t.Fatal(err)
	}
	res := valid(t, call(f.client(ob2), writeReq("op-1", false)))
	if res.Outcome != OutcomeRefused || reason(res) != "busy" || f.count() != 0 {
		t.Fatalf("%s hits %d", res.JSON(), f.count())
	}
}

// P2-D: spooled is promised only after the record is durably queued again.
func TestSpooledNeedsTheQueuedStateToBeDurable(t *testing.T) {
	syncs := 0
	count := func(failFrom int) (Result, []Record) {
		ob := outbox(t)
		n := 0
		var mu sync.Mutex
		real := ob.syncDir
		ob.syncDir = func(d string) error {
			mu.Lock()
			defer mu.Unlock()
			n++
			if failFrom > 0 && n >= failFrom {
				return errors.New("disk")
			}
			return real(d)
		}
		c := dead(t, ob)
		c.Service = "zmsg"
		res := valid(t, call(c, writeReq("op-1", true)))
		recs, _ := ob.List()
		syncs = n
		return res, recs
	}
	count(0)
	total := syncs
	res, _ := count(total)
	if res.Outcome == OutcomeSpooled {
		t.Fatalf("spooled although the queued state was not durable: %s", res.JSON())
	}
	if res.Outcome != OutcomeRefused || reason(res) != "outbox" {
		t.Fatalf("%s", res.JSON())
	}
}

// P2-E: a reconcile that could not reach the service, or was turned away for the caller, proves nothing.
func TestReconcileEndsARecordOnlyOnADefinitiveAnswer(t *testing.T) {
	for name, tc := range map[string]struct {
		mode func(http.ResponseWriter, *http.Request) bool
		tls  bool
	}{
		"401": {mode: status(401)}, "403": {mode: status(403)}, "tls": {tls: true},
	} {
		f := newFake(t, lostAnswer)
		ob := outbox(t)
		valid(t, call(f.client(ob), writeReq("op-1", true)))
		f.mode = tc.mode
		c := f.client(ob)
		if tc.tls {
			c.Roots = nil
		}
		valid(t, c.Reconcile(context.Background(), "op-1"))
		recs, _ := ob.List()
		if len(recs) != 1 || recs[0].State != StateUnknown {
			t.Errorf("%s ended an unknown record: %v", name, recs)
		}
	}
}

// P2-F: the authorization envelope is stored and sent again.
func TestTheEnvelopeIsStoredAndSentOnADrain(t *testing.T) {
	var mu sync.Mutex
	var seen []string
	f := newFake(t, func(w http.ResponseWriter, r *http.Request) bool {
		mu.Lock()
		seen = append(seen, r.Header.Get("Zeo-Envelope"))
		mu.Unlock()
		return false
	})
	ob := outbox(t)
	req := writeReq("op-1", true)
	req.Envelope = &Envelope{Schema: "zeo.approval/v1", Audience: "aud", Body: []byte("env-body")}
	dc := dead(t, ob)
	if res := valid(t, call(dc, req)); res.Outcome != OutcomeSpooled {
		t.Fatalf("%s", res.JSON())
	}
	c := f.client(ob)
	if n, err := c.Drain(context.Background()); err != nil || n != 1 {
		t.Fatalf("drain %d %v", n, err)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(seen) != 1 || !strings.HasPrefix(seen[0], "zeo.approval/v1 aud ") {
		t.Fatalf("the drained write carried envelope %q", seen)
	}
}

// P3-1/2: abandon and resolve need their evidence; an in-flight record is not abandoned.
func TestAbandonRefusesARecordAWriterHolds(t *testing.T) {
	ob := outbox(t)
	rec, lk, err := ob.EnqueueLocked("zmsg", "send", "op-1", StoredRequest{Method: "POST", Path: "/v1/send", Audience: "aud", Body: []byte(`{"n":1}`)})
	if err != nil {
		t.Fatal(err)
	}
	lk, err = ob.setStateLocked(rec, StateUnknown, lk)
	if err != nil {
		t.Fatal(err)
	}
	if err := ob.Abandon("op-1", "checked the status port", ObservedNotApplied); !errors.Is(err, ErrDrainBusy) {
		t.Fatalf("abandoned a held record: %v", err)
	}
	lk.Release()
	if err := ob.Abandon("op-1", "checked the status port", ObservedNotApplied); err != nil {
		t.Fatalf("%v", err)
	}
	if _, err := ob.Resolve(""); err == nil {
		t.Error("resolve accepted empty evidence")
	}
}

// P3-8: a spooled or dispatch_unknown result names its record.
func TestSpooledAndUnknownResultsNameTheirRecord(t *testing.T) {
	sp := "00000000000000000001-op.json"
	r1, r2 := "unavailable", "dispatch_unknown"
	for _, bad := range []Result{
		{Outcome: OutcomeSpooled},
		{Outcome: OutcomeSpooled, SpoolID: &sp, Sent: true},
		{Outcome: OutcomeDispatchUnknown, Reason: &r2, Sent: true},
		{Outcome: OutcomeDispatchUnknown, Reason: &r2, SpoolID: &sp},
	} {
		if bad.Valid() {
			t.Errorf("valid: %+v", bad)
		}
	}
	_ = r1
	if !(Result{Outcome: OutcomeSpooled, SpoolID: &sp}).Valid() || !(Result{Outcome: OutcomeDispatchUnknown, Reason: &r2, SpoolID: &sp, Sent: true}).Valid() {
		t.Error("a well-formed result was refused")
	}
}

// P2-G: no exported variable lets an importer turn a release build into a dev build.
func TestResolveReadsNothingInAReleaseBuild(t *testing.T) {
	old := devBuildNow
	devBuildNow = false
	t.Cleanup(func() { devBuildNow = old })
	asked := false
	_, err := Resolve("zmsg", func(string) (string, bool) { asked = true; return "", false }, nil)
	assertRefusal(t, err, "not_configured")
	if asked {
		t.Error("a release build read the environment")
	}
	_ = os.Stdout
	_ = filepath.Join
	_ = time.Second
}

// P2-E: a sent not_found answer to the reconcile is definitive about the key.
func TestReconcileEndsARecordOnADefiniteNotFound(t *testing.T) {
	f := newFake(t, lostAnswer)
	ob := outbox(t)
	valid(t, call(f.client(ob), writeReq("op-1", true)))
	f.mode = status(404)
	res := valid(t, f.client(ob).Reconcile(context.Background(), "op-1"))
	recs, _ := ob.List()
	if res.Outcome != OutcomeNotFound || len(recs) != 1 || recs[0].State != StateRefused {
		t.Fatalf("%s %v", res.JSON(), recs)
	}
}
