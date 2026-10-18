package client

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptrace"
	"os"
	"regexp"
	"strings"
	"time"
)

// Bounds on what the client sends, reads and prints.
const (
	MaxRequest  = 1 << 20
	MaxResponse = 1 << 20
	MaxEnvelope = 8 << 10
)

// Envelope is the owning service's authorization envelope. The client carries
// it unchanged in one slot: it never reads, builds, logs or signs it.
type Envelope struct {
	Schema   string `json:"schema"`
	Audience string `json:"audience"`
	Body     []byte `json:"body"`
}

// Request is one call.
type Request struct {
	Service string
	Verb    string
	Method  string
	Path    string // starts with "/", no ".."
	Body    []byte // JSON for a write
	// OperationID is the idempotency key of a write; empty means generate one.
	OperationID string
	// Audience is the service instance the call is for; the credential is
	// attached only if it equals the host file's audience.
	Audience string
	Read     bool
	// Spool asks that an offline write be queued. Dedup names the owner's
	// versioned contract that de-duplicates by key with a stored result; a
	// write is spoolable only with it, and key-idempotent (reconcilable) only
	// with it.
	Spool bool
	Dedup string
	// Unordered marks a verb the owner declares outside the sequence order (at
	// least stop): written ahead, but never held behind other records.
	Unordered       bool
	RequireDelivery bool
	// ExpectSchema, if set, is the schema id the answer must carry.
	ExpectSchema string
	Envelope     *Envelope
}

// Client calls one resolved remote endpoint.
type Client struct {
	Endpoint Endpoint
	// Roots are the trust roots from the root-owned bundle. They are never
	// read from SSL_CERT_FILE or SSL_CERT_DIR.
	Roots *x509.CertPool
	// Credential returns the Authorization header value. It proves this host,
	// not a seat, and is never put in a Result or an error.
	Credential func() (string, error)
	Outbox     *Outbox
	// Service is the service this client reaches; a drain sends only its records.
	Service string
	Sleep   func(time.Duration)
	Jitter  func(time.Duration) time.Duration
}

var errPin = errors.New("the server key does not match the pin")

