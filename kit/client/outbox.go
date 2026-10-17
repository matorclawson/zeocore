package client

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"syscall"
	"time"

	"github.com/zeroemployeeorg/zeocore/kit"
)

// RecordSchema names an outbox record.
const RecordSchema = "zeo.outbox-record/v1"

// The outbox's bounds. All states count toward them. At a bound a new write
// refuses; no record is ever evicted.
const (
	MaxRecords = 1000
	MaxBytes   = 8 << 20
)

// The states of a record.
const (
	StateQueued  = "queued"  // a drain may send it
	StateUnknown = "unknown" // sent, no complete answer: never drained; reconcile only
	StateRefused = "refused" // the service refused it; terminal until purged
	// StateAbandoned ends an unknown record of a verb that cannot be reconciled
	// (no dedup contract). It keeps the ambiguity and is never resent.
	StateAbandoned = "abandoned"
)

// ReservedRecords is the room kept for unordered verbs (a stop), out of
// MaxRecords. Scaled down with a smaller bound.
const ReservedRecords = 16

// MaxUnorderedBody caps the body of an unordered verb (a stop), so that the
// room kept for it is a known number of bytes: ReservedRecords times this.
const MaxUnorderedBody = 4 << 10

// MaxAuditBytes is where audit.log is rotated, once, to audit.log.1.
const MaxAuditBytes = 256 << 10

// idRe restricts an operation id: it is a filename.
var idRe = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

// NewOperationID is a 128-bit random idempotency key.
func NewOperationID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic("no randomness")
	}
	return hex.EncodeToString(b[:])
}

// StoredRequest is what a record keeps of the call, so that a drain or a
// reconcile can send exactly it again. Dedup names the owner's contract that
// de-duplicates by key with a stored result.
type StoredRequest struct {
	Method   string `json:"method"`
	Path     string `json:"path"`
	Audience string `json:"audience"`
	Dedup    string `json:"dedup"`
	// Unordered marks a verb the owner declares outside the sequence order (at
	// least stop): never held behind other records, with reserved room.
	Unordered bool            `json:"unordered"`
	Body      json.RawMessage `json:"body"`
}

// Record is one retained write.
type Record struct {
	Schema        string          `json:"schema"`
	Sender        string          `json:"sender"`
	Incarnation   string          `json:"incarnation"`
	Seq           uint64          `json:"seq"`
	Service       string          `json:"service"`
	Verb          string          `json:"verb"`
	OperationID   string          `json:"operation_id"`
	PayloadDigest string          `json:"payload_digest"`
	Request       json.RawMessage `json:"request"`
	Created       string          `json:"created"`
	State         string          `json:"state"`
	// Resolution is set when a person ends the record (abandon).
	Resolution *Resolution `json:"resolution,omitempty"`
}

// Resolution is who ended a record, on what evidence, and what was observed.
// The actor is as claimed: nothing on a same-UID host authenticates it.
type Resolution struct {
	Actor    string `json:"actor"`
	Evidence string `json:"evidence"`
	Observed string `json:"observed"` // applied | not_applied | unknown
	At       string `json:"at"`
}

// Outbox is a directory of records on the host's internal disk. 0700 is not a
// boundary between seats that run as one user.
type Outbox struct {
	dir         string
	sender      string
	incarnation string
	now         func() time.Time

	maxRecords int
	maxBytes   int
	// Evidence, if set, receives each audit line of abandon and resolve, so the
	// tool can mail it to the owner or the ledger. The log in the directory is a
	// local convenience, not the evidence record.
	Evidence func(line string)
	// syncDir and syncFile are the durability calls; tests replace them to
	// see a failure.
	syncFile func(*os.File) error
	syncDir  func(string) error
}

// Errors of the outbox.
var (
	ErrFull        = errors.New("outbox is full")
	ErrKeyConflict = errors.New("the same key with a different payload")
	ErrDrainBusy   = errors.New("another drain is running")
	ErrDisk        = errors.New("the outbox could not be written durably")
	ErrHeld        = errors.New("the outbox order is held: a file needs a person to look at it")
	ErrTooBig      = errors.New("the body of an unordered verb is over its cap")
)

