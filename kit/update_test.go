package kit_test

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/zeroemployeeorg/zeocore/kit"
	"github.com/zeroemployeeorg/zeocore/kit/internal/sampleapp"
)

// fakeApp is an app with a service: every hook can be told to fail, and every
// call is recorded, so the order of the steps can be asserted.
type fakeApp struct {
	t       *testing.T
	root    string
	state   string
	stage   string
	rels    map[string]kit.Release
	calls   []string
	fail    map[string]int // hook name -> fail the next N calls
	running string         // the version the "service" believes it runs
	live    bool
	fence   bool
	migrate map[string]bool
	down    bool
	prot    string
	touch   bool
	now     time.Time
	// touchIn names hooks that rewrite a protected file when they run;
	// statusErr makes Status fail; asIsOK lets Ready take a zero t0 (recover
	// proving what already runs).
	touchIn   map[string]bool
	statusErr bool
	asIsOK    bool
}

func newFake2(t *testing.T) *fakeApp {
	f := &fakeApp{t: t, root: filepath.Join(t.TempDir(), "root"), state: t.TempDir(), stage: t.TempDir(), fail: map[string]int{}, rels: map[string]kit.Release{}, migrate: map[string]bool{}, now: time.Unix(1_000_000, 0)}
	f.prot = filepath.Join(t.TempDir(), "keys")
	_ = os.MkdirAll(f.prot, 0o700)
	_ = os.WriteFile(filepath.Join(f.prot, "approver.key"), []byte("secret"), 0o600)
	return f
}

func (f *fakeApp) release(v string, migrates bool) {
	dir := filepath.Join(f.stage, v)
	_ = os.MkdirAll(dir, 0o755)
	_ = os.WriteFile(filepath.Join(dir, "VERSION"), []byte(v), 0o644)
	f.rels[v] = kit.Release{Version: v, Dir: dir, SHA256: "x", Migrates: migrates}
}

func (f *fakeApp) hit(name string) error {
	f.calls = append(f.calls, name)
	if f.touchIn[name] {
		_ = os.WriteFile(filepath.Join(f.prot, "approver.key"), []byte("changed by "+name), 0o600)
	}
	if n := f.fail[name]; n > 0 {
		f.fail[name] = n - 1
		return errors.New(name + " failed")
	}
	return nil
}

func (f *fakeApp) app() *kit.App {
	app := sampleapp.New(false)
	live := f.live
	app.Status = func(context.Context) (kit.Status, error) {
		if f.statusErr {
			return kit.Status{}, errors.New("status is down")
		}
		return kit.Status{State: "running", Healthy: true, Live: &live}, nil
	}
	app.StateDir = f.state
	app.Subjects = []kit.Subject{{
		Name: "rules",
		Plan: func(context.Context) ([]kit.Change, error) {
			return []kit.Change{{Target: "t", Before: "a", After: "b"}}, nil
		},
		Read:  func(context.Context, []string) (map[string]string, error) { return map[string]string{"t": "a"}, nil },
		Apply: func(context.Context, []kit.Change) error { f.calls = append(f.calls, "apply"); return nil },
	}}
	u := &kit.Updater{
		Root: f.root, Protected: []string{f.prot}, Now: func() time.Time { return f.now },
		Fetch: func(_ context.Context, to string) (kit.Release, error) {
			if err := f.hit("fetch"); err != nil {
				return kit.Release{}, err
			}
			if to == "" {
				to = "2.0.0"
			}
			r, ok := f.rels[to]
			if !ok {
				return kit.Release{}, errors.New("no such release")
			}
			return r, nil
		},
		Preflight: func(context.Context, string, kit.Release) error { return f.hit("preflight") },
		Install: func(_ context.Context, rel kit.Release, dest string) error {
			if err := f.hit("install"); err != nil {
				return err
			}
			if f.touch {
				_ = os.WriteFile(filepath.Join(f.prot, "approver.key"), []byte("rewritten"), 0o600)
			}
			b, _ := os.ReadFile(filepath.Join(rel.Dir, "VERSION"))
			return os.WriteFile(filepath.Join(dest, "VERSION"), b, 0o644)
		},
		Verify: func(_ context.Context, dir string) error {
			if err := f.hit("verify"); err != nil {
				return err
			}
			b, err := os.ReadFile(filepath.Join(dir, "VERSION"))
			if err != nil || string(b) != f.running {
				return errors.New("the running version is not the one in " + dir)
			}
			return nil
		},
		Stop: func(context.Context) error { return f.hit("stop") },
		Start: func(context.Context) error {
			if err := f.hit("start"); err != nil {
				return err
			}
			b, err := os.ReadFile(filepath.Join(f.root, "current", "VERSION"))
			if err != nil {
				return err
			}
			f.running = string(b)
			return nil
		},
		Ready: func(_ context.Context, t0 time.Time, v string) error {
			if err := f.hit("ready"); err != nil {
				return err
			}
			if f.running != v || (t0.IsZero() && !f.asIsOK) {
				return errors.New("not ready as " + v)
			}
			return nil
		},
	}
	if f.down {
		u.Down = func(context.Context, string, string) error { return f.hit("down") }
	}
	if f.fence {
		u.Fence = func(context.Context) (func(), error) {
			if err := f.hit("fence"); err != nil {
				return nil, err
			}
			return func() { f.calls = append(f.calls, "unfence") }, nil
		}
	}
	u.LiveReason = "studio_live"
	app.Updater = u
	return app
}