func (c *Client) httpClient(p Policy) *http.Client {
	cfg := &tls.Config{RootCAs: c.Roots, MinVersion: tls.VersionTLS12, NextProtos: []string{"http/1.1"}}
	if c.Endpoint.SPKI != "" {
		want := c.Endpoint.SPKI
		cfg.VerifyConnection = func(cs tls.ConnectionState) error {
			if len(cs.PeerCertificates) == 0 {
				return errPin
			}
			sum := sha256.Sum256(cs.PeerCertificates[0].RawSubjectPublicKeyInfo)
			if hex.EncodeToString(sum[:]) != want {
				return errPin
			}
			return nil
		}
	}
	return &http.Client{
		Transport: &http.Transport{
			Proxy:               nil, // never from HTTPS_PROXY or ALL_PROXY
			DisableKeepAlives:   true,
			DialContext:         (&net.Dialer{Timeout: p.Connect}).DialContext,
			TLSHandshakeTimeout: p.Connect,
			TLSClientConfig:     cfg,
			ForceAttemptHTTP2:   false,
			// An empty, non-nil map turns HTTP/2 off: its transport has retry paths
			// of its own that keep-alive settings do not govern.
			TLSNextProto: map[string]func(string, *tls.Conn) http.RoundTripper{},
		},
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
}

func (c *Client) result(r Request, o Outcome, sent bool, reason string) Result {
	return Result{
		Service: r.Service, Verb: r.Verb, Outcome: o, Sent: sent, Transport: "remote",
		EndpointSource: c.Endpoint.Source, OperationID: str(r.OperationID), Reason: str(reason),
	}
}

var pathRe = regexp.MustCompile(`^/[!-~]*$`)

// validPath is a request path: it starts with a slash, is printable ASCII, and
// holds no "..", backslash or fragment, so joined to an origin it cannot name
// another host.
func validPath(p string) bool {
	return pathRe.MatchString(p) && !strings.Contains(p, "..") && !strings.ContainsAny(p, "\\#")
}

func validMethod(m string) bool {
	switch m {
	case "GET", "POST", "PUT", "PATCH", "DELETE":
		return true
	}
	return false
}

// Call performs r.
//
// A read fails closed (no cache) and retries only a failure before any byte
// was sent, inside one deadline. A write is stored in the outbox BEFORE it is
// sent, so an unknown outcome can always be retained; it is never retried
// inside one call, and it completes, is refused, is spooled (only if asked and
// only if the owner's contract de-duplicates by key), or comes back
// dispatch_unknown with its operation id and spool id.
func (c *Client) Call(ctx context.Context, r Request) Result {
	if !validService(r.Service) || r.Verb == "" || !validMethod(r.Method) ||
		!validPath(r.Path) || len(r.Body) > MaxRequest ||
		(r.Envelope != nil && len(r.Envelope.Body) > MaxEnvelope) {
		return c.result(r, OutcomeInvalid, false, "request")
	}
	if !r.Read {
		if r.OperationID == "" {
			r.OperationID = NewOperationID()
		}
		if !idRe.MatchString(r.OperationID) {
			return c.result(r, OutcomeInvalid, false, "operation_id")
		}
		if !json.Valid(r.Body) {
			return c.result(r, OutcomeInvalid, false, "request")
		}
	}
	if c.Endpoint.Local || c.Endpoint.Origin == "" {
		return c.result(r, OutcomeRefused, false, "endpoint")
	}
	if c.Service != "" && r.Service != c.Service {
		return c.result(r, OutcomeRefused, false, "origin")
	}
	authz := ""
	if c.Endpoint.Credential {
		if r.Audience == "" || r.Audience != c.Endpoint.Audience {
			return c.result(r, OutcomeRefused, false, "origin")
		}
		if c.Credential != nil {
			v, err := c.Credential()
			if err != nil {
				return c.result(r, OutcomeRefused, false, "credential")
			}
			authz = v
		}
	}
	policy := Write
	if r.Read {
		policy = Read
	}
	ctx, cancel := context.WithTimeout(ctx, policy.Total)
	defer cancel()

	if r.Read {
		res := c.readLoop(ctx, r, policy, authz)
		if res.Outcome == OutcomeOK && c.Outbox != nil {
			c.drainAfter(ctx, r.Service, authz)
		}
		return res
	}
	return c.write(ctx, r, policy, authz)
}

func (c *Client) readLoop(ctx context.Context, r Request, p Policy, authz string) Result {
	sleep, jitter := c.Sleep, c.Jitter
	if sleep == nil {
		sleep = time.Sleep
	}
	if jitter == nil {
		jitter = Jitter
	}
	var res Result
	for attempt := 0; ; attempt++ {
		res = c.once(ctx, r, p, authz)
		again := attempt < len(p.Backoff) && !res.Sent && ctx.Err() == nil &&
			(res.Outcome == OutcomeUnavailable || res.Outcome == OutcomeTimeout)
		if !again {
			return res
		}
		wait := jitter(p.Backoff[attempt])
		if dl, ok := ctx.Deadline(); ok && time.Until(dl) <= wait {
			return res
		}
		sleep(wait)
	}
}

func (c *Client) write(ctx context.Context, r Request, p Policy, authz string) Result {
	if c.Outbox == nil {
		return c.result(r, OutcomeRefused, false, "no_outbox")
	}
	if r.Spool && r.Dedup == "" {
		return c.result(r, OutcomeRefused, false, "not_spoolable")
	}
	rec, lk, err := c.Outbox.EnqueueLocked(r.Service, r.Verb, r.OperationID, StoredRequest{
		Method: r.Method, Path: r.Path, Audience: r.Audience, Dedup: r.Dedup, Unordered: r.Unordered, Body: r.Body,
		Envelope: r.Envelope,
	})
	defer func() { lk.Release() }()
	switch {
	case errors.Is(err, ErrTooBig):
		return c.result(r, OutcomeInvalid, false, "request")
	case errors.Is(err, ErrFull):
		return c.result(r, OutcomeRefused, false, "outbox_full")
	case errors.Is(err, ErrKeyConflict):
		return c.result(r, OutcomeRefused, false, "key_conflict")
	case errors.Is(err, ErrHeld):
		return c.result(r, OutcomeRefused, false, "blocked")
	case errors.Is(err, ErrDrainBusy):
		return c.result(r, OutcomeRefused, false, "busy")
	case err != nil:
		return c.result(r, OutcomeRefused, false, "outbox")
	}
	spool := str(fileName(rec.Seq, rec.OperationID))
	switch rec.State {
	case StateUnknown:
		// Already sent once with no complete answer: never sent again here.
		res := c.result(r, OutcomeDispatchUnknown, true, "dispatch_unknown")
		res.SpoolID = spool
		return res
	case StateRefused, StateAbandoned:
		return c.result(r, OutcomeRefused, false, "outbox")
	}
	// Per service and sender, a new write never overtakes what is ahead of it.
	if !r.Unordered && c.ahead(rec) {
		_, _ = c.Outbox.DrainBefore(rec.Service, rec.Seq, func(old Record) Result { return c.sendRecord(ctx, old, authz) })
		if c.ahead(rec) {
			if r.Spool && !r.RequireDelivery {
				out := c.result(r, OutcomeSpooled, false, "")
				out.SpoolID = spool
				return out
			}
			_ = c.Outbox.Remove(rec)
			return c.result(r, OutcomeRefused, false, "blocked")
		}
	}
	// Durably `unknown` BEFORE any byte is emitted: a crash before this proves
	// no emission, and a crash after it leaves the record unknown.
	lk, err = c.Outbox.setStateLocked(rec, StateUnknown, lk)
	if err != nil {
		return c.result(r, OutcomeRefused, false, "outbox")
	}
	res := c.once(ctx, r, p, authz)
	switch {
	case res.Outcome == OutcomeDispatchUnknown:
		res.SpoolID = spool
	case res.Outcome == OutcomeOK:
		_ = c.Outbox.Remove(rec)
	case !res.Sent:
		// Nothing left this process: the record goes back to queued, or goes.
		if r.Spool && !r.RequireDelivery && (res.Outcome == OutcomeUnavailable || res.Outcome == OutcomeTimeout) {
			// Spooled is promised only once the record is durably queued again.
			if err := c.Outbox.SetState(rec, StateQueued); err != nil {
				return c.result(r, OutcomeRefused, false, "outbox")
			}
			out := c.result(r, OutcomeSpooled, false, "")
			out.SpoolID = spool
			return out
		}
		_ = c.Outbox.Remove(rec)
		switch {
		case r.Spool && r.RequireDelivery && (res.Outcome == OutcomeUnavailable || res.Outcome == OutcomeTimeout):
			return c.result(r, OutcomeUnavailable, false, "require_delivery")
		case res.Outcome == OutcomeUnavailable && *res.Reason == "unavailable":
			return c.result(r, OutcomeUnavailable, false, "offline")
		}
	default:
		// The service answered definitively (refused, invalid, not found) or the
		// call could not complete after send: a definitive answer ends the record.
		_ = c.Outbox.Remove(rec)
	}
	return res
}

func unorderedRecord(r Record) bool {
	var sr StoredRequest
	return strictDecode(r.Request, &sr) == nil && sr.Unordered
}

// ahead says whether a record of the same service is queued or unknown ahead
// of rec. (A hold is refused earlier, when the record is enqueued.)
func (c *Client) ahead(rec Record) bool {
	recs, err := c.Outbox.List()
	if err != nil {
		return true
	}
	for _, o := range recs {
		if o.Service == rec.Service && o.Seq < rec.Seq && o.State != StateRefused && o.State != StateAbandoned && !unorderedRecord(o) {
			return true
		}
	}
	return false
}

// drainAfter drains this service's queued records once, best effort, after a
// call to it succeeded.
func (c *Client) drainAfter(ctx context.Context, service, authz string) {
	// The implicit drain has its own deadline and changes neither the result
	// nor the exit of the call that triggered it.
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), Write.Total)
	defer cancel()
	_, _ = c.drain(ctx, service, authz)
}

