package client

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/zeroemployeeorg/zeocore/kit"
)

func req(body string) StoredRequest {
	return StoredRequest{Method: "POST", Path: "/v1/send", Audience: "aud", Dedup: "zmsg.dedup/v1", Body: json.RawMessage(body)}
}

func enqueue(t *testing.T, ob *Outbox, id string) Record {
	t.Helper()
	r, err := ob.Enqueue("zmsg", "send", id, req(`{"n":"`+id+`"}`))
	if err != nil {
		t.Fatal(err)
	}
	return r
}

// O1: the same key and payload is one record.
func TestO1SameKeyIsOneRecord(t *testing.T) {
	ob := outbox(t)
	a, _ := ob.Enqueue("zmsg", "reply", "reply-msg-9", req(`{"x":1}`))
	b, err := ob.Enqueue("zmsg", "reply", "reply-msg-9", req(`{"x":1}`))
	recs, _ := ob.List()
	if err != nil || a.Seq != b.Seq || len(recs) != 1 {
		t.Fatalf("%v %v %v", a, err, recs)
	}
}

// O2: the same key with another payload is a conflict.
func TestO2SameKeyDifferentPayload(t *testing.T) {
	ob := outbox(t)
	enqueue(t, ob, "op-1")
	if _, err := ob.Enqueue("zmsg", "send", "op-1", req(`{"n":"other"}`)); !errors.Is(err, ErrKeyConflict) {
		t.Fatalf("%v", err)
	}
	f := newFake(t, nil)
	r := writeReq("op-1", false)
	r.Body = []byte(`{"to":"someone","body":"else"}`)
	if res := valid(t, call(f.client(ob), r)); reason(res) != "key_conflict" || f.count() != 0 {
		t.Fatalf("%s", res.JSON())
	}
}

// O3: concurrent writers never share a sequence number or lose a record.
func TestO3ConcurrentWriters(t *testing.T) {
	ob := outbox(t)
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			if _, err := ob.Enqueue("zmsg", "send", "op-"+string(rune('a'+i)), req(`{"i":`+string(rune('0'+i%10))+`}`)); err != nil {
				t.Error(err)
			}
		}(i)
	}
	wg.Wait()
	recs, _ := ob.List()
	seen := map[uint64]bool{}
	for _, r := range recs {
		if seen[r.Seq] {
			t.Fatalf("sequence %d used twice", r.Seq)
		}
		seen[r.Seq] = true
	}
	if len(recs) != 20 {
		t.Fatalf("%d records", len(recs))
	}
}

// O4: only one drain runs.
func TestO4OneDrainAtATime(t *testing.T) {
	ob := outbox(t)
	enqueue(t, ob, "op-1")
	in, release := make(chan struct{}), make(chan struct{})
	done := make(chan struct{})
	go func() {
		defer close(done)
		_, _ = ob.Drain(func(Record) Result { close(in); <-release; return Result{Outcome: OutcomeOK} })
	}()
	<-in
	if _, err := ob.Drain(func(Record) Result { return Result{Outcome: OutcomeOK} }); !errors.Is(err, ErrDrainBusy) {
		t.Fatalf("%v", err)
	}
	close(release)
	<-done
}

// O5: a call or drain interrupted after the service accepted leaves the record
// unknown: no drain resends it, reconcile does, with the same key; one effect.
func TestO5InterruptedAfterAcceptIsUnknownAndReconciled(t *testing.T) {
	f := newFake(t, nil)
	ob := outbox(t)
	rec := enqueue(t, ob, "op-1")
	c := f.client(ob)
	// The crash: unknown was synced, the service accepted, Remove never ran.
	if err := ob.SetState(rec, StateUnknown); err != nil {
		t.Fatal(err)
	}
	if res := c.sendRecord(context.Background(), rec, secret); res.Outcome != OutcomeOK {
		t.Fatalf("%s", res.JSON())
	}
	if n, err := c.Drain(context.Background()); err != nil || n != 0 || f.seen["op-1"] != 1 {
		t.Fatalf("a drain resent an unknown record: %d %v seen %v", n, err, f.seen)
	}
	res := valid(t, c.Reconcile(context.Background(), "op-1"))
	left, _ := ob.List()
	if res.Outcome != OutcomeOK || len(left) != 0 || f.seen["op-1"] != 2 {
		t.Fatalf("%s left %v seen %v (the service deduplicates by the key)", res.JSON(), left, f.seen)
	}
}

// O14 to O16: the crash boundaries.
func TestO14ToO16CrashBoundaries(t *testing.T) {
	// O14: a crash before unknown is synced: the record is queued, nothing was
	// emitted, and the next drain sends it.
	f := newFake(t, nil)
	ob := outbox(t)
	enqueue(t, ob, "op-1")
	if n, _ := f.client(ob).Drain(context.Background()); n != 1 || f.seen["op-1"] != 1 {
		t.Fatalf("O14: %d %v", n, f.seen)
	}
	// O15: a crash after the unknown sync and before emission: unknown, not drained.
	ob2 := outbox(t)
	rec := enqueue(t, ob2, "op-2")
	ob2.SetState(rec, StateUnknown)
	hits := f.count()
	if n, _ := f.client(ob2).Drain(context.Background()); n != 0 || f.count() != hits {
		t.Fatalf("O15: %d", n)
	}
	// O16: a drain's send marks unknown before the send runs.
	ob3 := outbox(t)
	enqueue(t, ob3, "op-3")
	var during string
	ob3.Drain(func(r Record) Result {
		recs, _ := ob3.List()
		during = recs[0].State
		return Result{Outcome: OutcomeDispatchUnknown, Sent: true}
	})
	if during != StateUnknown {
		t.Fatalf("O16: the record was %q while the send ran", during)
	}
	// A failure that proves nothing was sent puts it back to queued.
	ob4 := outbox(t)
	enqueue(t, ob4, "op-4")
	ob4.Drain(func(Record) Result { return Result{Outcome: OutcomeUnavailable} })
	if recs, _ := ob4.List(); recs[0].State != StateQueued {
		t.Fatalf("a send that sent nothing should leave the record queued: %v", recs[0].State)
	}
}