// OpenOutbox opens dir, creating it with mode 0700 if it is missing. The path
// must be absolute and contain no symbolic link.
func OpenOutbox(dir, sender string, now func() time.Time) (*Outbox, error) {
	if !filepath.IsAbs(dir) || filepath.Clean(dir) != dir {
		return nil, errors.New("the outbox path must be absolute and clean")
	}
	if !idRe.MatchString(sender) {
		return nil, errors.New("the outbox sender is not valid")
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, errors.New("the outbox directory cannot be created")
	}
	real, err := filepath.EvalSymlinks(dir)
	if err != nil || real != dir {
		return nil, errors.New("the outbox path must contain no symbolic link")
	}
	st, err := os.Stat(dir)
	if err != nil || !st.IsDir() || st.Mode().Perm()&0o077 != 0 {
		return nil, errors.New("the outbox directory must be mode 0700")
	}
	if now == nil {
		now = time.Now
	}
	o := &Outbox{
		dir: dir, sender: sender, now: now, maxRecords: MaxRecords, maxBytes: MaxBytes,
		syncFile: func(f *os.File) error { return f.Sync() },
		syncDir: func(d string) error {
			f, err := os.Open(d)
			if err != nil {
				return err
			}
			defer f.Close()
			return f.Sync()
		},
	}
	if err := o.loadIncarnation(); err != nil {
		return nil, err
	}
	o.sweep()
	return o, nil
}

// sweep sets aside what a crash left behind, under the outbox lock so that a
// live writer (which holds that lock for the whole of a write) is never swept.
// An unfinished .tmp- file becomes .suspect: it is not a record, and deleting
// it would not prove that no durable intent exists; it was never sent (the
// record is made before the send), so it is terminal and does not hold the
// order. A queued record of a verb with no dedup contract becomes .stale: such
// a verb is never spooled, so a queued record of it is what a crash left, and
// it must never be drained later. A record is moved only if its advisory lock
// can be taken without waiting: a second invocation of the same sender cannot
// sweep a record that a live writer holds in flight.
func (o *Outbox) sweep() {
	unlock, err := o.lock(".alloc.lock", true)
	if err != nil {
		return
	}
	defer unlock()
	if ents, err := os.ReadDir(o.dir); err == nil {
		for _, e := range ents {
			if strings.HasPrefix(e.Name(), ".tmp-") && !strings.Contains(e.Name(), ".suspect") {
				_ = os.Rename(filepath.Join(o.dir, e.Name()), filepath.Join(o.dir, e.Name()+".suspect"))
			}
		}
	}
	recs, _, err := o.list()
	if err != nil {
		return
	}
	for _, r := range recs {
		var sr StoredRequest
		if r.State == StateQueued && strictDecode(r.Request, &sr) == nil && sr.Dedup == "" {
			p := filepath.Join(o.dir, fileName(r.Seq, r.OperationID))
			if l, err := lockFile(p); err == nil {
				_ = os.Rename(p, p+".stale")
				l.Release()
			}
		}
	}
}

// recLock is an advisory flock(2) lock (LOCK_EX|LOCK_NB) on a record file, held
// for as long as the record is in flight. flock, not fcntl: a process loses an
// fcntl lock when it closes ANY descriptor to the file, and the sweep code in
// the same process could do that. The descriptor is close-on-exec (os.OpenFile
// sets it), and the lock is released when the process dies. The lock follows
// the inode, so it is taken again on the replacement file whenever the record
// is rewritten. The outbox must be on a local disk: flock over NFS is out of
// scope (stated, not checked).
type recLock struct{ f *os.File }

func lockFile(path string) (*recLock, error) {
	f, err := os.OpenFile(path, os.O_RDONLY, 0)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return nil, ErrDrainBusy
	}
	return &recLock{f}, nil
}