// Drain sends the queued records. Only one drain runs at a time.
func (c *Client) Drain(ctx context.Context) (int, error) {
	if !validService(c.Service) {
		return 0, errors.New("a drain needs the service it drains")
	}
	authz := ""
	if c.Endpoint.Credential && c.Credential != nil {
		v, err := c.Credential()
		if err != nil {
			return 0, err
		}
		authz = v
	}
	return c.drain(ctx, c.Service, authz)
}

func (c *Client) drain(ctx context.Context, service, authz string) (int, error) {
	return c.Outbox.DrainBefore(service, 0, func(rec Record) Result { return c.sendRecord(ctx, rec, authz) })
}

func (c *Client) recordRequest(rec Record) (Request, error) {
	var sr StoredRequest
	if err := strictDecode(rec.Request, &sr); err != nil {
		return Request{}, err
	}
	return Request{
		Service: rec.Service, Verb: rec.Verb, Method: sr.Method, Path: sr.Path, Body: sr.Body,
		OperationID: rec.OperationID, Audience: sr.Audience, Dedup: sr.Dedup, Envelope: sr.Envelope,
	}, nil
}

func (c *Client) sendRecord(ctx context.Context, rec Record, authz string) Result {
	r, err := c.recordRequest(rec)
	if err != nil {
		return Result{Outcome: OutcomeInvalid, Reason: str("request")}
	}
	// A record is sent only by the client of its own service, to the audience it
	// was written for: the credential never goes anywhere else.
	if (c.Service != "" && rec.Service != c.Service) ||
		(c.Endpoint.Credential && (r.Audience == "" || r.Audience != c.Endpoint.Audience)) {
		return Result{Outcome: OutcomeRefused, Reason: str("origin")}
	}
	cctx, cancel := context.WithTimeout(ctx, Write.Total)
	defer cancel()
	return c.once(cctx, r, Write, authz)
}