func (f *fakeApp) current() string {
	target, err := os.Readlink(filepath.Join(f.root, "current"))
	if err != nil {
		return ""
	}
	return filepath.Base(target)
}

type upd struct {
	code    int
	doc     map[string]any
	result  string
	stdout  string
	stderr  string
	outcome string
	reason  string
}

func (f *fakeApp) do(args ...string) upd {
	f.t.Helper()
	f.calls = nil
	var out, errb strings.Builder
	code := f.app().Run(context.Background(), append(args, "--json"), &sbWriter{&out}, &sbWriter{&errb})
	u := upd{code: code, stdout: out.String(), stderr: errb.String()}
	_ = json.Unmarshal([]byte(u.stdout), &u.doc)
	for _, l := range strings.Split(strings.TrimSpace(u.stderr), "\n") {
		if strings.HasPrefix(l, "RESULT ") {
			u.result = l
		}
	}
	u.outcome, _ = u.doc["outcome"].(string)
	u.reason, _ = u.doc["reason"].(string)
	return u
}

type sbWriter struct{ b *strings.Builder }

func (w *sbWriter) Write(p []byte) (int, error) { return w.b.Write(p) }

func (f *fakeApp) install1() {
	f.t.Helper()
	f.release("1.0.0", false)
	f.release("2.0.0", false)
	if u := f.do("update", "--to", "1.0.0"); u.code != 0 || f.current() != "1.0.0" || f.running != "1.0.0" {
		f.t.Fatalf("first install: %+v current %q", u, f.current())
	}
}

func (f *fakeApp) ran(want ...string) {
	f.t.Helper()
	if strings.Join(f.calls, ",") != strings.Join(want, ",") {
		f.t.Errorf("hooks ran %v, want %v", f.calls, want)
	}
}

func TestUpdateSteps(t *testing.T) {
	f := newFake2(t)
	f.install1()
	u := f.do("update")
	if u.code != 0 || u.outcome != "ok" || f.current() != "2.0.0" || f.running != "2.0.0" {
		t.Fatalf("update: %+v current %s", u, f.current())
	}
	f.ran("fetch", "preflight", "install", "stop", "start", "ready", "verify")
	if !strings.HasPrefix(u.stdout, `{"schema":"zeo.update/v1"`) || !strings.Contains(u.result, "RESULT sample update ok from=1.0.0 to=2.0.0") || !strings.Contains(u.result, `undo="sample rollback"`) {
		t.Errorf("stdout %s result %q", u.stdout, u.result)
	}
	if _, err := os.Stat(filepath.Join(f.root, "versions", "1.0.0", "VERSION")); err != nil {
		t.Error("the previous version must stay installed")
	}
	if _, err := os.Stat(filepath.Join(f.state, "intent.json")); err == nil {
		t.Error("the intent record must be cleared after a verified update")
	}
	if u.doc["backup"] != filepath.Join(f.root, "versions", "1.0.0") {
		t.Errorf("backup = %v", u.doc["backup"])
	}
}