// Release drops the lock. It is safe on a nil lock.
func (l *recLock) Release() {
	if l != nil && l.f != nil {
		_ = syscall.Flock(int(l.f.Fd()), syscall.LOCK_UN)
		l.f.Close()
		l.f = nil
	}
}

// lockRecord takes the in-flight lock of a record, or fails if another process holds it.
func (o *Outbox) lockRecord(rec Record) (*recLock, error) {
	return lockFile(filepath.Join(o.dir, fileName(rec.Seq, rec.OperationID)))
}

func (o *Outbox) loadIncarnation() error {
	p := filepath.Join(o.dir, "incarnation")
	raw, err := os.ReadFile(p)
	if err == nil {
		id := strings.TrimSpace(string(raw))
		if len(id) != 32 {
			return errors.New("the outbox incarnation is not valid")
		}
		o.incarnation = id
		return nil
	}
	id := NewOperationID()
	if err := os.WriteFile(p, []byte(id+"\n"), 0o600); err != nil {
		return errors.New("the outbox incarnation cannot be written")
	}
	o.incarnation = id
	return nil
}

func fileName(seq uint64, id string) string { return fmt.Sprintf("%020d-%s.json", seq, id) }

// lock takes an advisory lock on name inside the directory.
func (o *Outbox) lock(name string, wait bool) (func(), error) {
	f, err := os.OpenFile(filepath.Join(o.dir, name), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	how := syscall.LOCK_EX
	if !wait {
		how |= syscall.LOCK_NB
	}
	if err := syscall.Flock(int(f.Fd()), how); err != nil {
		f.Close()
		return nil, ErrDrainBusy
	}
	return func() { _ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN); f.Close() }, nil
}

// List returns every record in sequence order. A file that does not parse is
// moved aside as .bad and counted by Stats, never read.
func (o *Outbox) List() ([]Record, error) {
	recs, _, err := o.list()
	return recs, err
}

func (o *Outbox) list() ([]Record, int, error) {
	ents, err := os.ReadDir(o.dir)
	if err != nil {
		return nil, 0, err
	}
	var out []Record
	bad := 0
	for _, e := range ents {
		name := e.Name()
		if strings.HasSuffix(name, ".bad") {
			bad++
			continue
		}
		if strings.HasSuffix(name, ".suspect") || strings.HasSuffix(name, ".stale") {
			continue
		}
		if e.IsDir() || !strings.HasSuffix(name, ".json") {
			continue
		}
		path := filepath.Join(o.dir, name)
		raw, err := os.ReadFile(path)
		var r Record
		if err != nil || strictDecode(raw, &r) != nil || r.Schema != RecordSchema || r.Incarnation != o.incarnation ||
			!idRe.MatchString(r.OperationID) || name != fileName(r.Seq, r.OperationID) {
			_ = os.Rename(path, path+".bad")
			bad++
			continue
		}
		out = append(out, r)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Seq < out[j].Seq })
	return out, bad, nil
}

// Held says whether a .bad file (a record that fails to parse, which may stand
// for an emitted write) holds the order. While it does, no later ordered record
// is sent and no new ordered write is accepted: the missing ordinal is never
// skipped. A .suspect or .stale file was never sent and does not hold it.
func (o *Outbox) Held() bool {
	_, bad, err := o.list()
	return err != nil || bad > 0
}

// Resolve clears a hold after a person has looked at the held files. A .bad
// file is kept, renamed .resolved; a .suspect or .stale file may only be
// discarded (renamed .discarded), never requeued.
func (o *Outbox) Resolve(evidence string) (int, error) {
	ents, err := os.ReadDir(o.dir)
	if err != nil {
		return 0, err
	}
	n := 0
	for _, e := range ents {
		name := e.Name()
		var to string
		switch {
		case strings.HasSuffix(name, ".bad"):
			to = name + ".resolved"
		case strings.HasSuffix(name, ".suspect") || strings.HasSuffix(name, ".stale"):
			to = name + ".discarded"
		default:
			continue
		}
		if err := os.Rename(filepath.Join(o.dir, name), filepath.Join(o.dir, to)); err != nil {
			return n, err
		}
		n++
	}
	if n > 0 {
		o.audit(AuditEntry{Verb: "resolve", Evidence: evidence, Observed: ObservedUnknown})
	}
	return n, nil
}