// Reconcile resends an unknown record with the SAME key. It is the only way a
// record in that state is sent again, and only for a verb whose owner declared
// key de-duplication with a stored result.
func (c *Client) Reconcile(ctx context.Context, operationID string) Result {
	fail := func(o Outcome, reason string) Result {
		return Result{Schema: ResultSchema, Outcome: o, Reason: str(reason), Transport: "remote", EndpointSource: c.Endpoint.Source, OperationID: str(operationID)}
	}
	if c.Outbox == nil {
		return fail(OutcomeRefused, "no_outbox")
	}
	recs, err := c.Outbox.List()
	if err != nil {
		return fail(OutcomeRefused, "outbox")
	}
	for _, rec := range recs {
		if rec.OperationID != operationID {
			continue
		}
		r, err := c.recordRequest(rec)
		if err != nil || rec.State != StateUnknown || r.Dedup == "" {
			return fail(OutcomeRefused, "not_spoolable")
		}
		authz := ""
		if c.Endpoint.Credential && c.Credential != nil {
			if authz, err = c.Credential(); err != nil {
				return fail(OutcomeRefused, "credential")
			}
		}
		lk, lerr := c.Outbox.lockRecord(rec)
		if lerr != nil {
			return fail(OutcomeRefused, "busy")
		}
		defer lk.Release()
		c.Outbox.audit(AuditEntry{Verb: "reconcile", Service: rec.Service, RecordVerb: rec.Verb, OperationID: operationID, Evidence: "reconcile with the same key", Observed: ObservedUnknown})
		res := c.sendRecord(ctx, rec, authz)
		res.Service, res.Verb = rec.Service, rec.Verb
		switch res.Outcome {
		case OutcomeOK:
			_ = c.Outbox.Remove(rec)
		case OutcomeInvalid, OutcomeNotFound:
			// Only a definitive answer about this key ends the record: the request
			// must have been sent. A refusal of the caller (401, 403) is a
			// refused outcome and is not in this case; a request that never left
			// (a bad stored path) says nothing about the original write.
			if res.Sent {
				_ = c.Outbox.SetState(rec, StateRefused)
			}
		}
		res.SpoolID = str(fileName(rec.Seq, rec.OperationID))
		return res
	}
	return fail(OutcomeNotFound, "not_found")
}

