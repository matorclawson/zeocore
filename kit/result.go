package kit

import (
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// Outcome is the verdict a RESULT line carries (rev d section 5).
type Outcome string

// The five outcomes of a state-changing verb, and what each means for a retry.
const (
	OutcomeOK               Outcome = "ok"                // done and verified
	OutcomeRefused          Outcome = "refused"           // a guard failed before any change
	OutcomeFailed           Outcome = "failed"            // failed, nothing changed
	OutcomeUndone           Outcome = "undone"            // failed after the switch and went back, verified
	OutcomeRecoveryRequired Outcome = "recovery_required" // failed after the switch and could not go back; do not retry
)

// Exit is the exit code an outcome ends the process with. Every failure
// outcome but a refusal is exit 1, so the table stays closed; the outcome
// is what differs, in the RESULT line.
func (o Outcome) Exit() ExitCode {
	switch o {
	case OutcomeOK:
		return ExitOK
	case OutcomeRefused:
		return ExitRefused
	default:
		return ExitFailed
	}
}

// SafeToRetry reports whether asking the same again is safe. Only
// recovery_required is not: its state is unknown until `recover` proves it.
func (o Outcome) SafeToRetry() bool { return o != OutcomeRecoveryRequired }

func (o Outcome) valid() bool {
	switch o {
	case OutcomeOK, OutcomeRefused, OutcomeFailed, OutcomeUndone, OutcomeRecoveryRequired:
		return true
	}
	return false
}

// Pair is one key=value total on a report verb's RESULT line, in order.
type Pair struct{ Key, Value string }

// Result is one RESULT line:
//
//	RESULT <app> <verb> <outcome> from=<v> to=<v> [reason=<token>] [undo="<command>"]
type Result struct {
	App     string
	Verb    string
	Outcome Outcome
	From    string
	To      string
	// Reason is one lowercase token, present unless the outcome is ok.
	Reason string
	// Undo is the command that reverses a change; quoted on the line.
	Undo string
	// Totals are the key=value pairs of a report verb (rev d section 12).
	Totals []Pair
}

var (
	wordRe   = regexp.MustCompile(`^[^\s"=]+$`)
	reasonRe = regexp.MustCompile(`^[a-z][a-z0-9_]*$`)
	keyRe    = regexp.MustCompile(`^[a-z][a-z0-9_]*$`)
)

// Line renders the RESULT line, or refuses a result that breaks the grammar,
// so a malformed line can never reach a reader.
func (r Result) Line() (string, error) {
	if !wordRe.MatchString(r.App) || !wordRe.MatchString(r.Verb) {
		return "", errors.New("kit: RESULT app and verb must be one word each")
	}
	if !r.Outcome.valid() {
		return "", fmt.Errorf("kit: RESULT outcome %q is not one of ok, refused, failed, undone, recovery_required", r.Outcome)
	}
	switch {
	case r.Outcome == OutcomeOK && r.Reason != "":
		return "", errors.New("kit: RESULT ok carries no reason")
	case r.Outcome != OutcomeOK && r.Reason == "":
		return "", fmt.Errorf("kit: RESULT %s needs a reason token", r.Outcome)
	case r.Reason != "" && !reasonRe.MatchString(r.Reason):
		return "", errors.New("kit: RESULT reason must be one lowercase token")
	}
	var b strings.Builder
	fmt.Fprintf(&b, "RESULT %s %s %s", r.App, r.Verb, r.Outcome)
	for _, f := range []struct{ k, v string }{{"from", r.From}, {"to", r.To}} {
		if f.v == "" {
			continue
		}
		if !wordRe.MatchString(f.v) {
			return "", fmt.Errorf("kit: RESULT %s must be one word", f.k)
		}
		fmt.Fprintf(&b, " %s=%s", f.k, f.v)
	}
	if r.Reason != "" {
		fmt.Fprintf(&b, " reason=%s", r.Reason)
	}
	for _, p := range r.Totals {
		if !keyRe.MatchString(p.Key) || !wordRe.MatchString(p.Value) {
			return "", fmt.Errorf("kit: RESULT total %q must be key=value with one-word parts", p.Key)
		}
		fmt.Fprintf(&b, " %s=%s", p.Key, p.Value)
	}
	if r.Undo != "" {
		if strings.ContainsAny(r.Undo, "\n\r") {
			return "", errors.New("kit: RESULT undo must be one line")
		}
		fmt.Fprintf(&b, " undo=%s", strconv.Quote(r.Undo))
	}
	return b.String(), nil
}
