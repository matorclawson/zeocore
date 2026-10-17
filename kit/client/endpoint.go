package client

import (
	"bytes"
	"encoding/json"
	"errors"
	"io/fs"
	"net/url"
	"regexp"
	"strings"
)

// Source says where an endpoint came from.
type Source string

// The three sources of an endpoint.
const (
	SourceEnv   Source = "env"
	SourceHost  Source = "host"
	SourceLocal Source = "local"
)

// EndpointsSchema names the per-host config document.
const EndpointsSchema = "zeo.endpoints/v1"

// Endpoint is the resolved place of a service: an https URL, or Local, which
// means the caller runs the service's own --local verb.
type Endpoint struct {
	URL    string
	Local  bool
	Source Source
}

// Refusal is a resolution that refused. Message names the variable or the key
// and never the value it refused.
type Refusal struct {
	Reason  string // "endpoint" or "not_configured"
	Message string
}

func (r *Refusal) Error() string { return r.Message }

var serviceRe = regexp.MustCompile(`^[a-z][a-z0-9-]{0,31}$`)

// EnvName is the one override variable of a service: ZEO_<S>_ENDPOINT, with
// every character that is not a letter or a digit written as an underscore.
func EnvName(service string) string {
	var b strings.Builder
	for _, r := range strings.ToUpper(service) {
		if r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' {
			b.WriteRune(r)
		} else {
			b.WriteByte('_')
		}
	}
	return "ZEO_" + b.String() + "_ENDPOINT"
}

// Resolve finds the endpoint of service. A variable that is SET, even empty,
// wins and the host file is then not read; with none, the host file's entry
// is used; with neither, it refuses: there is no default and no fallback, and
// in particular a missing or malformed configuration never becomes "local".
func Resolve(service string, getenv func(string) (string, bool), hostFile func() ([]byte, error)) (Endpoint, error) {
	if !serviceRe.MatchString(service) {
		return Endpoint{}, &Refusal{"endpoint", "the service name is not valid"}
	}
	name := EnvName(service)
	if v, ok := getenv(name); ok {
		ep, err := parseValue(v, SourceEnv)
		if err != nil {
			return Endpoint{}, &Refusal{"endpoint", name + " is malformed"}
		}
		return ep, nil
	}
	raw, err := hostFile()
	if errors.Is(err, fs.ErrNotExist) {
		return Endpoint{}, &Refusal{"not_configured", "no endpoint for " + service + ": set " + name + " or the host config"}
	}
	if err != nil {
		return Endpoint{}, &Refusal{"endpoint", "the host endpoint config cannot be read"}
	}
	var doc struct {
		Schema   string            `json:"schema"`
		Services map[string]string `json:"services"`
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&doc); err != nil || doc.Schema != EndpointsSchema || dec.More() {
		return Endpoint{}, &Refusal{"endpoint", "the host endpoint config is malformed"}
	}
	v, ok := doc.Services[service]
	if !ok {
		return Endpoint{}, &Refusal{"not_configured", "no endpoint for " + service + ": set " + name + " or the host config"}
	}
	ep, err := parseValue(v, SourceHost)
	if err != nil {
		return Endpoint{}, &Refusal{"endpoint", "the host config entry for " + service + " is malformed"}
	}
	return ep, nil
}

// parseValue accepts the exact word local, or an https URL with a host and no
// userinfo, query, fragment, space or control character. Nothing is trimmed
// or case-folded.
func parseValue(v string, src Source) (Endpoint, error) {
	if v == "local" {
		return Endpoint{Local: true, Source: src}, nil
	}
	if !strings.HasPrefix(v, "https://") {
		return Endpoint{}, errors.New("not https")
	}
	for i := 0; i < len(v); i++ {
		if v[i] <= ' ' || v[i] >= 0x7f || v[i] == '?' || v[i] == '#' {
			return Endpoint{}, errors.New("bad character")
		}
	}
	u, err := url.Parse(v)
	if err != nil || u.Host == "" || u.User != nil || u.Hostname() == "" {
		return Endpoint{}, errors.New("bad url")
	}
	return Endpoint{URL: strings.TrimRight(v, "/"), Source: src}, nil
}
