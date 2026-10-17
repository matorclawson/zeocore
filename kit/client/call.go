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
	Schema   string
	Audience string
	Body     []byte
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
	Spool           bool
	Dedup           string
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
	Sleep      func(time.Duration)
	Jitter     func(time.Duration) time.Duration
}

var errPin = errors.New("the server key does not match the pin")

func (c *Client) httpClient(p Policy) *http.Client {
	cfg := &tls.Config{RootCAs: c.Roots, MinVersion: tls.VersionTLS12}
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
		!strings.HasPrefix(r.Path, "/") || strings.Contains(r.Path, "..") || len(r.Body) > MaxRequest ||
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
			(res.Outcome == OutcomeUnavailable && *res.Reason != "tls" || res.Outcome == OutcomeTimeout && ctx.Err() == nil)
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
	rec, err := c.Outbox.Enqueue(r.Service, r.Verb, r.OperationID, StoredRequest{
		Method: r.Method, Path: r.Path, Audience: r.Audience, Dedup: r.Dedup, Body: r.Body,
	})
	switch {
	case errors.Is(err, ErrFull):
		return c.result(r, OutcomeRefused, false, "outbox_full")
	case errors.Is(err, ErrKeyConflict):
		return c.result(r, OutcomeRefused, false, "key_conflict")
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
	case StateRefused:
		return c.result(r, OutcomeRefused, false, "outbox")
	}
	res := c.once(ctx, r, p, authz)
	switch res.Outcome {
	case OutcomeDispatchUnknown:
		_ = c.Outbox.SetState(rec, StateUnknown)
		res.SpoolID = spool
	case OutcomeUnavailable, OutcomeTimeout:
		if res.Sent {
			break
		}
		if r.Spool && !r.RequireDelivery {
			out := c.result(r, OutcomeSpooled, false, "")
			out.SpoolID = spool
			return out
		}
		_ = c.Outbox.Remove(rec)
		switch {
		case r.Spool && r.RequireDelivery:
			return c.result(r, OutcomeUnavailable, false, "require_delivery")
		case res.Outcome == OutcomeUnavailable && *res.Reason == "unavailable":
			return c.result(r, OutcomeUnavailable, false, "offline")
		}
	default:
		// The service answered: the record has done its job.
		_ = c.Outbox.Remove(rec)
		if res.Outcome == OutcomeOK {
			c.drainAfter(ctx, r.Service, authz)
		}
	}
	return res
}

// drainAfter drains this service's queued records once, best effort, after a
// call to it succeeded.
func (c *Client) drainAfter(ctx context.Context, service, authz string) {
	recs, err := c.Outbox.List()
	if err != nil {
		return
	}
	for _, r := range recs {
		if r.State == StateQueued && r.Service == service {
			_, _ = c.drain(ctx, authz)
			return
		}
	}
}

// Drain sends the queued records. Only one drain runs at a time.
func (c *Client) Drain(ctx context.Context) (int, error) {
	authz := ""
	if c.Endpoint.Credential && c.Credential != nil {
		v, err := c.Credential()
		if err != nil {
			return 0, err
		}
		authz = v
	}
	return c.drain(ctx, authz)
}

func (c *Client) drain(ctx context.Context, authz string) (int, error) {
	return c.Outbox.Drain(func(rec Record) Result { return c.sendRecord(ctx, rec, authz) })
}

func (c *Client) recordRequest(rec Record) (Request, error) {
	var sr StoredRequest
	if err := strictDecode(rec.Request, &sr); err != nil {
		return Request{}, err
	}
	return Request{
		Service: rec.Service, Verb: rec.Verb, Method: sr.Method, Path: sr.Path, Body: sr.Body,
		OperationID: rec.OperationID, Audience: sr.Audience, Dedup: sr.Dedup,
	}, nil
}

func (c *Client) sendRecord(ctx context.Context, rec Record, authz string) Result {
	r, err := c.recordRequest(rec)
	if err != nil {
		return Result{Outcome: OutcomeInvalid, Reason: str("request")}
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
		res := c.sendRecord(ctx, rec, authz)
		res.Service, res.Verb = rec.Service, rec.Verb
		switch res.Outcome {
		case OutcomeOK:
			_ = c.Outbox.Remove(rec)
		case OutcomeRefused, OutcomeInvalid, OutcomeNotFound:
			_ = c.Outbox.SetState(rec, StateRefused)
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
	req, err := http.NewRequestWithContext(ctx, r.Method, c.Endpoint.Origin+r.Path, io.NopCloser(bytes.NewReader(body)))
	if err != nil {
		return c.result(r, OutcomeInvalid, false, "request")
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
	trace := &httptrace.ClientTrace{
		WroteHeaders: func() { sent = true },
		WroteRequest: func(httptrace.WroteRequestInfo) { sent = true },
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
	case isTLS(err):
		return c.result(r, OutcomeUnavailable, false, "tls")
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
}

// Doctor probes the service once with the probe policy.
func (c *Client) Doctor(ctx context.Context, service, healthPath, audience string) Doctor {
	d := Doctor{Service: service, Origin: c.Endpoint.Origin, Source: c.Endpoint.Source, CredentialPins: c.Endpoint.Credential}
	if c.Outbox != nil {
		if s, err := c.Outbox.Stats(); err == nil {
			d.Outbox = &s
		}
	}
	if c.Endpoint.Local || c.Endpoint.Origin == "" {
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
