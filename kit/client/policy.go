package client

import (
	"math/rand/v2"
	"time"
)

// Policy is a call's timeouts and retries. A configuration may lower any
// value and never raise it (Clamp).
type Policy struct {
	Connect time.Duration
	Total   time.Duration
	Backoff []time.Duration // one entry per retry
}

// The constants of the contract (SOW-35 section 10.3).
var (
	Read  = Policy{Connect: 3 * time.Second, Total: 10 * time.Second, Backoff: []time.Duration{200 * time.Millisecond, 600 * time.Millisecond}}
	Write = Policy{Connect: 3 * time.Second, Total: 15 * time.Second}
	Probe = Policy{Connect: 2 * time.Second, Total: 3 * time.Second}
)

// Jitter adds up to half of d, so retries from many clients do not align.
func Jitter(d time.Duration) time.Duration {
	if d <= 0 {
		return d
	}
	return d + time.Duration(rand.Int64N(int64(d/2)+1))
}

// Clamp returns want with every value capped at the contract's value p.
func (p Policy) Clamp(want Policy) Policy {
	out := p
	if want.Connect > 0 && want.Connect < p.Connect {
		out.Connect = want.Connect
	}
	if want.Total > 0 && want.Total < p.Total {
		out.Total = want.Total
	}
	if want.Backoff != nil && len(want.Backoff) < len(p.Backoff) {
		out.Backoff = p.Backoff[:len(want.Backoff)]
	}
	return out
}