// AuditEntry is one line of audit.log.
type AuditEntry struct {
	At          string `json:"at"`
	Actor       string `json:"actor"` // as claimed; nothing authenticates it
	Verb        string `json:"verb"`
	Service     string `json:"service"`
	RecordVerb  string `json:"record_verb"`
	OperationID string `json:"operation_id"`
	Evidence    string `json:"evidence"`
	Observed    string `json:"observed"`
}

// audit appends one JSON line to audit.log, rotating it once to audit.log.1 at
// MaxAuditBytes. It is a local convenience, not the evidence record: it is
// writable by every seat on the host, its actor is a claim, and a second
// rotation overwrites the first. abandon and resolve also hand the line to
// Evidence so that the tool can mail it to the owner or the ledger.
func (o *Outbox) audit(e AuditEntry) {
	e.At = o.now().UTC().Format("2006-01-02T15:04:05Z")
	if e.Actor == "" {
		e.Actor = os.Getenv("USER")
	}
	line, err := json.Marshal(e)
	if err != nil {
		return
	}
	path := filepath.Join(o.dir, "audit.log")
	if fi, err := os.Stat(path); err == nil && fi.Size() >= MaxAuditBytes {
		_ = os.Rename(path, path+".1")
	}
	if f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600); err == nil {
		_, _ = f.Write(append(line, '\n'))
		f.Close()
	}
	if o.Evidence != nil && (e.Verb == "abandon" || e.Verb == "resolve") {
		o.Evidence(string(line))
	}
}

// Observations an abandon may record.
const (
	ObservedApplied    = "applied"
	ObservedNotApplied = "not_applied"
	ObservedUnknown    = "unknown"
)

// Show returns a record and the audit lines that name it.
func (o *Outbox) Show(operationID string) (Record, []AuditEntry, error) {
	recs, err := o.List()
	if err != nil {
		return Record{}, nil, err
	}
	for _, r := range recs {
		if r.OperationID != operationID {
			continue
		}
		var out []AuditEntry
		raw, _ := os.ReadFile(filepath.Join(o.dir, "audit.log"))
		for _, ln := range strings.Split(string(raw), "\n") {
			var e AuditEntry
			if ln != "" && json.Unmarshal([]byte(ln), &e) == nil && e.OperationID == operationID {
				out = append(out, e)
			}
		}
		return r, out, nil
	}
	return Record{}, nil, errors.New("no such record")
}

// Abandon ends an unknown record of a verb with no dedup contract: the owner's
// status port or a named reviewer has checked the outcome. The record is kept,
// terminal, never resent, and carries who ended it, on what evidence and what
// was observed. A dedup verb is reconciled instead, and only an unknown record
// can be abandoned.
func (o *Outbox) Abandon(operationID, evidence, observed string) error {
	if evidence == "" || (observed != ObservedApplied && observed != ObservedNotApplied && observed != ObservedUnknown) {
		return errors.New("abandon needs the evidence and what was observed (applied, not_applied or unknown)")
	}
	recs, err := o.List()
	if err != nil {
		return err
	}
	for _, r := range recs {
		if r.OperationID != operationID {
			continue
		}
		var sr StoredRequest
		if r.State != StateUnknown || strictDecode(r.Request, &sr) != nil || sr.Dedup != "" {
			return errors.New("only an unknown record of a verb with no dedup contract can be abandoned")
		}
		at := o.now().UTC().Format("2006-01-02T15:04:05Z")
		r.Resolution = &Resolution{Actor: os.Getenv("USER"), Evidence: evidence, Observed: observed, At: at}
		if err := o.SetState(r, StateAbandoned); err != nil {
			return err
		}
		o.audit(AuditEntry{Verb: "abandon", Service: r.Service, RecordVerb: r.Verb, OperationID: operationID, Evidence: evidence, Observed: observed})
		return nil
	}
	return errors.New("no such record")
}

