package kit

// The JSON documents of rev d section 6. The field order is the order the
// document prints, so "schema" is always first. Fields are added, never
// renamed; a rename makes v2.

// Schema names the documents the kit itself prints.
const (
	SchemaHelp    = "zeo.help/v1"
	SchemaVersion = "zeo.version/v1"
	SchemaDoctor  = "zeo.doctor/v1"
	SchemaStatus  = "zeo.status/v1"
)

// Signature states a version document reports.
const (
	SignatureVerified      = "verified"
	SignatureUnsigned      = "unsigned"
	SignatureInvalid       = "invalid"
	SignatureUnknownSigner = "unknown_signer"
)

type helpFlag struct {
	Name       string `json:"name"`
	Summary    string `json:"summary"`
	TakesValue bool   `json:"takes_value"`
}

type helpCommand struct {
	Name    string     `json:"name"`
	Summary string     `json:"summary"`
	Args    string     `json:"args"`
	Flags   []helpFlag `json:"flags"`
	Output  string     `json:"output"`
	Exits   []int      `json:"exits"`
	Kit     bool       `json:"kit"`
}

type helpDoc struct {
	Schema   string        `json:"schema"`
	App      string        `json:"app"`
	Version  string        `json:"version"`
	Contract string        `json:"contract"`
	Commands []helpCommand `json:"commands"`
	Exits    []ExitInfo    `json:"exits"`
	Schemas  []string      `json:"schemas"`
}

type versionDoc struct {
	Schema    string `json:"schema"`
	App       string `json:"app"`
	Version   string `json:"version"`
	Commit    string `json:"commit"`
	BuiltAt   string `json:"built_at"`
	Signature string `json:"signature"`
	Signer    string `json:"signer"`
}

// Check is one prerequisite doctor ran. Skipped is additive on zeo.doctor/v1:
// a check that did not apply is neither a pass nor a failure.
type Check struct {
	Name    string `json:"name"`
	OK      bool   `json:"ok"`
	Skipped bool   `json:"skipped,omitempty"`
	Detail  string `json:"detail"`
	// Next names the next step when the check failed.
	Next string `json:"next"`
}

type doctorDoc struct {
	Schema string  `json:"schema"`
	App    string  `json:"app"`
	Ready  bool    `json:"ready"`
	Checks []Check `json:"checks"`
}

// Status is what an app reports about itself. Live is nil for an app that has
// no state it must not be restarted in.
type Status struct {
	// State is one of running, stopped, degraded, unknown.
	State   string
	Healthy bool
	// Live is true when the app is serving real work and must not be restarted.
	Live   *bool
	Detail string
	// Counts are the app's own numbers (zeocouncil: ready rows).
	Counts map[string]int
}

type statusDoc struct {
	Schema  string         `json:"schema"`
	App     string         `json:"app"`
	Version string         `json:"version"`
	State   string         `json:"state"`
	Healthy bool           `json:"healthy"`
	Live    *bool          `json:"live"`
	Detail  string         `json:"detail"`
	Counts  map[string]int `json:"counts"`
}

var validStates = map[string]bool{"running": true, "stopped": true, "degraded": true, "unknown": true}