func isTLS(err error) bool {
	var cv *tls.CertificateVerificationError
	var rh tls.RecordHeaderError
	var ua x509.UnknownAuthorityError
	var he x509.HostnameError
	var ci x509.CertificateInvalidError
	return errors.Is(err, errPin) || errors.As(err, &cv) || errors.As(err, &rh) ||
		errors.As(err, &ua) || errors.As(err, &he) || errors.As(err, &ci)
}

func (c *Client) once(ctx context.Context, r Request, p Policy, authz string) Result {
	body := r.Body
	if !validPath(r.Path) {
		return c.result(r, OutcomeInvalid, false, "request")
	}
	req, err := http.NewRequestWithContext(ctx, r.Method, c.Endpoint.Origin+r.Path, io.NopCloser(bytes.NewReader(body)))
	if err != nil {
		return c.result(r, OutcomeInvalid, false, "request")
	}
	// Whatever the path holds, the request goes to the pinned origin and nowhere
	// else: this is the one place the credential is attached.
	if req.URL.Scheme+"://"+req.URL.Host != c.Endpoint.Origin || req.URL.User != nil {
		return c.result(r, OutcomeRefused, false, "origin")
	}
	// A NopCloser leaves GetBody unset, so net/http cannot replay the body on
	// its own: every request that reaches the server was counted here.
	req.ContentLength = int64(len(body))
	if len(body) == 0 {
		req.Body = http.NoBody
	}
	if authz != "" {
		req.Header.Set("Authorization", authz)
	}
	if !r.Read {
		req.Header.Set("Idempotency-Key", r.OperationID)
	}
	if e := r.Envelope; e != nil {
		req.Header.Set("Zeo-Envelope", e.Schema+" "+e.Audience+" "+base64.StdEncoding.EncodeToString(e.Body))
	}
	req.Header.Set("Content-Type", "application/json")
	sent := false
	// sent is false ONLY while the failure came before the request was handed
	// to an established connection: GotConn fires after the TLS handshake, so
	// handshake bytes are not request bytes, and any error at or after it (a
	// write error on the request line or the headers included) is sent.
	trace := &httptrace.ClientTrace{
		GotConn: func(httptrace.GotConnInfo) { sent = true },
	}
	req = req.WithContext(httptrace.WithClientTrace(ctx, trace))

	resp, err := c.httpClient(p).Do(req)
	if err != nil {
		return c.transportError(ctx, r, sent, err)
	}
	defer resp.Body.Close()
	raw, rerr := io.ReadAll(io.LimitReader(resp.Body, MaxResponse+1))
	return c.answer(ctx, r, resp.StatusCode, raw, rerr)
}

func (c *Client) transportError(ctx context.Context, r Request, sent bool, err error) Result {
	// A DNS failure is the default case below: nothing was sent.
	switch {
	case isTLS(err) && !sent:
		// A verification failure before the request was handed to a connection is
		// a refusal, not an outage: scripts read exit 6 as "try again". After
		// GotConn every error is sent (a bad record on a live connection included),
		// so it falls to the cases below and a write is dispatch_unknown.
		return c.result(r, OutcomeRefused, false, "tls")
	case errors.Is(ctx.Err(), context.DeadlineExceeded):
		switch {
		case sent && !r.Read:
			return c.result(r, OutcomeDispatchUnknown, true, "timeout_after_send")
		default:
			return c.result(r, OutcomeTimeout, sent, "timeout")
		}
	case sent && !r.Read:
		return c.result(r, OutcomeDispatchUnknown, true, "dispatch_unknown")
	default:
		return c.result(r, OutcomeUnavailable, sent, "unavailable")
	}
}