func TestCheckChangesNothing(t *testing.T) {
	f := newFake2(t)
	f.install1()
	u := f.do("update", "--check")
	if u.code != 0 || u.doc["check_only"] != true || f.current() != "1.0.0" || u.result != "" {
		t.Fatalf("%+v current %s", u, f.current())
	}
	f.ran("fetch", "preflight")
	if _, err := os.Stat(filepath.Join(f.root, "versions", "2.0.0")); err == nil {
		t.Error("--check installed something")
	}
}

func TestFailureBeforeTheSwitchIsFailedAndNothingChanged(t *testing.T) {
	for _, hook := range []string{"fetch", "install"} {
		f := newFake2(t)
		f.install1()
		f.fail[hook] = 1
		u := f.do("update")
		if u.code != 1 || u.outcome != "failed" || f.current() != "1.0.0" || f.running != "1.0.0" {
			t.Errorf("%s: %+v current %s running %s", hook, u, f.current(), f.running)
		}
		if _, err := os.Stat(filepath.Join(f.state, "intent.json")); err == nil {
			t.Errorf("%s: no intent was needed before the switch", hook)
		}
	}
}

func TestStopFailureRestartsTheOldVersion(t *testing.T) {
	f := newFake2(t)
	f.install1()
	f.fail["stop"] = 1
	u := f.do("update")
	if u.code != 1 || u.outcome != "failed" || f.current() != "1.0.0" {
		t.Fatalf("%+v", u)
	}
	f.ran("fetch", "preflight", "install", "stop", "start")
	if _, err := os.Stat(filepath.Join(f.state, "intent.json")); err == nil {
		t.Error("a failed stop must clear the intent once the old version runs again")
	}
}

func TestStopFailureAndRestartFailureIsRecoveryRequired(t *testing.T) {
	f := newFake2(t)
	f.install1()
	f.fail["stop"] = 1
	f.fail["start"] = 1
	u := f.do("update")
	if u.code != 1 || u.outcome != "recovery_required" {
		t.Fatalf("%+v", u)
	}
	if _, err := os.Stat(filepath.Join(f.state, "intent.json")); err != nil {
		t.Error("the intent must be kept")
	}
}

func TestFailureAfterTheSwitchGoesBackAndIsUndone(t *testing.T) {
	for _, hook := range []string{"start", "ready", "verify"} {
		f := newFake2(t)
		f.install1()
		f.fail[hook] = 1
		u := f.do("update")
		if u.code != 1 || u.outcome != "undone" || f.current() != "1.0.0" || f.running != "1.0.0" {
			t.Errorf("%s: %+v current %s running %s", hook, u, f.current(), f.running)
		}
		if !strings.Contains(u.result, "RESULT sample update undone") {
			t.Errorf("%s: %q", hook, u.result)
		}
		if _, err := os.Stat(filepath.Join(f.state, "intent.json")); err == nil {
			t.Errorf("%s: a proved switch-back clears the intent", hook)
		}
		// The next update is allowed (undone is safe to retry).
		if again := f.do("update"); again.code != 0 {
			t.Errorf("%s: retry after undone: %+v", hook, again)
		}
	}
}

