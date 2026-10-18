package client

import (
	"errors"
	"net/url"
	"regexp"
	"strconv"
	"strings"
)

// devBuildNow says whether this binary was built with the zeodev tag. Only such
// a binary accepts an endpoint from the environment when there is no host file.
// It is a package variable only so that this package's tests can stand in either
// kind of build; it is not exported, so no importer can switch a release build.
var devBuildNow = devBuild

// IsDevBuild reports whether this is a zeodev build.
func IsDevBuild() bool { return devBuildNow }

// Source says where an endpoint came from.
type Source string

// The sources of an endpoint.
const (
	SourceEnv   Source = "env"
	SourceHost  Source = "host"
	SourceDev   Source = "dev"
	SourceLocal Source = "local"
)

// EndpointsSchema names the per-host config document.
const EndpointsSchema = "zeo.endpoints/v1"

// Endpoint is the resolved place of a service.
type Endpoint struct {
	Origin   string // https://host[:port], host lower-cased; empty when Local
	Local    bool
	Source   Source
	Audience string
	SPKI     string
	// Credential says the per-host credential may be attached: the origin is
	// pinned by the root-owned host file. It is never true in development.
	Credential bool
}

// Refusal is a resolution that refused. Message names the variable or the key
// and never the value it refused.
type Refusal struct {
	Reason  string
	Message string
}

func (r *Refusal) Error() string { return r.Message }

var serviceRe = regexp.MustCompile(`^[a-z][a-z0-9]*(-[a-z0-9]+)*$`)

// EnvName is the one override variable of a service: ZEO_<S>_ENDPOINT, with
// each hyphen written as an underscore.
func EnvName(service string) string {
	return "ZEO_" + strings.ToUpper(strings.ReplaceAll(service, "-", "_")) + "_ENDPOINT"
}

func validService(s string) bool { return len(s) <= 32 && serviceRe.MatchString(s) }

// Resolve finds the endpoint of service. host is the validated host file, or
// nil when it is absent (development).
//
// With a host file the entry decides. A variable is consulted only if the
// entry allows it, and then it can only select an origin the file lists (or
// local, if the file permits): a seat that plants a variable cannot choose
// where the credential goes. Without a host file a variable is accepted, the
// source is dev, and no credential is ever attached. There is no default and
// no fallback: a missing or malformed configuration never becomes local.
func Resolve(service string, getenv func(string) (string, bool), host *HostConfig) (Endpoint, error) {
	if !validService(service) {
		return Endpoint{}, &Refusal{"endpoint", "the service name is not valid"}
	}
	name := EnvName(service)
	if host == nil && !devBuildNow {
		// Nothing is consulted: a release build does not read the environment.
		return Endpoint{}, &Refusal{"not_configured", "no host config for " + service + ": a release build does not use " + name}
	}
	v, set := getenv(name)
	if host == nil {
		if !set {
			return Endpoint{}, &Refusal{"not_configured", "no endpoint for " + service + ": set " + name + " or install the host config"}
		}
		if v == "local" {
			return Endpoint{Local: true, Source: SourceDev}, nil
		}
		o, err := parseOrigin(v)
		if err != nil {
			return Endpoint{}, &Refusal{"endpoint", name + " is malformed"}
		}
		return Endpoint{Origin: o, Source: SourceDev}, nil
	}
	e, ok := host.Services[service]
	if !ok {
		return Endpoint{}, &Refusal{"not_configured", "no endpoint for " + service + " in the host config"}
	}
	spki := ""
	if e.SPKI != nil {
		spki = strings.ToLower(*e.SPKI)
	}
	if set && e.AllowEnv {
		if v == "local" {
			if !e.EnvLocal {
				return Endpoint{}, &Refusal{"origin", name + " names local, which the host config does not permit"}
			}
			return Endpoint{Local: true, Source: SourceEnv, Audience: e.Audience}, nil
		}
		o, err := parseOrigin(v)
		if err != nil {
			return Endpoint{}, &Refusal{"endpoint", name + " is malformed"}
		}
		if !listed(o, e) {
			return Endpoint{}, &Refusal{"origin", name + " names an origin the host config does not list"}
		}
		return Endpoint{Origin: o, Source: SourceEnv, Audience: e.Audience, SPKI: spki, Credential: true}, nil
	}
	if e.Local {
		return Endpoint{Local: true, Source: SourceHost, Audience: e.Audience}, nil
	}
	o, _ := parseOrigin(*e.Origin)
	return Endpoint{Origin: o, Source: SourceHost, Audience: e.Audience, SPKI: spki, Credential: true}, nil
}

// PrivilegedLocalAllowed says whether the tool may run the service's privileged
// --local verbs: never for a local binding that came from a development build.
func (e Endpoint) PrivilegedLocalAllowed() bool { return e.Local && e.Source != SourceDev }

func listed(origin string, e HostEntry) bool {
	if e.Origin != nil {
		if p, err := parseOrigin(*e.Origin); err == nil && p == origin {
			return true
		}
	}
	for _, o := range e.EnvOrigins {
		if p, err := parseOrigin(o); err == nil && p == origin {
			return true
		}
	}
	return false
}

// parseOrigin accepts https://host[:port] with printable ASCII and nothing
// else: originRe admits no userinfo, path, query, fragment or space. The host
// is lower-cased; nothing else is changed.
func parseOrigin(v string) (string, error) {
	if !originRe.MatchString(v) {
		return "", errors.New("not an origin")
	}
	u, err := url.Parse(v)
	if err != nil || u.Hostname() == "" || strings.HasSuffix(u.Host, ":") {
		return "", errors.New("not an origin")
	}
	if p := u.Port(); p != "" {
		if n, err := strconv.Atoi(p); err != nil || n < 1 || n > 65535 {
			return "", errors.New("bad port")
		}
	}
	return "https://" + strings.ToLower(u.Host), nil
}
