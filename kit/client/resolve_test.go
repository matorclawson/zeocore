package client

import (
	"io/fs"
	"strings"
	"testing"
)

const pinned = "https://a.zeo.ac"

func hostWith(t *testing.T, e string) *HostConfig { return loadHost(t, hostDoc(`{"a":`+e+`}`)) }

func allow(origin, envOrigins, local string) string {
	return `{"origin":"` + origin + `","local":false,"audience":"aud","spki_sha256":null,"allow_env":true,"env_origins":[` + envOrigins + `],"env_local":` + local + `}`
}

// R1: a variable cannot beat the root-owned file by being set.
func TestR1VariableIsIgnoredWhenNotAllowed(t *testing.T) {
	ep, err := Resolve("a", env(map[string]string{"ZEO_A_ENDPOINT": "https://evil.example"}), hostWith(t, entry(pinned, "")))
	if err != nil || ep.Origin != pinned || ep.Source != SourceHost || !ep.Credential {
		t.Fatalf("%+v %v", ep, err)
	}
}

// R2: an allowed variable can only select an origin the file lists.
func TestR2AnUnlistedOriginRefuses(t *testing.T) {
	h := hostWith(t, allow(pinned, `"https://b.zeo.ac"`, "false"))
	_, err := Resolve("a", env(map[string]string{"ZEO_A_ENDPOINT": "https://evil.example"}), h)
	assertRefusal(t, err, "origin")
}

// R3: a listed origin is accepted and keeps the credential; the audience rule still holds.
func TestR3AListedOriginKeepsTheCredential(t *testing.T) {
	h := hostWith(t, allow(pinned, `"https://b.zeo.ac"`, "false"))
	ep, err := Resolve("a", env(map[string]string{"ZEO_A_ENDPOINT": "https://B.zeo.ac"}), h)
	if err != nil || ep.Origin != "https://b.zeo.ac" || ep.Source != SourceEnv || !ep.Credential {
		t.Fatalf("%+v %v", ep, err)
	}
	f := newFake(t, nil)
	c := f.client(outbox(t))
	c.Endpoint = ep
	c.Endpoint.Origin = f.origin()
	r := readReq()
	r.Audience = "someone-else"
	if res := call(c, r); res.Outcome != OutcomeRefused || *res.Reason != "origin" || f.count() != 0 {
		t.Fatalf("%+v hits %d", res, f.count())
	}
}

// R4: local from the environment needs the file's permission.
func TestR4LocalFromTheEnvironmentNeedsPermission(t *testing.T) {
	_, err := Resolve("a", env(map[string]string{"ZEO_A_ENDPOINT": "local"}), hostWith(t, allow(pinned, "", "false")))
	assertRefusal(t, err, "origin")
	ep, err := Resolve("a", env(map[string]string{"ZEO_A_ENDPOINT": "local"}), hostWith(t, allow(pinned, "", "true")))
	if err != nil || !ep.Local || ep.Credential {
		t.Fatalf("%+v %v", ep, err)
	}
}

// R5: with no host file a variable is accepted, as dev, and never carries the credential.
func TestR5DevelopmentTakesTheVariableWithoutACredential(t *testing.T) {
	ep, err := Resolve("a", env(map[string]string{"ZEO_A_ENDPOINT": "https://x.zeo.ac"}), nil)
	if err != nil || ep.Source != SourceDev || ep.Credential {
		t.Fatalf("%+v %v", ep, err)
	}
	f := newFake(t, nil)
	c := f.client(nil)
	c.Endpoint = Endpoint{Origin: f.origin(), Source: SourceDev}
	if res := call(c, readReq()); res.Outcome != OutcomeOK {
		t.Fatalf("%+v", res)
	}
	if got := f.auth[0]; got != "" {
		t.Fatalf("a dev call sent a credential: %q", got)
	}
	ep, err = Resolve("a", env(map[string]string{"ZEO_A_ENDPOINT": "local"}), nil)
	if err != nil || !ep.Local || ep.Source != SourceDev {
		t.Fatalf("%+v %v", ep, err)
	}
}