func TestFailedSwitchBackIsRecoveryRequiredAndItBlocksEverything(t *testing.T) {
	f := newFake2(t)
	f.install1()
	f.fail["verify"] = 2 // the new version fails verify, and so does the old one after switch-back
	u := f.do("update")
	if u.code != 1 || u.outcome != "recovery_required" {
		t.Fatalf("%+v", u)
	}
	if _, err := os.Stat(filepath.Join(f.state, "intent.json")); err != nil {
		t.Fatal("the intent must be kept")
	}
	for _, verb := range [][]string{{"update"}, {"rollback"}, {"apply", "rules"}} {
		blocked := f.do(verb...)
		if blocked.code != 3 || blocked.reason != "recovery_required" && !strings.Contains(blocked.stderr, "reason=recovery_required") {
			t.Errorf("%v while recovery is required: %+v", verb, blocked)
		}
	}
	// recover proves one version running and clears the lock.
	r := f.do("recover")
	if r.code != 0 || r.outcome != "ok" {
		t.Fatalf("recover: %+v", r)
	}
	if _, err := os.Stat(filepath.Join(f.state, "intent.json")); err == nil {
		t.Error("recover must clear the intent")
	}
	if again := f.do("update"); again.code != 0 {
		t.Errorf("update after recover: %+v", again)
	}
}

func TestRecoverFailsWhileNeitherVersionIsProved(t *testing.T) {
	f := newFake2(t)
	f.install1()
	f.fail["verify"] = 2
	f.do("update")
	f.fail["verify"] = 99
	r := f.do("recover")
	if r.code != 1 || r.outcome != "recovery_required" {
		t.Fatalf("%+v", r)
	}
	if _, err := os.Stat(filepath.Join(f.state, "intent.json")); err != nil {
		t.Error("recover must keep the intent when nothing is proved")
	}
}

func TestRecoverWithNothingToRecoverIsOK(t *testing.T) {
	f := newFake2(t)
	if r := f.do("recover"); r.code != 0 || r.outcome != "ok" {
		t.Fatalf("%+v", r)
	}
}

func TestAnIntentLeftByADeadProcessBlocksTheNextRun(t *testing.T) {
	f := newFake2(t)
	f.install1()
	// What a process killed between the switch and Verify leaves behind.
	_ = os.WriteFile(filepath.Join(f.state, "intent.json"), []byte(`{"app":"sample","from":"1.0.0","to":"2.0.0","step":"switch"}`), 0o600)
	u := f.do("update")
	if u.code != 3 || u.outcome != "refused" || u.reason != "recovery_required" || !strings.Contains(u.result, "reason=recovery_required") {
		t.Fatalf("%+v", u)
	}
	// An unreadable intent still blocks.
	_ = os.WriteFile(filepath.Join(f.state, "intent.json"), []byte(`{{{`), 0o600)
	if u := f.do("update"); u.code != 3 {
		t.Errorf("unreadable intent: %+v", u)
	}
}

func TestRollbackGoesToThePreviousVersion(t *testing.T) {
	f := newFake2(t)
	f.install1()
	f.do("update")
	u := f.do("rollback")
	if u.code != 0 || f.current() != "1.0.0" || f.running != "1.0.0" || !strings.Contains(u.result, "RESULT sample rollback ok from=2.0.0 to=1.0.0") {
		t.Fatalf("%+v current %s", u, f.current())
	}
	f.ran("preflight", "stop", "start", "ready", "verify")
	if again := f.do("rollback"); again.code != 0 || f.current() != "2.0.0" {
		t.Errorf("rollback twice swaps back: %+v current %s", again, f.current())
	}
}

func TestRollbackRefusalsAndFailures(t *testing.T) {
	f := newFake2(t)
	f.release("1.0.0", false)
	f.do("update", "--to", "1.0.0")
	if u := f.do("rollback"); u.code != 3 || u.reason != "no_previous" {
		t.Errorf("no previous: %+v", u)
	}
	f.release("2.0.0", false)
	f.do("update")
	_ = os.RemoveAll(filepath.Join(f.root, "versions", "1.0.0"))
	if u := f.do("rollback"); u.code != 3 || u.reason != "no_previous" {
		t.Errorf("previous removed: %+v", u)
	}
}

