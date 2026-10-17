package client

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/zeroemployeeorg/zeocore/kit"
)

// RecordSchema names an outbox record.
const RecordSchema = "zeo.outbox-record/v1"

// The outbox's bounds (SOW-35 section 10.4). At a bound a new write refuses;
// no record is ever evicted.
const (
	MaxRecords = 1000
	MaxBytes   = 8 << 20
)

// The states of a record.
const (
	StateQueued  = "queued"  // not yet sent
	StateUnknown = "unknown" // sent, no complete answer: reconcile, never resend by itself
	StateRefused = "refused" // the service refused it; retrying cannot cure it
)

var idRe = regexp.MustCompile(`^[A-Za-z0-9._:-]{1,128}$`)

// Record is one retained write.
type Record struct {
	Schema      string          `json:"schema"`
	Sender      string          `json:"sender"`
	Seq         uint64          `json:"seq"`
	Service     string          `json:"service"`
	Verb        string          `json:"verb"`
	OperationID string          `json:"operation_id"`
	Request     json.RawMessage `json:"request"`
	Created     string          `json:"created"`
	State       string          `json:"state"`
}

// Outbox is a directory of records on the host's internal disk.
type Outbox struct {
	dir    string
	sender string
	now    func() time.Time

	maxRecords int
	maxBytes   int
}

// ErrFull is returned when a bound is reached.
var ErrFull = errors.New("outbox is full")

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
	return &Outbox{dir: dir, sender: sender, now: now, maxRecords: MaxRecords, maxBytes: MaxBytes}, nil
}

func fileName(seq uint64, id string) string { return fmt.Sprintf("%020d-%s.json", seq, id) }

// List returns every record in sequence order.
func (o *Outbox) List() ([]Record, error) {
	ents, err := os.ReadDir(o.dir)
	if err != nil {
		return nil, err
	}
	var out []Record
	for _, e := range ents {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		raw, err := os.ReadFile(filepath.Join(o.dir, e.Name()))
		if err != nil {
			return nil, err
		}
		var r Record
		if err := json.Unmarshal(raw, &r); err != nil || r.Schema != RecordSchema {
			return nil, fmt.Errorf("outbox record %s is not valid", e.Name())
		}
		out = append(out, r)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Seq < out[j].Seq })
	return out, nil
}

// Enqueue stores a write. A second Enqueue of the same operation id returns
// the first record unchanged, so a reply spooled twice is one record.
func (o *Outbox) Enqueue(service, verb, id string, request []byte) (Record, error) {
	if !idRe.MatchString(id) {
		return Record{}, errors.New("the operation id is not valid")
	}
	if !json.Valid(request) {
		return Record{}, errors.New("a spooled request must be JSON")
	}
	canon, err := kit.Canonical(request)
	if err != nil {
		return Record{}, errors.New("a spooled request must be canonical JSON")
	}
	recs, err := o.List()
	if err != nil {
		return Record{}, err
	}
	var next uint64 = 1
	var bytes int
	for _, r := range recs {
		if r.OperationID == id {
			return r, nil
		}
		if r.Seq >= next {
			next = r.Seq + 1
		}
		bytes += len(r.Request)
	}
	if len(recs) >= o.maxRecords || bytes+len(canon) > o.maxBytes {
		return Record{}, ErrFull
	}
	rec := Record{
		Schema: RecordSchema, Sender: o.sender, Seq: next, Service: service, Verb: verb,
		OperationID: id, Request: canon, Created: o.now().UTC().Format("2006-01-02T15:04:05Z"), State: StateQueued,
	}
	return rec, o.write(rec)
}

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
	defer os.Remove(name)
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(doc); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(name, filepath.Join(o.dir, fileName(rec.Seq, rec.OperationID))); err != nil {
		return err
	}
	if d, err := os.Open(o.dir); err == nil {
		_ = d.Sync()
		d.Close()
	}
	return nil
}

// SetState changes a record's state.
func (o *Outbox) SetState(rec Record, state string) error {
	rec.State = state
	return o.write(rec)
}

// Remove deletes a delivered record.
func (o *Outbox) Remove(rec Record) error {
	return os.Remove(filepath.Join(o.dir, fileName(rec.Seq, rec.OperationID)))
}

// Stats is what doctor prints.
type Stats struct {
	Count     int
	Bytes     int
	OldestAge time.Duration
	Queued    int
	Unknown   int
	Refused   int
	NearFull  bool
}

// Stats reads the outbox's state.
func (o *Outbox) Stats() (Stats, error) {
	recs, err := o.List()
	if err != nil {
		return Stats{}, err
	}
	var s Stats
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
		}
		if t, err := time.Parse("2006-01-02T15:04:05Z", r.Created); err == nil {
			if age := o.now().Sub(t); age > s.OldestAge {
				s.OldestAge = age
			}
		}
	}
	s.NearFull = s.Count >= o.maxRecords*9/10 || s.Bytes >= o.maxBytes*9/10
	return s, nil
}

// Drain sends queued records in sequence order, one at a time. A record is
// removed only after the send reports ok; a refusal marks it refused and the
// drain goes on; a dispatch of unknown outcome marks it unknown and stops;
// anything else stops with the record still queued. A record that is not
// queued stops the drain too, so per-sender order is kept.
func (o *Outbox) Drain(send func(Record) Result) (sent int, err error) {
	recs, err := o.List()
	if err != nil {
		return 0, err
	}
	for _, r := range recs {
		if r.State != StateQueued {
			return sent, nil
		}
		res := send(r)
		switch res.Outcome {
		case OutcomeOK:
			if err := o.Remove(r); err != nil {
				return sent, err
			}
			sent++
		case OutcomeRefused, OutcomeInvalid:
			if err := o.SetState(r, StateRefused); err != nil {
				return sent, err
			}
		case OutcomeDispatchUnknown:
			return sent, o.SetState(r, StateUnknown)
		default:
			return sent, nil
		}
	}
	return sent, nil
}