// R6: nothing configured is a refusal, never local.
func TestR6NotConfigured(t *testing.T) {
	ep, err := Resolve("a", env(nil), nil)
	assertRefusal(t, err, "not_configured")
	if ep.Local {
		t.Fatal("never local")
	}
	_, err = Resolve("zzz", env(map[string]string{"ZEO_ZZZ_ENDPOINT": "https://x.zeo.ac"}), hostWith(t, entry(pinned, "")))
	assertRefusal(t, err, "not_configured")
}

func TestAnEmptyOrMalformedVariableRefusesInDev(t *testing.T) {
	for _, v := range []string{"", "LOCAL", " local", "http://h", "https://u:p@h", "https://h/p", "https://h/", "https://h?q", "https://h#f", "https://h x", "https://héllo", "HTTP://h", "https://", "https://h:99999x"} {
		_, err := Resolve("a", env(map[string]string{"ZEO_A_ENDPOINT": v}), nil)
		assertRefusal(t, err, "endpoint")
	}
}

func hostLoad(t *testing.T, tree map[string]fent) error {
	_, err := LoadHost(fakeFS(tree), hostPath)
	return err
}

// R7 to R12: the host file's own checks are enforced, and a failure refuses.
func TestR7ToR12HostFileChecks(t *testing.T) {
	doc := hostDoc(`{"a":` + entry(pinned, "") + `}`)
	cases := map[string]func(m map[string]fent){
		"R7 wrong owner":        func(m map[string]fent) { e := m[hostPath]; e.uid = 501; m[hostPath] = e },
		"R8 group-writable":     func(m map[string]fent) { e := m[hostPath]; e.mode = 0o664; m[hostPath] = e },
		"R8b other-writable":    func(m map[string]fent) { e := m[hostPath]; e.mode = 0o646; m[hostPath] = e },
		"R9 parent owned":       func(m map[string]fent) { e := m["/etc/zeo"]; e.uid = 501; m["/etc/zeo"] = e },
		"R9b parent writable":   func(m map[string]fent) { e := m["/etc"]; e.mode = 0o775; m["/etc"] = e },
		"R10 symlink":           func(m map[string]fent) { e := m["/etc"]; e.mode = 0o755 | fs.ModeSymlink; m["/etc"] = e },
		"R11 not regular":       func(m map[string]fent) { m[hostPath] = fent{mode: 0o755, dir: true, data: doc} },
		"R11b parent is a file": func(m map[string]fent) { m["/etc/zeo"] = fent{mode: 0o644, data: "x"} },
		"R12 oversize": func(m map[string]fent) {
			e := m[hostPath]
			e.data = doc + strings.Repeat(" ", MaxHostFile)
			m[hostPath] = e
		},
		"R13 unknown key": func(m map[string]fent) {
			e := m[hostPath]
			e.data = strings.Replace(doc, `"audience"`, `"extra":1,"audience"`, 1)
			m[hostPath] = e
		},
		"R13b unknown top key": func(m map[string]fent) {
			e := m[hostPath]
			e.data = strings.Replace(doc, `"services"`, `"x":1,"services"`, 1)
			m[hostPath] = e
		},
		"R14 duplicate key": func(m map[string]fent) {
			e := m[hostPath]
			e.data = strings.Replace(doc, `"audience":"aud"`, `"audience":"aud","audience":"evil"`, 1)
			m[hostPath] = e
		},
		"R15 malformed": func(m map[string]fent) { e := m[hostPath]; e.data = `{`; m[hostPath] = e },
		"R15b wrong schema": func(m map[string]fent) {
			e := m[hostPath]
			e.data = strings.Replace(doc, "endpoints/v1", "endpoints/v2", 1)
			m[hostPath] = e
		},
		"R15c trailing data": func(m map[string]fent) { e := m[hostPath]; e.data = doc + `{}`; m[hostPath] = e },
		"bundle not root":    func(m map[string]fent) { e := m["/etc/zeo/ca.pem"]; e.uid = 501; m["/etc/zeo/ca.pem"] = e },
		"entry with neither": func(m map[string]fent) {
			e := m[hostPath]
			e.data = hostDoc(`{"a":{"origin":null,"local":false,"audience":"aud"}}`)
			m[hostPath] = e
		},
		"entry with both": func(m map[string]fent) {
			e := m[hostPath]
			e.data = hostDoc(`{"a":{"origin":"` + pinned + `","local":true,"audience":"aud"}}`)
			m[hostPath] = e
		},
		"origin without aud": func(m map[string]fent) {
			e := m[hostPath]
			e.data = hostDoc(`{"a":{"origin":"` + pinned + `","local":false}}`)
			m[hostPath] = e
		},
		"bad env origin": func(m map[string]fent) {
			e := m[hostPath]
			e.data = hostDoc(`{"a":` + allow(pinned, `"http://x"`, "false") + `}`)
			m[hostPath] = e
		},
	}
	for name, mutate := range cases {
		tree := goodTree(doc)
		mutate(tree)
		err := hostLoad(t, tree)
		r, ok := err.(*Refusal)
		if !ok || r.Reason != "host_config" {
			t.Errorf("%s: want host_config, got %#v", name, err)
		}
	}
	if err := hostLoad(t, goodTree(doc)); err != nil {
		t.Fatalf("the good tree should load: %v", err)
	}
}

