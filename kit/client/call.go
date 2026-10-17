package client

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptrace"
	"strings"
	"time"
)

// Request is one call.
type Request struct {
	Service     string
	Verb        string
	Method      string
	Path        string // starts with "/", no ".."
	Body        []byte
	OperationID string // required for a write: the idempotency key
	Read        bool
	// Spoolable says the owner admitted this write to the outbox. A write
	// that is not spoolable (a runtime nudge, start, stop, assignment) refuses
	// offline and is never spooled or retried.
	Spoolable bool
}

// Client calls one resolved remote endpoint.
type Client struct {
	Endpoint Endpoint
	HTTP     *http.Client
	// Credential returns the Authorization header value. It proves this host,
	// not a seat. Its value is never put in a Result or an error.
	Credential func() (string, error)
	Outbox     *Outbox
	Sleep      func(time.Duration)
}

// NewHTTP is the default HTTP client for p: https only, no redirects.
func NewHTTP(p Policy) *http.Client {
	return &http.Client{
		Transport: &http.Transport{
			DialContext:         (&net.Dialer{Timeout: p.Connect}).DialContext,
			TLSHandshakeTimeout: p.Connect,
		},
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
}

func (c *Client) result(r Request, o Outcome, sent bool, reason string) Result {
	return Result{
		Service: r.Service, Verb: r.Verb, Outcome: o, Sent: sent,
		EndpointSource: c.Endpoint.Source, OperationID: str(r.OperationID), Reason: str(reason),
	}
}

// Call performs r. A read fails closed: it never uses a cache and retries only
// a failure before any response byte. A write is never retried inside one
// call: it completes, is refused visibly, is spooled (when the owner admitted
// that), or comes back dispatch_unknown with its operation id retained.
func (c *Client) Call(ctx context.Context, r Request) Result {
	if c.Endpoint.Local || c.Endpoint.URL == "" {
		return c.result(r, OutcomeRefused, false, "endpoint")
	}
	// A path without its slash would also fail in NewRequest; the check is
	// kept so the refusal does not depend on that.
	if !strings.HasPrefix(r.Path, "/") || strings.Contains(r.Path, "..") || r.Method == "" {
		return c.result(r, OutcomeInvalid, false, "request")
	}
	if !r.Read && !idRe.MatchString(r.OperationID) {
		return c.result(r, OutcomeInvalid, false, "operation_id")
	}
	policy := Write
	if r.Read {
		policy = Read
	}
	sleep := c.Sleep
	if sleep == nil {
		sleep = time.Sleep
	}
	var authz string
	if c.Credential != nil {
		v, err := c.Credential()
		if err != nil {
			return c.result(r, OutcomeRefused, false, "credential")
		}
		authz = v
	}
	var last Result
	for attempt := 0; ; attempt++ {
		last = c.once(ctx, r, policy, authz)
		retry := attempt < len(policy.Backoff) &&
			(last.Outcome == OutcomeUnavailable || last.Outcome == OutcomeTimeout) && !last.Sent
		if !retry {
			break
		}
		sleep(policy.Backoff[attempt])
	}
	if r.Read {
		return last
	}
	return c.afterWrite(r, last)
}

func (c *Client) once(ctx context.Context, r Request, p Policy, authz string) Result {
	ctx, cancel := context.WithTimeout(ctx, p.Total)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, r.Method, c.Endpoint.URL+r.Path, bytes.NewReader(r.Body))
	if err != nil {
		return c.result(r, OutcomeInvalid, false, "request")
	}
	if authz != "" {
		req.Header.Set("Authorization", authz)
	}
	if !r.Read {
		req.Header.Set("Idempotency-Key", r.OperationID)
	}
	req.Header.Set("Content-Type", "application/json")
	sent := false
	trace := &httptrace.ClientTrace{
		WroteHeaders: func() { sent = true },
		WroteRequest: func(httptrace.WroteRequestInfo) { sent = true },
	}
	req = req.WithContext(httptrace.WithClientTrace(ctx, trace))
	h := c.HTTP
	if h == nil {
		h = NewHTTP(p)
	}
	resp, err := h.Do(req)
	if err != nil {
		timedOut := errors.Is(ctx.Err(), context.DeadlineExceeded)
		switch {
		case sent && !r.Read:
			return c.result(r, OutcomeDispatchUnknown, true, "dispatch_unknown")
		case timedOut:
			return c.result(r, OutcomeTimeout, sent, "timeout")
		default:
			return c.result(r, OutcomeUnavailable, sent, "unavailable")
		}
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
	switch {
	case resp.StatusCode >= 200 && resp.StatusCode < 300:
		return c.result(r, OutcomeOK, true, "")
	case resp.StatusCode == 401:
		return c.result(r, OutcomeRefused, true, "auth")
	case resp.StatusCode == 403:
		return c.result(r, OutcomeRefused, true, "stopped")
	case resp.StatusCode == 400 || resp.StatusCode == 422:
		return c.result(r, OutcomeInvalid, true, "request")
	case !r.Read:
		// A 5xx or an unexpected answer to a write: the service may have acted.
		return c.result(r, OutcomeDispatchUnknown, true, "dispatch_unknown")
	default:
		return c.result(r, OutcomeUnavailable, true, "unavailable")
	}
}

// afterWrite decides what a failed write becomes: spooled or refused when
// nothing was sent, retained as unknown when something may have been.
func (c *Client) afterWrite(r Request, res Result) Result {
	switch res.Outcome {
	case OutcomeUnavailable, OutcomeTimeout:
		if res.Sent {
			return res
		}
		if !r.Spoolable || c.Outbox == nil {
			return c.result(r, OutcomeRefused, false, "offline")
		}
		rec, err := c.Outbox.Enqueue(r.Service, r.Verb, r.OperationID, r.Body)
		switch {
		case errors.Is(err, ErrFull):
			return c.result(r, OutcomeRefused, false, "outbox_full")
		case err != nil:
			return c.result(r, OutcomeRefused, false, "outbox")
		}
		out := c.result(r, OutcomeSpooled, false, "")
		out.SpoolID = str(fileName(rec.Seq, rec.OperationID))
		return out
	case OutcomeDispatchUnknown:
		if c.Outbox != nil {
			if rec, err := c.Outbox.Enqueue(r.Service, r.Verb, r.OperationID, r.Body); err == nil {
				_ = c.Outbox.SetState(rec, StateUnknown)
			}
		}
		return res
	}
	return res
}
