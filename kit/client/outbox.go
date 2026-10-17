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
	// An unfinished temporary file from a crash is not a record, and deleting
	// it would not prove that no durable intent exists: it is kept aside as
	// .suspect. It was never sent (the record is made before the send), so it
	// is terminal and does not hold the order.
	if ents, err := os.ReadDir(dir); err == nil {
		for _, e := range ents {
			if strings.HasPrefix(e.Name(), ".tmp-") && !strings.Contains(e.Name(), ".suspect") {
				_ = os.Rename(filepath.Join(dir, e.Name()), filepath.Join(dir, e.Name()+".suspect"))
			}
		}
	}
	o.setAsideStale()
	return o, nil
}

// setAsideStale moves a queued record of a verb with no dedup contract aside as
// .stale: such a verb is never spooled, so a queued record of it is what a
// crash left behind, and it must never be drained later.
func (o *Outbox) setAsideStale() {
	recs, _, err := o.list()
	if err != nil {
		return
	}
	for _, r := range recs {
		var sr StoredRequest
		if r.State == StateQueued && strictDecode(r.Request, &sr) == nil && sr.Dedup == "" {
			p := filepath.Join(o.dir, fileName(r.Seq, r.OperationID))
			_ = os.Rename(p, p+".stale")
		}
	}
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
func (o *Outbox) Resolve() (int, error) {
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
		o.audit("resolve", "")
	}
	return n, nil
}

// audit appends one line (time, verb, id) to audit.log. Nothing stops another
// seat on the same host from running these verbs; the log makes it visible.
func (o *Outbox) audit(verb, id string) {
	f, err := os.OpenFile(filepath.Join(o.dir, "audit.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return
	}
	defer f.Close()
	_, _ = f.WriteString(o.now().UTC().Format("2006-01-02T15:04:05Z") + " " + verb + " " + id + "\n")
}

// Abandon ends an unknown record of a verb with no dedup contract: the owner's
// status port or a named reviewer has checked the outcome. The record is kept,
// terminal, and never resent. A dedup verb is reconciled instead.
func (o *Outbox) Abandon(operationID string) error {
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
		if err := o.SetState(r, StateAbandoned); err != nil {
			return err
		}
		o.audit("abandon", operationID)
		return nil
	}
	return errors.New("no such record")
}

func digestOf(canon []byte) string {
	h := sha256.Sum256(canon)
	return "sha256:" + hex.EncodeToString(h[:])
}

// Enqueue stores a write BEFORE it is sent. The same key and payload is the
// same record (a reply spooled twice is one record); the same key with another
// payload is a conflict, never silently the first payload.
func (o *Outbox) Enqueue(service, verb, id string, req StoredRequest) (Record, error) {
	if !idRe.MatchString(id) {
		return Record{}, errors.New("the operation id is not valid")
	}
	// json.Marshal refuses a body that is not valid JSON.
	rawReq, err := json.Marshal(req)
	if err != nil {
		return Record{}, err
	}
	canon, err := kit.Canonical(rawReq)
	if err != nil {
		return Record{}, errors.New("a stored request must be canonical JSON")
	}
	unlock, err := o.lock(".alloc.lock", true)
	if err != nil {
		return Record{}, ErrDisk
	}
	defer unlock()
	recs, bad, err := o.list()
	if err != nil {
		return Record{}, err
	}
	if bad > 0 && !req.Unordered {
		return Record{}, ErrHeld
	}
	digest := digestOf(canon)
	var next uint64 = 1
	var used int
	for _, r := range recs {
		if r.OperationID == id {
			if r.PayloadDigest != digest {
				return Record{}, ErrKeyConflict
			}
			return r, nil
		}
		if r.Seq >= next {
			next = r.Seq + 1
		}
		used += len(r.Request)
	}
	limit := o.maxRecords
	if !req.Unordered {
		limit -= o.maxRecords * ReservedRecords / MaxRecords
	}
	if len(recs) >= limit || used+len(canon) > o.maxBytes {
		return Record{}, ErrFull
	}
	rec := Record{
		Schema: RecordSchema, Sender: o.sender, Incarnation: o.incarnation, Seq: next, Service: service,
		Verb: verb, OperationID: id, PayloadDigest: digest, Request: canon,
		Created: o.now().UTC().Format("2006-01-02T15:04:05Z"), State: StateQueued,
	}
	if err := o.write(rec); err != nil {
		return Record{}, ErrDisk
	}
	return rec, nil
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
	rec.State = state
	if err := o.write(rec); err != nil {
		return ErrDisk
	}
	return nil
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
		o.audit("purge", "")
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
		if err := o.SetState(r, StateUnknown); err != nil {
			return sent, err
		}
		res := send(r)
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