// O6: an unknown record is never drained, blocks the sender's later records,
// and is resent only by reconcile, with the same key, for a dedup verb.
func TestO6UnknownIsHeldAndReconciled(t *testing.T) {
	f := newFake(t, lostAnswer)
	ob := outbox(t)
	valid(t, call(f.client(ob), writeReq("op-1", true)))
	enqueue(t, ob, "op-2")
	f.mode = nil
	c := f.client(ob)
	hits := f.count()
	if n, _ := c.Drain(context.Background()); n != 0 || f.count() != hits {
		t.Fatalf("a drain went past an unknown record: %d, hits %d -> %d", n, hits, f.count())
	}
	res := valid(t, c.Reconcile(context.Background(), "op-1"))
	if res.Outcome != OutcomeOK || f.seen["op-1"] != 1 {
		t.Fatalf("%s seen %v", res.JSON(), f.seen)
	}
	left, _ := ob.List()
	if len(left) != 1 || left[0].OperationID != "op-2" {
		t.Fatalf("%v", left)
	}
	// A verb without a dedup declaration is held, never resent.
	g := newFake(t, lostAnswer)
	ob2 := outbox(t)
	w := writeReq("op-9", false)
	valid(t, call(g.client(ob2), w))
	g.mode = nil
	if r := valid(t, g.client(ob2).Reconcile(context.Background(), "op-9")); r.Outcome != OutcomeRefused || reason(r) != "not_spoolable" || g.count() != 1 {
		t.Fatalf("%s hits %d", r.JSON(), g.count())
	}
	if r := valid(t, c.Reconcile(context.Background(), "nope")); r.Outcome != OutcomeNotFound {
		t.Fatalf("%s", r.JSON())
	}
	if r := valid(t, (&Client{}).Reconcile(context.Background(), "x")); reason(r) != "no_outbox" {
		t.Fatalf("%s", r.JSON())
	}
}

// O7: a refused record is terminal until purged, and does not stop later ones.
func TestO7RefusedIsTerminalAndPurged(t *testing.T) {
	ob := outbox(t)
	enqueue(t, ob, "op-1")
	enqueue(t, ob, "op-2")
	n, _ := ob.Drain(func(r Record) Result {
		if r.OperationID == "op-1" {
			return Result{Outcome: OutcomeRefused, Sent: true}
		}
		return Result{Outcome: OutcomeOK, Sent: true}
	})
	left, _ := ob.List()
	if n != 1 || len(left) != 1 || left[0].State != StateRefused {
		t.Fatalf("%d %v", n, left)
	}
	if n, _ := ob.Drain(func(Record) Result { t.Fatal("a refused record was sent"); return Result{} }); n != 0 {
		t.Fatal(n)
	}
	if k, err := ob.Purge(); err != nil || k != 1 {
		t.Fatalf("%d %v", k, err)
	}
	if left, _ := ob.List(); len(left) != 0 {
		t.Fatal(left)
	}
}