func (c *Client) answer(ctx context.Context, r Request, status int, raw []byte, readErr error) Result {
	switch {
	case status >= 300 && status < 400:
		if r.Read {
			return c.result(r, OutcomeUnavailable, true, "response_invalid")
		}
		return c.result(r, OutcomeDispatchUnknown, true, "redirect")
	case status == 401:
		return c.result(r, OutcomeRefused, true, "auth")
	case status == 403:
		return c.result(r, OutcomeRefused, true, "stopped")
	case status == 404:
		return c.result(r, OutcomeNotFound, true, "not_found")
	case status == 400 || status == 422:
		return c.result(r, OutcomeInvalid, true, "request")
	case status >= 200 && status < 300:
		if readErr != nil && errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return c.transportError(ctx, r, true, readErr)
		}
		doc, ok := parseAnswer(raw, readErr, r.ExpectSchema)
		if !ok {
			if r.Read {
				return c.result(r, OutcomeUnavailable, true, "response_invalid")
			}
			return c.result(r, OutcomeDispatchUnknown, true, "response_invalid")
		}
		res := c.result(r, OutcomeOK, true, "")
		res.Result = doc
		return res
	default:
		if r.Read {
			return c.result(r, OutcomeUnavailable, true, "server_error")
		}
		return c.result(r, OutcomeDispatchUnknown, true, "server_error")
	}
}

// parseAnswer accepts a JSON object that carries a schema id (and the one the
// caller expects, if any), no duplicate key, within the size bound.
func parseAnswer(raw []byte, readErr error, expect string) (*Payload, bool) {
	if readErr != nil || len(raw) == 0 || len(raw) > MaxResponse || noDuplicateKeys(raw) != nil {
		return nil, false
	}
	var head struct {
		Schema string `json:"schema"`
	}
	if err := json.Unmarshal(raw, &head); err != nil || head.Schema == "" || (expect != "" && head.Schema != expect) {
		return nil, false
	}
	return &Payload{Schema: head.Schema, Value: json.RawMessage(append([]byte(nil), raw...))}, true
}

// Doctor is what `doctor` prints about a service: the ORIGIN only, never a
// path, a credential or a server body.
type Doctor struct {
	Service        string
	Origin         string
	Source         Source
	CredentialPins bool
	Answers        bool
	Outbox         *Stats
	Dev            bool
	Binary         string
	Held           bool
}

// Doctor probes the service once with the probe policy.
func (c *Client) Doctor(ctx context.Context, service, healthPath, audience string) Doctor {
	d := Doctor{Service: service, Origin: c.Endpoint.Origin, Source: c.Endpoint.Source, CredentialPins: c.Endpoint.Credential, Dev: devBuildNow}
	d.Binary, _ = os.Executable()
	if c.Outbox != nil {
		if s, err := c.Outbox.Stats(); err == nil {
			d.Outbox = &s
		}
		d.Held = c.Outbox.Held()
	}
	if c.Endpoint.Local || c.Endpoint.Origin == "" || !validPath(healthPath) ||
		(c.Endpoint.Credential && (audience == "" || audience != c.Endpoint.Audience)) {
		return d
	}
	ctx, cancel := context.WithTimeout(ctx, Probe.Total)
	defer cancel()
	authz := ""
	if c.Endpoint.Credential && c.Credential != nil {
		authz, _ = c.Credential()
	}
	res := c.once(ctx, Request{Service: service, Verb: "doctor", Method: "GET", Path: healthPath, Read: true, Audience: audience}, Probe, authz)
	d.Answers = res.Outcome == OutcomeOK
	return d
}

// ReplyKey is the operation id of a reply: derived from the id of the message
// it answers, the same every time, so a reply spooled twice is one record and
// one effect at the service, even if the outbox is recreated in between.
func ReplyKey(messageID string) string { return "reply-" + messageID }

// ValidOperationID reports whether an operation id is acceptable.
func ValidOperationID(id string) bool { return idRe.MatchString(id) }