// aside counts the files set aside (.stale, .suspect, .bad) and the audit logs.
func (o *Outbox) aside() (n, bytes int) {
	ents, err := os.ReadDir(o.dir)
	if err != nil {
		return 0, 0
	}
	for _, e := range ents {
		name := e.Name()
		set := strings.HasSuffix(name, ".stale") || strings.HasSuffix(name, ".suspect") || strings.HasSuffix(name, ".bad")
		if set {
			n++
		}
		if set || name == "audit.log" || name == "audit.log.1" {
			if fi, err := e.Info(); err == nil {
				bytes += int(fi.Size())
			}
		}
	}
	return n, bytes
}

func digestOf(canon []byte) string {
	h := sha256.Sum256(canon)
	return "sha256:" + hex.EncodeToString(h[:])
}

// Enqueue stores a write BEFORE it is sent. The same key and payload is the
// same record (a reply spooled twice is one record); the same key with another
// payload is a conflict, never silently the first payload.
func (o *Outbox) Enqueue(service, verb, id string, req StoredRequest) (Record, error) {
	rec, lk, err := o.EnqueueLocked(service, verb, id, req)
	lk.Release()
	return rec, err
}

// EnqueueLocked is Enqueue that returns the in-flight lock of the record. The
// caller holds it until the write is settled.
func (o *Outbox) EnqueueLocked(service, verb, id string, req StoredRequest) (Record, *recLock, error) {
	rec, lk, err := o.enqueue(service, verb, id, req)
	return rec, lk, err
}

func (o *Outbox) enqueue(service, verb, id string, req StoredRequest) (Record, *recLock, error) {
	if req.Unordered && len(req.Body) > MaxUnorderedBody {
		return Record{}, nil, ErrTooBig
	}
	if !idRe.MatchString(id) {
		return Record{}, nil, errors.New("the operation id is not valid")
	}
	// json.Marshal refuses a body that is not valid JSON.
	rawReq, err := json.Marshal(req)
	if err != nil {
		return Record{}, nil, err
	}
	canon, err := kit.Canonical(rawReq)
	if err != nil {
		return Record{}, nil, errors.New("a stored request must be canonical JSON")
	}
	unlock, err := o.lock(".alloc.lock", true)
	if err != nil {
		return Record{}, nil, ErrDisk
	}
	defer unlock()
	recs, bad, err := o.list()
	if err != nil {
		return Record{}, nil, err
	}
	if bad > 0 && !req.Unordered {
		return Record{}, nil, ErrHeld
	}
	digest := digestOf(canon)
	var next uint64 = 1
	var used int
	for _, r := range recs {
		if r.OperationID == id {
			if r.PayloadDigest != digest {
				return Record{}, nil, ErrKeyConflict
			}
			lk, _ := o.lockRecord(r)
			return r, lk, nil
		}
		if r.Seq >= next {
			next = r.Seq + 1
		}
		used += len(r.Request)
	}
	// Sixteen records and 64 KiB are reserved for unordered verbs (a stop). An
	// ordinary write is refused when it would eat the reserve, by count or by
	// bytes; set-aside files and audit.log count toward the ordinary bound
	// (so disk use is bounded) and never toward the reserve.
	asideN, asideB := o.aside()
	switch {
	case req.Unordered:
		if len(recs) >= o.maxRecords || used+len(canon) > o.maxBytes {
			return Record{}, nil, ErrFull
		}
	default:
		reserveN := o.maxRecords * ReservedRecords / MaxRecords
		reserveB := o.maxBytes / 128
		if len(recs)+asideN >= o.maxRecords-reserveN || used+asideB+len(canon) > o.maxBytes-reserveB {
			return Record{}, nil, ErrFull
		}
	}
	rec := Record{
		Schema: RecordSchema, Sender: o.sender, Incarnation: o.incarnation, Seq: next, Service: service,
		Verb: verb, OperationID: id, PayloadDigest: digest, Request: canon,
		Created: o.now().UTC().Format("2006-01-02T15:04:05Z"), State: StateQueued,
	}
	if err := o.write(rec); err != nil {
		return Record{}, nil, ErrDisk
	}
	lk, err := o.lockRecord(rec)
	if err != nil {
		return Record{}, nil, ErrDisk
	}
	return rec, lk, nil
}

