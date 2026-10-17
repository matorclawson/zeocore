package client

import (
	"encoding/json"

	"github.com/zeroemployeeorg/zeocore/kit"
)

// ResultSchema names the document a client call prints with --json.
const ResultSchema = "zeo.client-result/v1"

// Outcome is what a client call came to.
type Outcome string

// The seven outcomes.
const (
	OutcomeOK              Outcome = "ok"
	OutcomeRefused         Outcome = "refused"
	OutcomeUnavailable     Outcome = "unavailable"
	OutcomeTimeout         Outcome = "timeout"
	OutcomeDispatchUnknown Outcome = "dispatch_unknown"
	OutcomeSpooled         Outcome = "spooled"
	OutcomeInvalid         Outcome = "invalid"
)

// Result is the one document a call produces. Sent is false only when no byte
// of the request left this process. It carries no credential, no URL path and
// no input value.
type Result struct {
	Schema         string  `json:"schema"`
	Service        string  `json:"service"`
	Verb           string  `json:"verb"`
	Outcome        Outcome `json:"outcome"`
	Sent           bool    `json:"sent"`
	EndpointSource Source  `json:"endpoint_source"`
	OperationID    *string `json:"operation_id"`
	SpoolID        *string `json:"spool_id"`
	Reason         *string `json:"reason"`
}

func str(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// Exit is the zeo-cli/v1 exit code of the result (SOW-35 section 10.2). An
// unknown dispatch is exit 1, never 6, so that a script which treats 6 as
// "try again" cannot resend it; the outcome and the operation id carry the
// difference.
func (r Result) Exit() kit.ExitCode {
	switch r.Outcome {
	case OutcomeOK, OutcomeSpooled:
		return kit.ExitOK
	case OutcomeInvalid:
		return kit.ExitUsage
	case OutcomeRefused:
		return kit.ExitRefused
	case OutcomeTimeout:
		return kit.ExitTimeout
	case OutcomeUnavailable:
		return kit.ExitUnavailable
	default:
		return kit.ExitFailed
	}
}

// JSON is the document, one line.
func (r Result) JSON() []byte {
	r.Schema = ResultSchema
	b, _ := json.Marshal(r)
	return b
}