func TestRollbackAcrossAMigrationIsRefusedUnlessThereIsADownMigration(t *testing.T) {
	f := newFake2(t)
	f.release("1.0.0", false)
	f.release("2.0.0", true)
	f.do("update", "--to", "1.0.0")
	u := f.do("update")
	if u.code != 0 {
		t.Fatalf("%+v", u)
	}
	if r := f.do("rollback"); r.code != 3 || r.reason != "migration" || f.current() != "2.0.0" {
		t.Fatalf("%+v current %s", r, f.current())
	}
	f.down = true
	if r := f.do("rollback"); r.code != 0 || f.current() != "1.0.0" {
		t.Fatalf("with a down-migration: %+v", r)
	}
	f.ran("preflight", "stop", "down", "start", "ready", "verify")
}

func TestDownMigrationFailureRestartsTheCurrentVersion(t *testing.T) {
	f := newFake2(t)
	f.release("1.0.0", false)
	f.release("2.0.0", true)
	f.do("update", "--to", "1.0.0")
	f.do("update")
	f.down = true
	f.fail["down"] = 1
	r := f.do("rollback")
	if r.code != 1 || r.outcome != "failed" || r.reason != "migration" || f.current() != "2.0.0" {
		t.Fatalf("%+v current %s", r, f.current())
	}
}

func TestUpdateCheckSaysAMigrationWillRun(t *testing.T) {
	f := newFake2(t)
	f.release("1.0.0", false)
	f.release("2.0.0", true)
	f.do("update", "--to", "1.0.0")
	u := f.do("update", "--check")
	if !strings.Contains(u.stdout, "a state migration and a backup will run") {
		t.Errorf("%s", u.stdout)
	}
}

func TestPreflightRefusals(t *testing.T) {
	f := newFake2(t)
	f.install1()
	f.do("update")
	if u := f.do("update", "--to", "1.0.0"); u.code != 3 || u.reason != "downgrade" || f.current() != "2.0.0" {
		t.Errorf("downgrade: %+v", u)
	}
	if u := f.do("update", "--to", "2.0.0"); u.code != 3 || u.reason != "current" {
		t.Errorf("same version: %+v", u)
	}
	f.release("3.0.0", false)
	f.fail["preflight"] = 1
	if u := f.do("update", "--to", "3.0.0"); u.code != 3 || u.reason != "preflight" || f.current() != "2.0.0" {
		t.Errorf("app preflight: %+v", u)
	}
}

func TestAnUnsafeVersionNeverReachesAPath(t *testing.T) {
	f := newFake2(t)
	f.release("1.0.0", false)
	for _, v := range []string{"../x", "a/b", ".hidden", ""} {
		if v == "" {
			continue
		}
		if u := f.do("update", "--to", v); u.code != 2 {
			t.Errorf("--to %q: exit %d, want 2", v, u.code)
		}
	}
	f.rels["9.9.9"] = kit.Release{Version: "../../etc", Dir: f.stage}
	if u := f.do("update", "--to", "9.9.9"); u.code != 3 || u.reason != "version" {
		t.Errorf("a release naming an unsafe version: %+v", u)
	}
}

func TestLiveRuleNeedsTheIdleFence(t *testing.T) {
	f := newFake2(t)
	f.install1()
	f.live = true
	if u := f.do("update"); u.code != 3 || u.reason != "studio_live" || f.current() != "1.0.0" {
		t.Fatalf("live, no fence: %+v", u)
	}
	f.fence = true
	f.fail["fence"] = 1
	if u := f.do("update"); u.code != 3 || u.reason != "studio_live" {
		t.Fatalf("fence busy: %+v", u)
	}
	u := f.do("update")
	if u.code != 0 || f.current() != "2.0.0" {
		t.Fatalf("fence held: %+v", u)
	}
	// The fence is taken before anything is installed and released after the last step.
	f.ran("fetch", "fence", "preflight", "install", "stop", "start", "ready", "verify", "unfence")
}