func TestAnAbsentHostFileIsNotAnError(t *testing.T) {
	tree := goodTree("")
	delete(tree, hostPath)
	h, err := LoadHost(fakeFS(tree), hostPath)
	if h != nil || err != nil {
		t.Fatalf("%v %v", h, err)
	}
}

func TestTheHostPathMustBeAbsoluteAndClean(t *testing.T) {
	for _, p := range []string{"etc/zeo/x.json", "/etc/zeo/../zeo/x.json", "/etc//zeo/x.json"} {
		if _, err := LoadHost(fakeFS(goodTree("")), p); err == nil {
			t.Errorf("%q should refuse", p)
		}
	}
}

// R15: a broken file is never bypassed by a valid variable.
func TestR15ABrokenFileIsNotBypassedByAVariable(t *testing.T) {
	tree := goodTree(`{`)
	_, err := LoadHost(fakeFS(tree), hostPath)
	assertRefusal(t, err, "host_config")
}

// R16: two services that give one variable name refuse the whole file.
func TestR16VariableNameCollision(t *testing.T) {
	// serviceRe forbids the characters that could collide, so the guard is the
	// closed grammar; a second check rejects equal variables defensively.
	for _, n := range []string{"zeo_messenger", "zeo.messenger", "Zeo-messenger", "a--b", "-a", "a-", "", strings.Repeat("a", 33)} {
		if validService(n) {
			t.Errorf("%q should be invalid", n)
		}
	}
	doc := hostDoc(`{"zeo_messenger":` + entry(pinned, "") + `}`)
	assertRefusal(t, hostLoad(t, goodTree(doc)), "host_config")
	if EnvName("zeo-messenger") != "ZEO_ZEO_MESSENGER_ENDPOINT" {
		t.Fatal(EnvName("zeo-messenger"))
	}
}

// R17: service names are a closed grammar.
func TestR17ServiceNames(t *testing.T) {
	anyValue := func(string) (string, bool) { return "https://x.zeo.ac", true }
	for _, bad := range []string{"", "Zmsg", "-x", "a b", "a.b", "a_b", strings.Repeat("a", 33)} {
		_, err := Resolve(bad, anyValue, nil)
		assertRefusal(t, err, "endpoint")
	}
}