func TestADrainKeepsOrderAndStopsOnAFailure(t *testing.T) {
	ob := outbox(t)
	for _, id := range []string{"op-1", "op-2", "op-3"} {
		enqueue(t, ob, id)
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

func TestADrainMarksAnUnknownDispatchAndStops(t *testing.T) {
	ob := outbox(t)
	enqueue(t, ob, "op-1")
	enqueue(t, ob, "op-2")
	n, _ := ob.Drain(func(Record) Result { return Result{Outcome: OutcomeDispatchUnknown, Sent: true} })
	left, _ := ob.List()
	if n != 0 || len(left) != 2 || left[0].State != StateUnknown || left[1].State != StateQueued {
		t.Fatalf("n=%d left=%v", n, left)
	}
}

// A spooled record is drained by a later successful call to the same service.
func TestALaterSuccessDrainsTheSpool(t *testing.T) {
	f := newFake(t, nil)
	ob := outbox(t)
	enqueue(t, ob, "op-1")
	if res := valid(t, call(f.client(ob), readReq())); res.Outcome != OutcomeOK {
		t.Fatalf("%s", res.JSON())
	}
	if left, _ := ob.List(); len(left) != 0 || f.seen["op-1"] != 1 {
		t.Fatalf("%v %v", left, f.seen)
	}
}

// O8: a full outbox refuses and evicts nothing; every state counts.
func TestO8FullOutbox(t *testing.T) {
	ob := outbox(t)
	ob.maxRecords = 2
	c := dead(t, ob)
	for _, id := range []string{"op-1", "op-2"} {
		if r := valid(t, call(c, writeReq(id, true))); r.Outcome != OutcomeSpooled {
			t.Fatalf("%s", r.JSON())
		}
	}
	res := valid(t, call(c, writeReq("op-3", true)))
	recs, _ := ob.List()
	if res.Outcome != OutcomeRefused || reason(res) != "outbox_full" || len(recs) != 2 {
		t.Fatalf("%s %v", res.JSON(), recs)
	}
	ob.SetState(recs[0], StateUnknown)
	if _, err := ob.Enqueue("zmsg", "send", "op-4", req(`{}`)); !errors.Is(err, ErrFull) {
		t.Fatal("an unknown record must still count")
	}
	ob2 := outbox(t)
	ob2.maxBytes = 10
	if _, err := ob2.Enqueue("zmsg", "send", "op-1", req(`{"a":"0123456789"}`)); !errors.Is(err, ErrFull) {
		t.Fatalf("%v", err)
	}
}

// O9: a failed fsync is durability unknown: the write refuses and the file is
// kept aside as .suspect (not deleted). It was never sent, so it is terminal and
// does NOT hold the order; resolve may only discard it.
func TestO9FailedFsync(t *testing.T) {
	for _, which := range []string{"file", "dir"} {
		ob := outbox(t)
		if which == "file" {
			ob.syncFile = func(*os.File) error { return errors.New("disk") }
		} else {
			ob.syncDir = func(string) error { return errors.New("disk") }
		}
		if _, err := ob.Enqueue("zmsg", "send", "op-1", req(`{}`)); !errors.Is(err, ErrDisk) {
			t.Fatalf("%s: %v", which, err)
		}
		ents, _ := os.ReadDir(ob.dir)
		suspect := 0
		for _, e := range ents {
			if strings.HasSuffix(e.Name(), ".suspect") {
				suspect++
			}
			if strings.HasPrefix(e.Name(), ".tmp-") && !strings.HasSuffix(e.Name(), ".suspect") {
				t.Fatalf("%s: an unmarked temporary file: %s", which, e.Name())
			}
		}
		if suspect != 1 || ob.Held() {
			t.Fatalf("%s: suspect %d held %v", which, suspect, ob.Held())
		}
		ob.syncFile = func(f *os.File) error { return f.Sync() }
		ob.syncDir = func(string) error { return nil }
		if _, err := ob.Enqueue("zmsg", "send", "op-2", req(`{}`)); err != nil {
			t.Fatalf("%s: a suspect file must not hold the order: %v", which, err)
		}
		if st, _ := ob.Stats(); st.Aside != 1 {
			t.Fatalf("%s: doctor should list the file set aside: %+v", which, st)
		}
		if n, err := ob.Resolve("a person looked"); err != nil || n != 1 {
			t.Fatalf("%s: resolve %d %v", which, n, err)
		}
		if recs, _ := ob.List(); len(recs) != 1 {
			t.Fatalf("%s: resolve must never requeue a suspect file: %v", which, recs)
		}
		if ents, _ := os.ReadDir(ob.dir); !hasSuffixFile(ents, ".discarded") {
			t.Fatalf("%s: a discarded file should be kept renamed", which)
		}
	}
	ob := outbox(t)
	ob.syncFile = func(*os.File) error { return errors.New("disk") }
	f := newFake(t, nil)
	if res := valid(t, call(f.client(ob), writeReq("op-1", false))); reason(res) != "outbox" || f.count() != 0 {
		t.Fatalf("a write that cannot be retained must not be sent: %s hits %d", res.JSON(), f.count())
	}
}

func hasSuffixFile(ents []os.DirEntry, suffix string) bool {
	for _, e := range ents {
		if strings.HasSuffix(e.Name(), suffix) {
			return true
		}
	}
	return false
}

// O10: a torn record is moved aside, shown by doctor, never read, and HOLDS the
// sender's order: later records and new writes wait; the ordinal is not skipped.
func TestO10TornRecordHoldsTheOrder(t *testing.T) {
	ob := outbox(t)
	enqueue(t, ob, "op-1")
	torn := filepath.Join(ob.dir, fileName(7, "op-7"))
	if err := os.WriteFile(torn, []byte(`{"schema":"zeo.outbox-record/v1","seq":`), 0o600); err != nil {
		t.Fatal(err)
	}
	recs, _ := ob.List()
	s, _ := ob.Stats()
	if len(recs) != 1 || s.Bad != 1 || !ob.Held() {
		t.Fatalf("%v %+v", recs, s)
	}
	if _, err := os.Stat(torn + ".bad"); err != nil {
		t.Fatal("the torn file should be moved aside")
	}
	f := newFake(t, nil)
	c := f.client(ob)
	if n, err := c.Drain(context.Background()); !errors.Is(err, ErrHeld) || n != 0 || f.count() != 0 {
		t.Fatalf("a held outbox drained: %d %v", n, err)
	}
	if res := valid(t, call(c, writeReq("op-new", false))); reason(res) != "blocked" || f.count() != 0 {
		t.Fatalf("%s", res.JSON())
	}
	if n, err := ob.Resolve("a person looked"); err != nil || n != 1 || ob.Held() {
		t.Fatalf("%d %v", n, err)
	}
	if n, _ := c.Drain(context.Background()); n != 1 {
		t.Fatalf("after resolve the queue drains: %d", n)
	}
	// O17: a temporary file left by a crash is kept aside at open, terminal, and
	// does not hold the order.
	tmp := filepath.Join(ob.dir, ".tmp-crash")
	os.WriteFile(tmp, []byte("x"), 0o600)
	ob2, err := OpenOutbox(ob.dir, "seat-a", nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(tmp + ".suspect"); err != nil || ob2.Held() {
		t.Fatalf("the crash temporary file should be kept aside and not hold: %v held %v", err, ob2.Held())
	}
	// A record from another incarnation is not read.
	other := outbox(t)
	enqueue(t, other, "op-1")
	raw, _ := os.ReadFile(filepath.Join(other.dir, fileName(1, "op-1")))
	ob3 := outbox(t)
	os.WriteFile(filepath.Join(ob3.dir, fileName(1, "op-1")), raw, 0o600)
	if recs, _ := ob3.List(); len(recs) != 0 {
		t.Fatalf("%v", recs)
	}
}

// O11: a path-injecting id is refused.
func TestO11PathInjectingIDs(t *testing.T) {
	ob := outbox(t)
	for _, id := range []string{"../x", "a/b", "a\x00b", "", "a b", strings.Repeat("a", 65), "x.json"} {
		if _, err := ob.Enqueue("zmsg", "send", id, req(`{}`)); err == nil || !strings.Contains(err.Error(), "operation id") {
			t.Errorf("%q should be refused as an operation id, got %v", id, err)
		}
	}
	if ents, _ := os.ReadDir(ob.dir); len(ents) > 3 {
		t.Errorf("a refused id left files: %v", ents)
	}
}

// O12: the directory is checked.
func TestO12TheDirectoryIsChecked(t *testing.T) {
	base, _ := filepath.EvalSymlinks(t.TempDir())
	if _, err := OpenOutbox("relative/dir", "s", nil); err == nil {
		t.Fatal("a relative path should refuse")
	}
	if _, err := OpenOutbox(base+"/a/../b", "s", nil); err == nil {
		t.Fatal("an unclean path should refuse")
	}
	os.Mkdir(filepath.Join(base, "real"), 0o700)
	os.Symlink(filepath.Join(base, "real"), filepath.Join(base, "link"))
	if _, err := OpenOutbox(filepath.Join(base, "link", "ob"), "s", nil); err == nil {
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

// O13: a record file is mode 0600, canonical, and carries the digest.
func TestO13RecordsAreCanonicalPrivateFiles(t *testing.T) {
	ob := outbox(t)
	rec, err := ob.Enqueue("zmsg", "send", "op-1", req(`{"b":2,"a":1}`))
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(ob.dir, fileName(rec.Seq, rec.OperationID))
	st, _ := os.Stat(path)
	raw, _ := os.ReadFile(path)
	if st.Mode().Perm() != 0o600 || !strings.Contains(string(raw), `"body":{"a":1,"b":2}`) || !strings.HasPrefix(rec.PayloadDigest, "sha256:") ||
		rec.Incarnation == "" || rec.State != StateQueued {
		t.Fatalf("mode %v doc %s", st.Mode(), raw)
	}
	if _, err := ob.Enqueue("zmsg", "send", "op-2", req(`not json`)); err == nil {
		t.Fatal("a non-JSON body should refuse")
	}
	if rec2, _ := OpenOutbox(ob.dir, "seat-a", nil); rec2.incarnation != ob.incarnation {
		t.Fatal("the incarnation changed on reopen")
	}
}

func TestStatsForDoctor(t *testing.T) {
	ob := outbox(t)
	ob.maxRecords = 10
	for i := 0; i < 9; i++ {
		enqueue(t, ob, "op-"+string(rune('a'+i)))
	}
	recs, _ := ob.List()
	ob.SetState(recs[0], StateUnknown)
	ob.now = func() time.Time { return time.Date(2026, 10, 17, 3, 0, 30, 0, time.UTC) }
	s, err := ob.Stats()
	if err != nil || s.Count != 9 || s.Unknown != 1 || s.Queued != 8 || !s.NearFull || s.OldestAge != 30*time.Second {
		t.Fatalf("%+v %v", s, err)
	}
}

// A read that returns while the spool holds another service's records leaves them.
func TestADrainAfterIsPerService(t *testing.T) {
	f := newFake(t, nil)
	ob := outbox(t)
	if _, err := ob.Enqueue("other", "send", "op-1", req(`{}`)); err != nil {
		t.Fatal(err)
	}
	call(f.client(ob), readReq())
	if left, _ := ob.List(); len(left) != 1 {
		t.Fatal("another service's record was drained")
	}
}

var _ = http.StatusOK
var _ = kit.ExitOK

// A call whose key already has a refused record is refused, and not sent.
func TestAKeyWithARefusedRecordIsNotSent(t *testing.T) {
	f := newFake(t, nil)
	ob := outbox(t)
	rec := enqueue(t, ob, "op-1")
	r := writeReq("op-1", false)
	r.Body = []byte(`{"n":"op-1"}`)
	// Make the stored request equal the call's, then refuse the record.
	sr := StoredRequest{Method: "POST", Path: "/v1/send", Audience: "aud", Dedup: "", Body: json.RawMessage(`{"n":"op-1"}`)}
	ob.Remove(rec)
	rec, err := ob.Enqueue("zmsg", "send", "op-1", sr)
	if err != nil {
		t.Fatal(err)
	}
	ob.SetState(rec, StateRefused)
	res := valid(t, call(f.client(ob), r))
	if res.Outcome != OutcomeRefused || reason(res) != "outbox" || f.count() != 0 {
		t.Fatalf("%s hits %d", res.JSON(), f.count())
	}
}

// Reconcile marks a record the service refused.
func TestReconcileMarksARefusal(t *testing.T) {
	f := newFake(t, lostAnswer)
	ob := outbox(t)
	valid(t, call(f.client(ob), writeReq("op-1", true)))
	f.mode = status(401)
	res := valid(t, f.client(ob).Reconcile(context.Background(), "op-1"))
	recs, _ := ob.List()
	if res.Outcome != OutcomeRefused || len(recs) != 1 || recs[0].State != StateRefused {
		t.Fatalf("%s %v", res.JSON(), recs)
	}
}

// A successful write drains this service's earlier spool.
func TestASuccessfulWriteDrainsTheSpool(t *testing.T) {
	f := newFake(t, nil)
	ob := outbox(t)
	enqueue(t, ob, "op-old")
	if res := valid(t, call(f.client(ob), writeReq("op-new", false))); res.Outcome != OutcomeOK {
		t.Fatalf("%s", res.JSON())
	}
	if left, _ := ob.List(); len(left) != 0 || f.seen["op-old"] != 1 {
		t.Fatalf("%v %v", left, f.seen)
	}
}

// A refused record does not stop a later queued one; purge keeps queued ones.
func TestARefusedRecordDoesNotBlockAndPurgeKeepsQueued(t *testing.T) {
	ob := outbox(t)
	enqueue(t, ob, "op-1")
	ob.SetState(mustFirst(t, ob), StateRefused)
	enqueue(t, ob, "op-2")
	sent := []string{}
	n, _ := ob.Drain(func(r Record) Result { sent = append(sent, r.OperationID); return Result{Outcome: OutcomeOK} })
	if n != 1 || len(sent) != 1 || sent[0] != "op-2" {
		t.Fatalf("%d %v", n, sent)
	}
	enqueue(t, ob, "op-3")
	if k, _ := ob.Purge(); k != 1 {
		t.Fatalf("purged %d", k)
	}
	if left, _ := ob.List(); len(left) != 1 || left[0].OperationID != "op-3" {
		t.Fatalf("%v", left)
	}
}

func mustFirst(t *testing.T, ob *Outbox) Record {
	recs, err := ob.List()
	if err != nil || len(recs) == 0 {
		t.Fatal(err)
	}
	return recs[0]
}

// A record under the wrong file name, or a bad incarnation file, is not trusted.
func TestAWronglyNamedRecordAndABadIncarnation(t *testing.T) {
	ob := outbox(t)
	enqueue(t, ob, "op-1")
	raw, _ := os.ReadFile(filepath.Join(ob.dir, fileName(1, "op-1")))
	os.WriteFile(filepath.Join(ob.dir, fileName(5, "op-1")), raw, 0o600)
	recs, _ := ob.List()
	if len(recs) != 1 {
		t.Fatalf("%v", recs)
	}
	os.WriteFile(filepath.Join(ob.dir, "incarnation"), []byte("short\n"), 0o600)
	if _, err := OpenOutbox(ob.dir, "seat-a", nil); err == nil {
		t.Fatal("a bad incarnation should refuse")
	}
}

// Reconcile refuses a record that is not unknown.
func TestReconcileRefusesAQueuedRecord(t *testing.T) {
	f := newFake(t, nil)
	ob := outbox(t)
	enqueue(t, ob, "op-1")
	res := valid(t, f.client(ob).Reconcile(context.Background(), "op-1"))
	if res.Outcome != OutcomeRefused || f.count() != 0 {
		t.Fatalf("%s hits %d", res.JSON(), f.count())
	}
}

// C20 to C22: a new write never overtakes what is ahead of it.
func TestC20ToC22NoOvertaking(t *testing.T) {
	// C20: queued records ahead are drained first; the new write then goes.
	f := newFake(t, nil)
	ob := outbox(t)
	enqueue(t, ob, "op-old")
	if res := valid(t, call(f.client(ob), writeReq("op-new", true))); res.Outcome != OutcomeOK || f.seen["op-old"] != 1 || f.seen["op-new"] != 1 {
		t.Fatalf("%s %v", res.JSON(), f.seen)
	}
	// C20b: if the queue cannot be drained, the new write is queued behind (it may
	// spool) or refused blocked (it may not).
	g := dead(t, outbox(t))
	enqueue(t, g.Outbox, "op-old")
	res := valid(t, call(g, writeReq("op-new", true)))
	if res.Outcome != OutcomeSpooled {
		t.Fatalf("queued behind: %s", res.JSON())
	}
	recs, _ := g.Outbox.List()
	if len(recs) != 2 || recs[0].OperationID != "op-old" || recs[1].OperationID != "op-new" {
		t.Fatalf("%v", recs)
	}
	res = valid(t, call(g, writeReq("op-other", false)))
	if res.Outcome != OutcomeRefused || reason(res) != "blocked" {
		t.Fatalf("%s", res.JSON())
	}
	if recs, _ := g.Outbox.List(); len(recs) != 2 {
		t.Fatalf("a refused write left a record: %v", recs)
	}
	// C21: --require-delivery with records ahead is refused blocked.
	r := writeReq("op-rd", true)
	r.RequireDelivery = true
	if res := valid(t, call(g, r)); reason(res) != "blocked" {
		t.Fatalf("%s", res.JSON())
	}
	// C22: an unknown record ahead blocks every later write.
	h := newFake(t, lostAnswer)
	obh := outbox(t)
	valid(t, call(h.client(obh), writeReq("op-1", true)))
	h.mode = nil
	hits := h.count()
	if res := valid(t, call(h.client(obh), writeReq("op-2", false))); reason(res) != "blocked" || h.count() != hits {
		t.Fatalf("%s hits %d -> %d", res.JSON(), hits, h.count())
	}
}

// O18: a derived reply key spooled twice, with the outbox recreated between,
// is the same key at the service.
func TestO18DerivedKeysSurviveANewIncarnation(t *testing.T) {
	if ReplyKey("msg-9") != "reply-msg-9" || !ValidOperationID(ReplyKey("msg-9")) {
		t.Fatal(ReplyKey("msg-9"))
	}
	f := newFake(t, nil)
	r := writeReq(ReplyKey("msg-9"), false)
	one := f.client(outbox(t))
	valid(t, call(one, r))
	two := f.client(outbox(t)) // a different directory, a different incarnation
	valid(t, call(two, r))
	if f.seen["reply-msg-9"] != 2 {
		t.Fatalf("the service should see the same key twice: %v", f.seen)
	}
	if one.Outbox.incarnation == two.Outbox.incarnation {
		t.Fatal("the incarnations should differ")
	}
}

// O19: the implicit drain changes neither the result nor the exit of the call.
func TestO19ImplicitDrainDoesNotChangeTheCall(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, r *http.Request) bool {
		if r.Header.Get("Idempotency-Key") == "op-old" {
			w.WriteHeader(500)
			return true
		}
		return false
	})
	ob := outbox(t)
	enqueue(t, ob, "op-old")
	res := valid(t, call(f.client(ob), readReq()))
	recs, _ := ob.List()
	if res.Outcome != OutcomeOK || res.Exit() != kit.ExitOK || len(recs) != 1 || recs[0].State != StateUnknown {
		t.Fatalf("%s %v", res.JSON(), recs)
	}
}

// While a write is on the wire its record is already unknown, durably.
func TestTheRecordIsUnknownWhileTheWriteIsInFlight(t *testing.T) {
	ob := outbox(t)
	var during string
	f := newFake(t, func(w http.ResponseWriter, r *http.Request) bool {
		recs, _ := ob.List()
		if len(recs) == 1 {
			during = recs[0].State
		}
		return false
	})
	if res := valid(t, call(f.client(ob), writeReq("op-1", false))); res.Outcome != OutcomeOK {
		t.Fatalf("%s", res.JSON())
	}
	if during != StateUnknown {
		t.Fatalf("the record was %q while the request was in flight", during)
	}
	if left, _ := ob.List(); len(left) != 0 {
		t.Fatalf("%v", left)
	}
}

// The implicit drain has its own deadline: it is not cut short by what is left
// of the triggering call's.
func TestTheImplicitDrainHasItsOwnDeadline(t *testing.T) {
	old := Read
	t.Cleanup(func() { Read = old })
	Read = Policy{Connect: time.Second, Total: 120 * time.Millisecond}
	f := newFake(t, func(w http.ResponseWriter, r *http.Request) bool {
		if r.Header.Get("Idempotency-Key") == "op-old" {
			time.Sleep(250 * time.Millisecond)
		}
		return false
	})
	ob := outbox(t)
	enqueue(t, ob, "op-old")
	valid(t, call(f.client(ob), readReq()))
	if left, _ := ob.List(); len(left) != 0 {
		t.Fatalf("the drain was cut short by the read's deadline: %v", left)
	}
}

// Records of another service neither block a write nor are sent by this client.
func TestRecordsOfAnotherServiceNeitherBlockNorAreSent(t *testing.T) {
	f := newFake(t, nil)
	ob := outbox(t)
	if _, err := ob.Enqueue("other", "send", "op-x", req(`{}`)); err != nil {
		t.Fatal(err)
	}
	dead := f.client(ob)
	res := valid(t, call(dead, writeReq("op-1", false)))
	left, _ := ob.List()
	if res.Outcome != OutcomeOK || f.seen["op-x"] != 0 || len(left) != 1 || left[0].OperationID != "op-x" {
		t.Fatalf("%s %v %v", res.JSON(), left, f.seen)
	}
	if n, _ := f.client(ob).Drain(context.Background()); n != 0 || f.seen["op-x"] != 0 {
		t.Fatalf("this client drained another service's record: %d %v", n, f.seen)
	}
}

func TestDoctorShowsAHold(t *testing.T) {
	f := newFake(t, nil)
	ob := outbox(t)
	os.WriteFile(filepath.Join(ob.dir, fileName(3, "op-3")), []byte("{"), 0o600)
	d := f.client(ob).Doctor(context.Background(), "zmsg", "/v1/health", "aud")
	if !d.Held || d.Outbox == nil || d.Outbox.Bad != 1 {
		t.Fatalf("%+v", d)
	}
}

func nudgeReq(id string) Request {
	return Request{Service: "zmsg", Verb: "nudge", Method: "POST", Path: "/v1/nudge", Audience: "aud", OperationID: id, Body: []byte(`{"seat":"a"}`)}
}

func stopReq(id string) Request {
	r := nudgeReq(id)
	r.Verb, r.Path, r.Unordered = "stop", "/v1/stop", true
	return r
}

// C24: a non-dedup write that sent nothing deletes its record, with a directory fsync.
func TestC24NonDedupWriteLeavesNoRecord(t *testing.T) {
	ob := outbox(t)
	dirs := 0
	ob.syncDir = func(string) error { dirs++; return nil }
	res := valid(t, call(dead(t, ob), nudgeReq("op-1")))
	recs, _ := ob.List()
	before := dirs
	if res.Outcome != OutcomeUnavailable || reason(res) != "offline" || res.Exit() != kit.ExitUnavailable || len(recs) != 0 {
		t.Fatalf("%s %v", res.JSON(), recs)
	}
	if before < 3 { // enqueue, unknown, delete
		t.Fatalf("the delete was not made durable: %d directory syncs", before)
	}
}

// C25: a leftover queued record of a non-dedup verb is set aside at open: never drained, not holding.
func TestC25StaleQueuedNonDedupRecord(t *testing.T) {
	ob := outbox(t)
	if _, err := ob.Enqueue("zmsg", "nudge", "op-1", StoredRequest{Method: "POST", Path: "/v1/nudge", Audience: "aud", Body: json.RawMessage(`{}`)}); err != nil {
		t.Fatal(err)
	}
	enqueue(t, ob, "op-2") // a dedup record
	ob2, err := OpenOutbox(ob.dir, "seat-a", nil)
	if err != nil {
		t.Fatal(err)
	}
	recs, _ := ob2.List()
	ents, _ := os.ReadDir(ob.dir)
	if len(recs) != 1 || recs[0].OperationID != "op-2" || !hasSuffixFile(ents, ".stale") || ob2.Held() {
		t.Fatalf("%v", recs)
	}
	if st, _ := ob2.Stats(); st.Aside != 1 {
		t.Fatalf("%+v", st)
	}
	f := newFake(t, nil)
	if n, _ := f.client(ob2).Drain(context.Background()); n != 1 || f.seen["op-1"] != 0 {
		t.Fatalf("the stale record was drained: %d %v", n, f.seen)
	}
}

// C26: a non-dedup record left unknown is never resent; abandon ends it, keeping it.
func TestC26AbandonEndsAnUnknownNonDedupRecord(t *testing.T) {
	f := newFake(t, lostAnswer)
	ob := outbox(t)
	res := valid(t, call(f.client(ob), nudgeReq("op-1")))
	if res.Outcome != OutcomeDispatchUnknown {
		t.Fatalf("%s", res.JSON())
	}
	f.mode = nil
	c := f.client(ob)
	hits := f.count()
	if r := valid(t, c.Reconcile(context.Background(), "op-1")); r.Outcome != OutcomeRefused || reason(r) != "not_spoolable" || f.count() != hits {
		t.Fatalf("reconcile of a non-dedup verb: %s", r.JSON())
	}
	if n, _ := c.Drain(context.Background()); n != 0 || f.count() != hits {
		t.Fatal("a drain sent a non-dedup record")
	}
	// An ordered write behind it is blocked.
	if r := valid(t, call(c, nudgeReq("op-2"))); reason(r) != "blocked" || f.count() != hits {
		t.Fatalf("%s", r.JSON())
	}
	if err := ob.Abandon("op-1", "status port: not applied", ObservedNotApplied); err != nil {
		t.Fatal(err)
	}
	recs, _ := ob.List()
	if len(recs) != 1 || recs[0].State != StateAbandoned {
		t.Fatalf("the record must be kept: %v", recs)
	}
	if r := valid(t, call(c, nudgeReq("op-2"))); r.Outcome != OutcomeOK {
		t.Fatalf("after abandon the next write goes: %s", r.JSON())
	}
	// The same key again is refused, not resent.
	hits = f.count()
	if r := valid(t, call(c, nudgeReq("op-1"))); r.Outcome != OutcomeRefused || f.count() != hits {
		t.Fatalf("%s", r.JSON())
	}
	if err := ob.Abandon("op-2", "status port: not applied", ObservedNotApplied); err == nil {
		t.Fatal("a record that is not unknown cannot be abandoned")
	}
	if err := ob.Abandon("nope", "status port: not applied", ObservedNotApplied); err == nil {
		t.Fatal("no such record")
	}
	ob2 := outbox(t)
	enqueue(t, ob2, "op-d")
	ob2.SetState(mustFirst(t, ob2), StateUnknown)
	if err := ob2.Abandon("op-d", "x", ObservedNotApplied); err == nil {
		t.Fatal("a dedup record is reconciled, not abandoned")
	}
	if k, _ := ob.Purge(); k != 1 {
		t.Fatalf("purge should remove the abandoned record: %d", k)
	}
}

// C23: an unordered verb is still recorded and sent behind an unknown record, a
// .bad file, and a full ordinary outbox (reserved room).
func TestC23UnorderedVerbIsNeverBlocked(t *testing.T) {
	f := newFake(t, lostAnswer)
	ob := outbox(t)
	valid(t, call(f.client(ob), nudgeReq("op-1"))) // unknown, ordered
	f.mode = nil
	os.WriteFile(filepath.Join(ob.dir, fileName(9, "op-9")), []byte("{"), 0o600) // a .bad file
	c := f.client(ob)
	if r := valid(t, call(c, nudgeReq("op-2"))); reason(r) != "blocked" {
		t.Fatalf("an ordered write should be blocked: %s", r.JSON())
	}
	if r := valid(t, call(c, stopReq("op-stop"))); r.Outcome != OutcomeOK {
		t.Fatalf("a stop must go: %s", r.JSON())
	}
	// reserved room
	ob2 := outbox(t)
	ob2.maxRecords = 1000
	for i := 0; i < 1000-ReservedRecords; i++ {
		ob2.writeRaw(t, i+1)
	}
	if _, err := ob2.Enqueue("zmsg", "send", "op-ord", req(`{}`)); !errors.Is(err, ErrFull) {
		t.Fatalf("an ordinary write should hit the bound before the reserve: %v", err)
	}
	un := req(`{}`)
	un.Unordered = true
	if _, err := ob2.Enqueue("zmsg", "stop", "op-un", un); err != nil {
		t.Fatalf("the reserved room is for unordered verbs: %v", err)
	}
}

func (o *Outbox) writeRaw(t *testing.T, n int) {
	t.Helper()
	rec := Record{Schema: RecordSchema, Sender: o.sender, Incarnation: o.incarnation, Seq: uint64(n), Service: "other", Verb: "v",
		OperationID:   "fill-" + string(rune('a'+n%26)) + "-" + strings.Repeat("x", n%7) + string(rune('a'+(n/26)%26)) + string(rune('a'+(n/676)%26)),
		PayloadDigest: "sha256:0", Request: json.RawMessage(`{"audience":"","body":{},"dedup":"d","method":"POST","path":"/","unordered":false}`),
		Created: "2026-10-17T03:00:00Z", State: StateRefused}
	if err := o.write(rec); err != nil {
		t.Fatal(err)
	}
}

// O20: the owner verbs leave an audit line with who, what, on what evidence.
func TestO20AuditLogAndShow(t *testing.T) {
	var mailed []string
	ob := outbox(t)
	ob.Evidence = func(l string) { mailed = append(mailed, l) }
	f := newFake(t, lostAnswer)
	valid(t, call(f.client(ob), nudgeReq("op-1")))
	if err := ob.Abandon("op-1", "status port: not applied", ObservedNotApplied); err != nil {
		t.Fatal(err)
	}
	rec, entries, err := ob.Show("op-1")
	if err != nil || rec.State != StateAbandoned || rec.Resolution == nil || rec.Resolution.Evidence != "status port: not applied" ||
		rec.Resolution.Observed != ObservedNotApplied || rec.Resolution.Actor == "" {
		t.Fatalf("%+v %v", rec, err)
	}
	if len(entries) != 1 || entries[0].Verb != "abandon" || entries[0].Service != "zmsg" || entries[0].RecordVerb != "nudge" ||
		entries[0].OperationID != "op-1" || entries[0].Evidence == "" || entries[0].Observed != ObservedNotApplied || entries[0].At == "" {
		t.Fatalf("%+v", entries)
	}
	if len(mailed) != 1 || !strings.Contains(mailed[0], `"operation_id":"op-1"`) {
		t.Fatalf("abandon should hand its line to the evidence sink: %v", mailed)
	}
	// resolve and purge audit too; resolve also mails.
	os.WriteFile(filepath.Join(ob.dir, fileName(9, "op-9")), []byte("{"), 0o600)
	ob.List()
	ob.Resolve("looked at the torn file")
	ob.Purge()
	raw, _ := os.ReadFile(filepath.Join(ob.dir, "audit.log"))
	if !strings.Contains(string(raw), `"verb":"purge"`) || !strings.Contains(string(raw), `"verb":"resolve"`) || len(mailed) != 2 {
		t.Fatalf("%q %v", raw, mailed)
	}
	st, _ := os.Stat(filepath.Join(ob.dir, "audit.log"))
	if st.Mode().Perm() != 0o600 {
		t.Fatal(st.Mode())
	}
	if _, _, err := ob.Show("nope"); err == nil {
		t.Fatal("no such record")
	}
	// A reconcile leaves a line too.
	g := newFake(t, lostAnswer)
	obg := outbox(t)
	valid(t, call(g.client(obg), writeReq("op-r", true)))
	g.mode = nil
	g.client(obg).Reconcile(context.Background(), "op-r")
	raw, _ = os.ReadFile(filepath.Join(obg.dir, "audit.log"))
	if !strings.Contains(string(raw), `"verb":"reconcile"`) {
		t.Fatalf("%q", raw)
	}
}

// abandon needs the evidence and a valid observation.
func TestAbandonNeedsEvidence(t *testing.T) {
	f := newFake(t, lostAnswer)
	ob := outbox(t)
	valid(t, call(f.client(ob), nudgeReq("op-1")))
	for _, c := range [][2]string{{"", ObservedApplied}, {"e", ""}, {"e", "maybe"}} {
		if err := ob.Abandon("op-1", c[0], c[1]); err == nil {
			t.Errorf("%q %q should be refused", c[0], c[1])
		}
	}
	if recs, _ := ob.List(); recs[0].State != StateUnknown {
		t.Fatal("a refused abandon must change nothing")
	}
}

// The audit log is rotated once at its bound and counts toward the ordinary bound.
func TestAuditLogRotatesAndCounts(t *testing.T) {
	ob := outbox(t)
	path := filepath.Join(ob.dir, "audit.log")
	os.WriteFile(path, []byte(strings.Repeat("x", MaxAuditBytes)), 0o600)
	ob.audit(AuditEntry{Verb: "purge"})
	if _, err := os.Stat(path + ".1"); err != nil {
		t.Fatal("the full log should have been rotated to audit.log.1")
	}
	if st, _ := os.Stat(path); st.Size() > 1024 {
		t.Fatalf("a fresh log was expected: %d bytes", st.Size())
	}
	n, b := ob.aside()
	if n != 0 || b < MaxAuditBytes {
		t.Fatalf("the audit logs should count toward the bound: %d files, %d bytes", n, b)
	}
}

// O8b: an ordered byte flood, then a stop.
func TestO8bOrderedByteFloodThenStop(t *testing.T) {
	ob := outbox(t)
	big := json.RawMessage(`"` + strings.Repeat("a", 1<<20-200) + `"`)
	filled := 0
	for i := 0; i < 20; i++ {
		_, err := ob.Enqueue("other", "bulk", "flood-"+string(rune('a'+i)), StoredRequest{Method: "POST", Path: "/", Dedup: "d", Body: big})
		if errors.Is(err, ErrFull) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		filled++
	}
	if filled < 6 || filled > 8 {
		t.Fatalf("the ordinary bound should stop the flood at about 8 MiB: %d records", filled)
	}
	un := StoredRequest{Method: "POST", Path: "/v1/stop", Audience: "aud", Unordered: true, Body: json.RawMessage(`{"seat":"a"}`)}
	if _, err := ob.Enqueue("zmsg", "stop", "op-stop", un); err != nil {
		t.Fatalf("a stop must still be recorded: %v", err)
	}
	un.Body = json.RawMessage(`"` + strings.Repeat("a", MaxUnorderedBody) + `"`)
	if _, err := ob.Enqueue("zmsg", "stop", "op-stop2", un); !errors.Is(err, ErrTooBig) {
		t.Fatalf("a stop over its cap: %v", err)
	}
	f := newFake(t, nil)
	c := f.client(ob)
	r := stopReq("op-big")
	r.Body = []byte(`"` + strings.Repeat("a", MaxUnorderedBody) + `"`)
	if res := valid(t, call(c, r)); res.Outcome != OutcomeInvalid {
		t.Fatalf("%s", res.JSON())
	}
}

// Set-aside files count toward the ordinary bound, never the reserve.
func TestSetAsideFilesCountTowardTheOrdinaryBound(t *testing.T) {
	ob := outbox(t)
	ob.maxRecords = 100
	for i := 0; i < 99; i++ {
		os.WriteFile(filepath.Join(ob.dir, ".tmp-x"+string(rune('a'+i%26))+string(rune('a'+i/26))+".suspect"), []byte("x"), 0o600)
	}
	if _, err := ob.Enqueue("zmsg", "send", "op-1", req(`{}`)); !errors.Is(err, ErrFull) {
		t.Fatalf("set-aside files should fill the ordinary bound: %v", err)
	}
	un := req(`{}`)
	un.Unordered = true
	if _, err := ob.Enqueue("zmsg", "stop", "op-s", un); err != nil {
		t.Fatalf("the reserve is untouched by set-aside files: %v", err)
	}
}

// T0: a peer that resets after the handshake, while the request is written,
// leaves sent true: a write is dispatch_unknown, a non-dedup record is kept.
func TestT0ResetAfterTheHandshakeIsSent(t *testing.T) {
	f := newFake(t, nil)
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	tl := tls.NewListener(ln, f.srv.TLS)
	go func() {
		for {
			c, err := tl.Accept()
			if err != nil {
				return
			}
			if tc, ok := c.(*tls.Conn); ok {
				_ = tc.Handshake()
			}
			c.Close() // reset before the request is read
		}
	}()
	ob := outbox(t)
	c := f.client(ob)
	c.Endpoint.Origin = "https://" + ln.Addr().String()
	res := valid(t, call(c, nudgeReq("op-1")))
	recs, _ := ob.List()
	if res.Outcome != OutcomeDispatchUnknown || !res.Sent || res.Exit() != kit.ExitFailed || len(recs) != 1 || recs[0].State != StateUnknown {
		t.Fatalf("%s %v", res.JSON(), recs)
	}
	// A read in the same case is unavailable but sent.
	if r := valid(t, call(c, readReq())); !r.Sent || r.Outcome != OutcomeUnavailable {
		t.Fatalf("%s", r.JSON())
	}
}

// O22: a second process cannot sweep a record that a live writer holds in flight.
func TestO22ASecondProcessDoesNotSweepALiveWriter(t *testing.T) {
	dir, _ := filepath.EvalSymlinks(t.TempDir())
	path := filepath.Join(dir, "outbox")
	ob, err := OpenOutbox(path, "seat-a", nil)
	if err != nil {
		t.Fatal(err)
	}
	// A queued non-dedup record, as a live writer would hold it mid-flight.
	if _, err := ob.Enqueue("zmsg", "nudge", "op-live", StoredRequest{Method: "POST", Path: "/", Audience: "aud", Body: json.RawMessage(`{}`)}); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(os.Args[0], "-test.run=TestHelperHoldRecordLock")
	cmd.Env = append(os.Environ(), "ZEO_HELPER_LOCK="+filepath.Join(path, fileName(1, "op-live")))
	out, _ := cmd.StdoutPipe()
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	line := make([]byte, 6)
	if _, err := out.Read(line); err != nil || string(line) != "locked" {
		t.Fatalf("helper: %q %v", line, err)
	}
	if _, err := OpenOutbox(path, "seat-a", nil); err != nil { // a second invocation opens: the sweep runs
		t.Fatal(err)
	}
	if recs, _ := ob.List(); len(recs) != 1 {
		t.Fatalf("a live writer's record was swept: %v", recs)
	}
	_ = cmd.Process.Kill()
	_, _ = cmd.Process.Wait()
	if _, err := OpenOutbox(path, "seat-a", nil); err != nil {
		t.Fatal(err)
	}
	ents, _ := os.ReadDir(path)
	if recs, _ := ob.List(); len(recs) != 0 || !hasSuffixFile(ents, ".stale") {
		t.Fatalf("once the writer is dead its queued non-dedup record is swept: %v", recs)
	}
}

// TestHelperHoldRecordLock is a helper process: it takes the in-flight lock of a
// record file and holds it until it is killed.
func TestHelperHoldRecordLock(t *testing.T) {
	path := os.Getenv("ZEO_HELPER_LOCK")
	if path == "" {
		t.Skip("helper only")
	}
	if _, err := lockFile(path); err != nil {
		os.Stdout.WriteString("failed")
		os.Exit(1)
	}
	os.Stdout.WriteString("locked")
	time.Sleep(time.Minute)
}

// O23: abandon refuses a dedup record and a queued one.
func TestO23AbandonRefusals(t *testing.T) {
	ob := outbox(t)
	enqueue(t, ob, "op-d")
	if err := ob.Abandon("op-d", "e", ObservedApplied); err == nil {
		t.Fatal("a queued dedup record cannot be abandoned")
	}
	ob.SetState(mustFirst(t, ob), StateUnknown)
	if err := ob.Abandon("op-d", "e", ObservedApplied); err == nil {
		t.Fatal("an unknown dedup record is reconciled, not abandoned")
	}
}

func TestDoctorShowsTheBinary(t *testing.T) {
	f := newFake(t, nil)
	if d := f.client(outbox(t)).Doctor(context.Background(), "zmsg", "/v1/health", "aud"); d.Binary == "" {
		t.Fatal("doctor should show the binary's own path")
	}
}

// A queued record of a verb with no dedup contract is not abandoned (it is not unknown).
func TestAbandonRefusesAQueuedNonDedupRecord(t *testing.T) {
	ob := outbox(t)
	if _, err := ob.Enqueue("zmsg", "nudge", "op-n", StoredRequest{Method: "POST", Path: "/", Audience: "aud", Body: json.RawMessage(`{}`)}); err != nil {
		t.Fatal(err)
	}
	if err := ob.Abandon("op-n", "e", ObservedApplied); err == nil {
		t.Fatal("only an unknown record can be abandoned")
	}
	if recs, _ := ob.List(); recs[0].State != StateQueued {
		t.Fatal("a refused abandon must change nothing")
	}
}

// An unknown UNORDERED record neither blocks a drain nor later writes.
func TestAnUnknownUnorderedRecordBlocksNothing(t *testing.T) {
	f := newFake(t, lostAnswer)
	ob := outbox(t)
	valid(t, call(f.client(ob), stopReq("op-stop"))) // unknown, unordered
	f.mode = nil
	enqueue(t, ob, "op-q") // a dedup record queued behind it
	c := f.client(ob)
	if n, _ := c.Drain(context.Background()); n != 1 || f.seen["op-q"] != 1 {
		t.Fatalf("an unordered unknown record blocked a drain: %d %v", n, f.seen)
	}
	if r := valid(t, call(c, nudgeReq("op-ord"))); r.Outcome != OutcomeOK {
		t.Fatalf("an unordered unknown record blocked an ordered write: %s", r.JSON())
	}
}

// A drain leaves a record that another process holds in flight.
func TestADrainSkipsARecordAnotherProcessHolds(t *testing.T) {
	f := newFake(t, nil)
	dir, _ := filepath.EvalSymlinks(t.TempDir())
	ob, err := OpenOutbox(filepath.Join(dir, "outbox"), "seat-a", nil)
	if err != nil {
		t.Fatal(err)
	}
	enqueue(t, ob, "op-held")
	cmd := exec.Command(os.Args[0], "-test.run=TestHelperHoldRecordLock")
	cmd.Env = append(os.Environ(), "ZEO_HELPER_LOCK="+filepath.Join(ob.dir, fileName(1, "op-held")))
	out, _ := cmd.StdoutPipe()
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = cmd.Process.Kill(); _, _ = cmd.Process.Wait() }()
	buf := make([]byte, 6)
	if _, err := out.Read(buf); err != nil || string(buf) != "locked" {
		t.Fatalf("%q %v", buf, err)
	}
	if n, _ := f.client(ob).Drain(context.Background()); n != 0 || f.seen["op-held"] != 0 {
		t.Fatalf("a drain sent a record that a live writer holds: %d %v", n, f.seen)
	}
}

// A drain never sends a queued record of a verb with no dedup contract.
func TestADrainSkipsANonDedupRecord(t *testing.T) {
	f := newFake(t, nil)
	ob := outbox(t)
	if _, err := ob.Enqueue("zmsg", "nudge", "op-n", StoredRequest{Method: "POST", Path: "/v1/nudge", Audience: "aud", Body: json.RawMessage(`{}`)}); err != nil {
		t.Fatal(err)
	}
	enqueue(t, ob, "op-d")
	if n, _ := f.client(ob).Drain(context.Background()); n != 1 || f.seen["op-n"] != 0 || f.seen["op-d"] != 1 {
		t.Fatalf("%d %v", n, f.seen)
	}
}

// The unordered verbs have their own record and byte bounds.
func TestTheUnorderedBoundsHold(t *testing.T) {
	ob := outbox(t)
	ob.maxRecords = 2
	un := func(id string) error {
		_, err := ob.Enqueue("zmsg", "stop", id, StoredRequest{Method: "POST", Path: "/", Audience: "aud", Unordered: true, Body: json.RawMessage(`{}`)})
		return err
	}
	if un("a") != nil || un("b") != nil || !errors.Is(un("c"), ErrFull) {
		t.Fatal("the record bound of unordered verbs")
	}
	ob2 := outbox(t)
	ob2.maxBytes = 300
	body := json.RawMessage(`"` + strings.Repeat("x", 100) + `"`)
	enq := func(id string) error {
		_, err := ob2.Enqueue("zmsg", "stop", id, StoredRequest{Method: "POST", Path: "/", Audience: "aud", Unordered: true, Body: body})
		return err
	}
	if enq("a") != nil || !errors.Is(enq("b"), ErrFull) {
		t.Fatal("the byte bound of unordered verbs")
	}
}
