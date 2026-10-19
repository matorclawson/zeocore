package client

import (
	"encoding/json"

	"github.com/zeroemployeeorg/zeocore/kit"
)

// ResultSchema names the document a client call prints with --json.
const ResultSchema = "zeo.client-result/v1"

// Outcome is what a client call came to.
type Outcome string

// The outcomes.
const (
	OutcomeOK              Outcome = "ok"
	OutcomeSpooled         Outcome = "spooled"
	OutcomeInvalid         Outcome = "invalid"
	OutcomeRefused         Outcome = "refused"
	OutcomeNotFound        Outcome = "not_found"
	OutcomeTimeout         Outcome = "timeout"
	OutcomeUnavailable     Outcome = "unavailable"
	OutcomeDispatchUnknown Outcome = "dispatch_unknown"
	OutcomeNotReady        Outcome = "not_ready"
)

// Reasons is the closed set of reason tokens per outcome. ok and spooled carry
// none.
var Reasons = map[Outcome][]string{
	OutcomeOK:      nil,
	OutcomeSpooled: nil,
	OutcomeInvalid: {"usage", "request", "operation_id"},
	OutcomeRefused: {"endpoint", "host_config", "not_configured", "origin", "credential", "auth", "stopped",
		"tls", "key_conflict", "not_spoolable", "outbox_full", "outbox", "no_outbox", "busy", "blocked"},
	OutcomeNotFound:        {"not_found"},
	OutcomeTimeout:         {"timeout"},
	OutcomeUnavailable:     {"unavailable", "offline", "require_delivery", "server_error", "response_invalid"},
	OutcomeDispatchUnknown: {"dispatch_unknown", "timeout_after_send", "server_error", "redirect", "response_invalid"},
	OutcomeNotReady:        {"not_ready"},
}

// Payload is the service's own document on ok, carried not replaced.
type Payload struct {
	Schema string          `json:"schema"`
	Value  json.RawMessage `json:"value"`
}

// Result is the one document a call produces. Sent is false only when no byte
// of the request left this process. It carries no credential, no URL, no path,
// no input value and no server body.
type Result struct {
	Schema         string   `json:"schema"`
	Service        string   `json:"service"`
	Verb           string   `json:"verb"`
	Outcome        Outcome  `json:"outcome"`
	Reason         *string  `json:"reason"`
	Sent           bool     `json:"sent"`
	Transport      string   `json:"transport"`
	EndpointSource Source   `json:"endpoint_source"`
	OperationID    *string  `json:"operation_id"`
	SpoolID        *string  `json:"spool_id"`
	Result         *Payload `json:"result"`
}

func str(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// Valid reports whether the reason is in the closed set for the outcome.
func (r Result) Valid() bool {
	allowed, ok := Reasons[r.Outcome]
	if !ok {
		return false
	}
	// A spooled result names its record and sent nothing; a dispatch_unknown
	// names its record and was sent.
	switch r.Outcome {
	case OutcomeSpooled:
		if r.SpoolID == nil || r.Sent {
			return false
		}
	case OutcomeDispatchUnknown:
		if r.SpoolID == nil || !r.Sent {
			return false
		}
	}
	if r.Reason == nil {
		return len(allowed) == 0
	}
	for _, a := range allowed {
		if a == *r.Reason {
			return true
		}
	}
	return false
}

// Exit is the zeo-cli/v1 exit code of the result. Exit 1 is reserved for
// ambiguity after send: dispatch_unknown is never 5 or 6, which scripts read
// as "try again" and which claim that nothing was done.
func (r Result) Exit() kit.ExitCode {
	switch r.Outcome {
	case OutcomeOK, OutcomeSpooled:
		return kit.ExitOK
	case OutcomeInvalid:
		return kit.ExitUsage
	case OutcomeRefused:
		return kit.ExitRefused
	case OutcomeNotFound:
		return kit.ExitNotFound
	case OutcomeTimeout:
		return kit.ExitTimeout
	case OutcomeUnavailable:
		return kit.ExitUnavailable
	case OutcomeNotReady:
		return kit.ExitNotReady
	default:
		return kit.ExitFailed
	}
}

// JSON is the document, one line.
func (r Result) JSON() []byte {
	r.Schema = ResultSchema
	if r.Transport == "" {
		r.Transport = "remote"
	}
	b, _ := json.Marshal(r)
	return b
}

// ParseResult reads a result document back, refusing unknown fields,
// duplicate keys and a reason outside the closed set.
func ParseResult(raw []byte) (Result, error) {
	var r Result
	if err := strictDecode(raw, &r); err != nil || r.Schema != ResultSchema || !r.Valid() {
		return Result{}, &Refusal{"request", "not a result document"}
	}
	return r, nil
}

// NotReady is the result of `doctor` when a prerequisite fails, for example a
// release build with no host config.
func NotReady(service string) Result {
	return Result{Service: service, Verb: "doctor", Outcome: OutcomeNotReady, Reason: str("not_ready"), Transport: "remote"}
}
