package client

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
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

// O5: a drain interrupted after the server accepted resends the same key; one effect.
func TestO5InterruptedDrainIsOneEffect(t *testing.T) {
	f := newFake(t, nil)
	ob := outbox(t)
	enqueue(t, ob, "op-1")
	c := f.client(ob)
	recs, _ := ob.List()
	if res := c.sendRecord(context.Background(), recs[0], secret); res.Outcome != OutcomeOK { // accepted; the crash is before Remove
		t.Fatalf("%s", res.JSON())
	}
	if n, err := c.Drain(context.Background()); err != nil || n != 1 {
		t.Fatalf("%d %v", n, err)
	}
	left, _ := ob.List()
	if len(left) != 0 || f.seen["op-1"] != 2 {
		t.Fatalf("left %v; the service saw the key %d times (and deduplicates by it)", left, f.seen["op-1"])
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
			return Result{Outcome: OutcomeRefused}
		}
		return Result{Outcome: OutcomeOK}
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
	n, _ := ob.Drain(func(Record) Result { return Result{Outcome: OutcomeDispatchUnknown} })
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

// O9: a failed fsync refuses visibly and leaves no temporary file.
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
		for _, e := range ents {
			if strings.HasPrefix(e.Name(), ".tmp-") {
				t.Fatalf("%s: temporary file left: %s", which, e.Name())
			}
		}
	}
	ob := outbox(t)
	ob.syncFile = func(*os.File) error { return errors.New("disk") }
	f := newFake(t, nil)
	if res := valid(t, call(f.client(ob), writeReq("op-1", false))); reason(res) != "outbox" || f.count() != 0 {
		t.Fatalf("a write that cannot be retained must not be sent: %s hits %d", res.JSON(), f.count())
	}
}

// O10: a torn record is moved aside, shown by doctor, never read.
func TestO10TornRecord(t *testing.T) {
	ob := outbox(t)
	enqueue(t, ob, "op-1")
	torn := filepath.Join(ob.dir, fileName(7, "op-7"))
	if err := os.WriteFile(torn, []byte(`{"schema":"zeo.outbox-record/v1","seq":`), 0o600); err != nil {
		t.Fatal(err)
	}
	recs, _ := ob.List()
	s, _ := ob.Stats()
	if len(recs) != 1 || s.Bad != 1 {
		t.Fatalf("%v %+v", recs, s)
	}
	if _, err := os.Stat(torn + ".bad"); err != nil {
		t.Fatal("the torn file should be moved aside")
	}
	// A temporary file from a crash is removed when the outbox is opened.
	tmp := filepath.Join(ob.dir, ".tmp-crash")
	os.WriteFile(tmp, []byte("x"), 0o600)
	if _, err := OpenOutbox(ob.dir, "seat-a", nil); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(tmp); err == nil {
		t.Fatal("a crash temporary file survived the open")
	}
	// A record from another incarnation is not read.
	other := outbox(t)
	enqueue(t, other, "op-1")
	raw, _ := os.ReadFile(filepath.Join(other.dir, fileName(1, "op-1")))
	os.WriteFile(filepath.Join(ob.dir, fileName(1, "op-1")), raw, 0o600)
	if recs, _ := ob.List(); len(recs) != 0 {
		t.Fatalf("%v", recs)
	}
}

// O11: a path-injecting id is refused.
func TestO11PathInjectingIDs(t *testing.T) {
	ob := outbox(t)
	for _, id := range []string{"../x", "a/b", "a\x00b", "", "a b", strings.Repeat("a", 65), "x.json"} {
		if _, err := ob.Enqueue("zmsg", "send", id, req(`{}`)); err == nil {
			t.Errorf("%q should refuse", id)
		}
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