// write puts the record on disk: a temporary name, fsync of the file, rename,
// fsync of the directory. On any failure the temporary file is removed.
func (o *Outbox) write(rec Record) error {
	raw, err := json.Marshal(rec)
	if err != nil {
		return err
	}
	doc, err := kit.Canonical(raw)
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(o.dir, ".tmp-*")
	if err != nil {
		return err
	}
	name := tmp.Name()
	// A failed write or fsync is durability UNKNOWN, not "nothing written":
	// the file is kept aside as .suspect and holds the order.
	fail := func(err error) error { tmp.Close(); _ = os.Rename(name, name+".suspect"); return err }
	if err := tmp.Chmod(0o600); err != nil {
		return fail(err)
	}
	if _, err := tmp.Write(doc); err != nil {
		return fail(err)
	}
	if err := o.syncFile(tmp); err != nil {
		return fail(err)
	}
	if err := tmp.Close(); err != nil {
		_ = os.Rename(name, name+".suspect")
		return err
	}
	final := filepath.Join(o.dir, fileName(rec.Seq, rec.OperationID))
	if err := os.Rename(name, final); err != nil {
		_ = os.Rename(name, name+".suspect")
		return err
	}
	if err := o.syncDir(o.dir); err != nil {
		_ = os.Rename(final, final+".suspect")
		return err
	}
	return nil
}

// SetState changes a record's state.
func (o *Outbox) SetState(rec Record, state string) error {
	lk, err := o.setStateLocked(rec, state, nil)
	lk.Release()
	return err
}

// setStateLocked rewrites the record, under the outbox lock, and takes the
// in-flight lock again on the replacement file before it lets the old one go.
func (o *Outbox) setStateLocked(rec Record, state string, old *recLock) (*recLock, error) {
	unlock, err := o.lock(".alloc.lock", true)
	if err != nil {
		return old, ErrDisk
	}
	defer unlock()
	rec.State = state
	if err := o.write(rec); err != nil {
		return old, ErrDisk
	}
	lk, err := o.lockRecord(rec)
	if err != nil {
		return old, ErrDisk
	}
	old.Release()
	return lk, nil
}

// Remove deletes a delivered record.
func (o *Outbox) Remove(rec Record) error {
	if err := os.Remove(filepath.Join(o.dir, fileName(rec.Seq, rec.OperationID))); err != nil {
		return err
	}
	return o.syncDir(o.dir)
}

// Purge removes the refused and abandoned records, after a person has looked at them.
func (o *Outbox) Purge() (int, error) {
	recs, err := o.List()
	if err != nil {
		return 0, err
	}
	n := 0
	for _, r := range recs {
		if r.State == StateRefused || r.State == StateAbandoned {
			if err := o.Remove(r); err != nil {
				return n, err
			}
			n++
		}
	}
	if n > 0 {
		o.audit(AuditEntry{Verb: "purge", Evidence: "purged records in state refused or abandoned", Observed: ObservedUnknown})
	}
	return n, nil
}

// Stats is what doctor prints.
type Stats struct {
	Count     int
	Bytes     int
	OldestAge time.Duration
	Queued    int
	Unknown   int
	Refused   int
	Bad       int
	Abandoned int
	Aside     int // .suspect and .stale files: terminal, shown, never read
	NearFull  bool
}