func TestProtectedPathTouchedByAHookFailsBeforeTheSwitch(t *testing.T) {
	f := newFake2(t)
	f.install1()
	f.touch = true
	u := f.do("update")
	if u.code != 1 || u.outcome != "failed" || u.reason != "protected" || f.current() != "1.0.0" {
		t.Fatalf("%+v current %s", u, f.current())
	}
	if _, err := os.Stat(filepath.Join(f.root, "versions", "2.0.0")); err == nil {
		t.Error("a refused install must not leave the new version behind")
	}
	if err := f.app().Updater.Guard(filepath.Join(f.prot, "approver.key")); err == nil {
		t.Error("Guard must refuse a protected path")
	}
	if err := f.app().Updater.Guard(filepath.Join(f.root, "versions")); err != nil {
		t.Errorf("Guard refused an ordinary path: %v", err)
	}
}

func TestOneOperationAtATime(t *testing.T) {
	f := newFake2(t)
	f.install1()
	app := f.app()
	var inner upd
	app.Updater.Verify = func(context.Context, string) error {
		var out, errb strings.Builder
		code := f.app().Run(context.Background(), []string{"update", "--to", "2.0.0", "--json"}, &sbWriter{&out}, &sbWriter{&errb})
		inner = upd{code: code, stderr: errb.String()}
		return errors.New("stop here")
	}
	var out, errb strings.Builder
	app.Run(context.Background(), []string{"update", "--json"}, &sbWriter{&out}, &sbWriter{&errb})
	if inner.code != 3 || !strings.Contains(inner.stderr, "reason=busy") {
		t.Errorf("a second operation while one runs: %+v", inner)
	}
}

func TestValidateUpdater(t *testing.T) {
	ok := func() *kit.App { return newFake2(t).app() }
	cases := map[string]func(*kit.App){
		"no state dir":       func(a *kit.App) { a.StateDir = "" },
		"relative root":      func(a *kit.App) { a.Updater.Root = "rel" },
		"no fetch":           func(a *kit.App) { a.Updater.Fetch = nil },
		"no verify":          func(a *kit.App) { a.Updater.Verify = nil },
		"stop without start": func(a *kit.App) { a.Updater.Start = nil },
		"ready without svc":  func(a *kit.App) { a.Updater.Stop, a.Updater.Start = nil, nil },
		"relative protected": func(a *kit.App) { a.Updater.Protected = []string{"keys"} },
		"protected in root":  func(a *kit.App) { a.Updater.Protected = []string{filepath.Join(a.Updater.Root, "versions")} },
		"root in protected":  func(a *kit.App) { a.Updater.Protected = []string{filepath.Dir(a.Updater.Root)} },
	}
	for name, mutate := range cases {
		app := ok()
		mutate(app)
		if err := app.Validate(); err == nil {
			t.Errorf("%s: Validate accepted it", name)
		}
	}
	if err := ok().Validate(); err != nil {
		t.Errorf("a good app: %v", err)
	}
}

func TestHelpListsTheUpdateVerbs(t *testing.T) {
	_, out, _ := run(t, newFake2(t).app(), "help", "--json")
	for _, want := range []string{`"name":"update"`, `"name":"rollback"`, `"name":"recover"`, `"zeo.update/v1"`} {
		if !strings.Contains(out, want) {
			t.Errorf("help --json lacks %s", want)
		}
	}
	_, out, _ = run(t, sampleapp.New(false), "help", "--json")
	if strings.Contains(out, `"name":"update"`) {
		t.Error("an app with no Updater must not list update")
	}
}

func TestCompareVersionsThroughUpdateOrder(t *testing.T) {
	f := newFake2(t)
	for _, v := range []string{"1.9.0", "1.10.0", "1.10.0-rc1"} {
		f.release(v, false)
	}
	f.do("update", "--to", "1.9.0")
	if u := f.do("update", "--to", "1.10.0"); u.code != 0 {
		t.Errorf("1.10.0 is newer than 1.9.0 (numeric, not text, order): %+v", u)
	}
}

// The --to value is input and is never echoed.
func TestUpdateToIsNotEchoed(t *testing.T) {
	f := newFake2(t)
	u := f.do("update", "--to", "bad version \x1b[31m")
	if u.code != 2 || strings.Contains(u.stderr, "bad version") || strings.Contains(u.stderr, "\x1b") || strings.Contains(u.stderr, "31m") {
		t.Fatalf("%+v", u)
	}
}