// R18: an upper-case host equals its lower-case spelling.
func TestR18HostCaseInsensitive(t *testing.T) {
	ep, err := Resolve("a", env(map[string]string{"ZEO_A_ENDPOINT": "https://H.Zeo.AC:8443"}), nil)
	if err != nil || ep.Origin != "https://h.zeo.ac:8443" {
		t.Fatalf("%+v %v", ep, err)
	}
}

// R19: no refusal echoes the value.
func TestR19ARefusalNeverEchoesTheValue(t *testing.T) {
	v := "https://user:hunter2@h.example/"
	_, err := Resolve("a", env(map[string]string{"ZEO_A_ENDPOINT": v}), nil)
	assertRefusal(t, err, "endpoint")
	if strings.Contains(err.Error(), "hunter2") || strings.Contains(err.Error(), "h.example") || !strings.Contains(err.Error(), "ZEO_A_ENDPOINT") {
		t.Fatalf("%q", err)
	}
	_, err = Resolve("a", env(map[string]string{"ZEO_A_ENDPOINT": "https://leak.example"}), hostWith(t, allow(pinned, `"https://b.zeo.ac"`, "false")))
	assertRefusal(t, err, "origin")
	if strings.Contains(err.Error(), "leak.example") {
		t.Fatalf("%q", err)
	}
}

func TestTheHostPathIsCompiledIn(t *testing.T) {
	if p := HostPath(); !strings.HasPrefix(p, "/") || strings.Contains(p, "/etc/../") {
		t.Fatal(p)
	}
}

func TestRootsAreBuiltFromTheBundleOnly(t *testing.T) {
	f := newFake(t, nil)
	pem := string(pemOf(f))
	tree := goodTree(hostDoc(`{"a":` + entry(pinned, "") + `}`))
	e := tree["/etc/zeo/ca.pem"]
	e.data = pem
	tree["/etc/zeo/ca.pem"] = e
	fsys := fakeFS(tree)
	h, err := LoadHost(fsys, hostPath)
	if err != nil {
		t.Fatal(err)
	}
	pool, err := h.Roots(fsys)
	if err != nil || pool == nil {
		t.Fatalf("%v %v", pool, err)
	}
	e.data = "not a certificate"
	tree["/etc/zeo/ca.pem"] = e
	if _, err := h.Roots(fsys); err == nil {
		t.Fatal("a bundle with no certificate should refuse")
	}
}

func TestAPlatformWhoseVerifierReadsTheEnvironmentNeedsABundle(t *testing.T) {
	old := platformIgnoresEnv
	t.Cleanup(func() { platformIgnoresEnv = old })
	platformIgnoresEnv = false
	tree := goodTree(`{"schema":"zeo.endpoints/v1","ca_bundle":null,"services":{"a":` + entry(pinned, "") + `}}`)
	assertRefusal(t, hostLoad(t, tree), "host_config")
	platformIgnoresEnv = true
	if err := hostLoad(t, tree); err != nil {
		t.Fatal(err)
	}
	h, err := LoadHost(fakeFS(tree), hostPath)
	if pool, err2 := h.Roots(fakeFS(tree)); err != nil || err2 != nil || pool != nil {
		t.Fatalf("no bundle means the platform verifier: %v %v %v", pool, err, err2)
	}
}

func TestPortsAreChecked(t *testing.T) {
	for _, v := range []string{"https://h:0", "https://h:65536", "https://h:99999x", "https://h:-1", "https://h:"} {
		if _, err := Resolve("a", env(map[string]string{"ZEO_A_ENDPOINT": v}), nil); err == nil {
			t.Errorf("%q should refuse", v)
		}
	}
	if ep, err := Resolve("a", env(map[string]string{"ZEO_A_ENDPOINT": "https://h:65535"}), nil); err != nil || ep.Origin != "https://h:65535" {
		t.Fatalf("%+v %v", ep, err)
	}
}