// Stats reads the outbox's state.
func (o *Outbox) Stats() (Stats, error) {
	recs, bad, err := o.list()
	if err != nil {
		return Stats{}, err
	}
	s := Stats{Bad: bad}
	for _, r := range recs {
		s.Count++
		s.Bytes += len(r.Request)
		switch r.State {
		case StateQueued:
			s.Queued++
		case StateUnknown:
			s.Unknown++
		case StateRefused:
			s.Refused++
		case StateAbandoned:
			s.Abandoned++
		}
		if t, err := time.Parse("2006-01-02T15:04:05Z", r.Created); err == nil {
			if age := o.now().Sub(t); age > s.OldestAge {
				s.OldestAge = age
			}
		}
	}
	if ents, err := os.ReadDir(o.dir); err == nil {
		for _, e := range ents {
			if strings.HasSuffix(e.Name(), ".suspect") || strings.HasSuffix(e.Name(), ".stale") {
				s.Aside++
			}
		}
	}
	s.NearFull = s.Count >= o.maxRecords*9/10 || s.Bytes >= o.maxBytes*9/10
	return s, nil
}

// Drain sends queued records in sequence order, one at a time, under a lock
// so that only one drain runs. Before each send the record is made `unknown`,
// durably: a crash before that proves no emission, and one after it leaves the
// record unknown, never to be resent by a drain. After the send: ok removes
// the record; a failure that proves nothing was sent puts it back to queued
// and stops; a refusal marks it refused and the drain goes on; anything else
// leaves it unknown and stops. A record that is not queued stops the drain (a
// refused one is skipped): an unresolved unknown record blocks the sender's
// later records, so order is kept. A hold stops it too.
func (o *Outbox) Drain(send func(Record) Result) (sent int, err error) {
	return o.DrainBefore("", 0, send)
}

// DrainBefore is Drain limited to one service's records ("" means all) and to
// those ahead of sequence number seq (0 means all), so a new write can clear
// what is ahead of it without sending itself. Records of other services are
// neither sent nor a block: order is kept per service and sender.
func (o *Outbox) DrainBefore(service string, seq uint64, send func(Record) Result) (sent int, err error) {
	unlock, err := o.lock(".drain.lock", false)
	if err != nil {
		return 0, err
	}
	defer unlock()
	recs, bad, err := o.list()
	if err != nil {
		return 0, err
	}
	if bad > 0 {
		return 0, ErrHeld
	}
	for _, r := range recs {
		if service != "" && r.Service != service {
			continue
		}
		if seq != 0 && r.Seq >= seq {
			return sent, nil
		}
		var sr StoredRequest
		if strictDecode(r.Request, &sr) != nil {
			return sent, nil
		}
		if r.State == StateRefused || r.State == StateAbandoned || sr.Unordered && r.State == StateUnknown {
			continue
		}
		if r.State != StateQueued {
			return sent, nil
		}
		if sr.Dedup == "" {
			// A verb with no dedup contract is never spooled and never drained.
			continue
		}
		// Another process holds this record in flight: leave it, and the order.
		lk, lerr := o.lockRecord(r)
		if lerr != nil {
			return sent, nil
		}
		lk, err = o.setStateLocked(r, StateUnknown, lk)
		if err != nil {
			lk.Release()
			return sent, err
		}
		res := send(r)
		lk.Release()
		switch {
		case res.Outcome == OutcomeOK:
			if err := o.Remove(r); err != nil {
				return sent, err
			}
			sent++
		case res.Outcome == OutcomeRefused && res.Sent || res.Outcome == OutcomeInvalid || res.Outcome == OutcomeNotFound:
			if err := o.SetState(r, StateRefused); err != nil {
				return sent, err
			}
		case !res.Sent:
			return sent, o.SetState(r, StateQueued)
		default:
			return sent, nil
		}
	}
	return sent, nil
}
