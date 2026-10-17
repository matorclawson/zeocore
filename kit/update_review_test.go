package kit_test

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/zeroemployeeorg/zeocore/kit"
)

// P2-A (council, #136): Guard compares the paths a hook gives it with symbolic
// links resolved, and wants them absolute.
func TestGuardResolvesLinksAndWantsAbsolutePaths(t *testing.T) {
	f := newFake2(t)
	u := f.app().Updater
	link := filepath.Join(t.TempDir(), "link")
	if err := os.Symlink(f.prot, link); err != nil {
		t.Fatal(err)
	}
	for name, p := range map[string]string{
		"plain":             filepath.Join(f.prot, "k"),
		"through a link":    filepath.Join(link, "k"),
		"the link itself":   link,
		"a relative path":   "keys/k",
		"above a protected": filepath.Dir(f.prot),
	} {
		if err := u.Guard(p); err == nil {
			t.Errorf("%s: Guard accepted %s", name, p)
		}
	}
	if err := u.Guard(filepath.Join(t.TempDir(), "elsewhere")); err != nil {
		t.Errorf("an unrelated path: %v", err)
	}
	// Root may not reach a protected path through a link either.
	g := newFake2(t)
	link2 := filepath.Join(t.TempDir(), "link2")
	if err := os.Symlink(g.prot, link2); err != nil {
		t.Fatal(err)
	}
	app := g.app()
	app.Updater.Root = filepath.Join(link2, "root")
	app.Updater.Protected = []string{g.prot}
	if err := app.Validate(); err == nil {
		t.Error("a Root that reaches a protected path through a link was accepted")
	}
}

// P2-B: no step may touch a protected path, whichever hook it is.
func TestNoHookMayTouchAProtectedPath(t *testing.T) {
	for _, hook := range []string{"fetch", "preflight", "stop", "start", "ready", "verify"} {
		f := newFake2(t)
		f.install1()
		f.touchIn = map[string]bool{hook: true}
		u := f.do("update", "--to", "2.0.0")
		switch hook {
		case "start", "ready", "verify":
			// After the switch: the update goes back.
			if u.outcome != "undone" || f.current() != "1.0.0" {
				t.Errorf("%s: outcome %s current %s", hook, u.outcome, f.current())
			}
		case "stop":
			// Caught just before the switch, by the check that stands there.
			if u.outcome != "failed" || u.reason != "protected" || f.current() != "1.0.0" {
				t.Errorf("stop: outcome %s reason %s current %s", u.outcome, u.reason, f.current())
			}
		default:
			if u.outcome == "ok" || f.current() != "1.0.0" {
				t.Errorf("%s: outcome %s current %s", hook, u.outcome, f.current())
			}
		}
		if u.outcome == "ok" {
			t.Errorf("%s: an update that touched a protected path ended ok", hook)
		}
	}
}

func TestAppendingToAnAppendOnlyPathIsNotATouchButOtherFilesMustNotGrow(t *testing.T) {
	f := newFake2(t)
	f.install1()
	app := f.app()
	app.Updater.AppendOnly = []string{filepath.Join(f.prot, "approver.key")}
	inst := app.Updater.Install
	app.Updater.Install = func(ctx context.Context, rel kit.Release, dest string) error {
		fh, err := os.OpenFile(filepath.Join(f.prot, "approver.key"), os.O_APPEND|os.O_WRONLY, 0)
		if err != nil {
			return err
		}
		_, _ = fh.WriteString(" and more")
		fh.Close()
		return inst(ctx, rel, dest)
	}
	var out, errb strings.Builder
	code := app.Run(context.Background(), []string{"update", "--to", "2.0.0", "--json"}, &sbWriter{&out}, &sbWriter{&errb})
	if code != 0 || f.current() != "2.0.0" {
		t.Fatalf("exit %d current %s %s", code, f.current(), errb.String())
	}
}

func TestANewOrRemovedProtectedEntryIsATouch(t *testing.T) {
	for name, mutate := range map[string]func(f *fakeApp){
		"a new file":     func(f *fakeApp) { _ = os.WriteFile(filepath.Join(f.prot, "new.key"), []byte("x"), 0o600) },
		"a removed file": func(f *fakeApp) { _ = os.Remove(filepath.Join(f.prot, "approver.key")) },
		"a shrunk file":  func(f *fakeApp) { _ = os.WriteFile(filepath.Join(f.prot, "approver.key"), []byte("sec"), 0o600) },
		"a mode change":  func(f *fakeApp) { _ = os.Chmod(filepath.Join(f.prot, "approver.key"), 0o644) },
	} {
		f := newFake2(t)
		f.install1()
		app := f.app()
		inst := app.Updater.Install
		app.Updater.Install = func(ctx context.Context, rel kit.Release, dest string) error {
			mutate(f)
			return inst(ctx, rel, dest)
		}
		var out, errb strings.Builder
		code := app.Run(context.Background(), []string{"update", "--to", "2.0.0", "--json"}, &sbWriter{&out}, &sbWriter{&errb})
		if code == 0 || f.current() != "1.0.0" {
			t.Errorf("%s: exit %d current %s", name, code, f.current())
		}
	}
}

// P2-C: the live rule fails closed.
func TestAnUnknownLiveStateRefusesWithTheLiveReason(t *testing.T) {
	f := newFake2(t)
	f.install1()
	f.statusErr = true
	u := f.do("update", "--to", "2.0.0")
	if u.code != 3 || !strings.Contains(u.result, "reason=studio_live") || f.current() != "1.0.0" {
		t.Fatalf("%+v current %s", u, f.current())
	}
	f.fence = true
	if u := f.do("update", "--to", "2.0.0"); u.code != 0 || f.current() != "2.0.0" {
		t.Fatalf("with an idle fence the unknown state is covered: %+v", u)
	}
}

// P2-D: recover looks before it acts.
func TestRecoverKeepsAHealthyCurrentAndStopsNothing(t *testing.T) {
	f := newFake2(t)
	f.install1()
	if u := f.do("update", "--to", "2.0.0"); u.code != 0 {
		t.Fatal(u)
	}
	// A crash after 2.0.0 was proved and before the intent was cleared.
	writeJSON(t, filepath.Join(f.state, "intent.json"), map[string]any{"app": "sample", "from": "1.0.0", "to": "2.0.0", "step": "switch"})
	f.asIsOK = true
	u := f.do("recover")
	if u.code != 0 || f.current() != "2.0.0" {
		t.Fatalf("%+v current %s", u, f.current())
	}
	for _, c := range f.calls {
		if c == "stop" || c == "start" {
			t.Fatalf("recover stopped or started a healthy service: %v", f.calls)
		}
	}
	if _, err := os.Stat(filepath.Join(f.state, "intent.json")); err == nil {
		t.Fatal("the intent should be cleared")
	}
}

func TestRecoverThatMustActObeysTheLiveRule(t *testing.T) {
	f := newFake2(t)
	f.install1()
	if u := f.do("update", "--to", "2.0.0"); u.code != 0 {
		t.Fatal(u)
	}
	writeJSON(t, filepath.Join(f.state, "intent.json"), map[string]any{"app": "sample", "from": "1.0.0", "to": "2.0.0", "step": "switch"})
	f.running = "9.9.9" // the running process is not the current version: it must act
	f.live = true
	u := f.do("recover")
	if u.code != 3 || !strings.Contains(u.result, "reason=studio_live") {
		t.Fatalf("%+v", u)
	}
	for _, c := range f.calls {
		if c == "stop" {
			t.Fatalf("a live studio was stopped: %v", f.calls)
		}
	}
	// With an idle fence it acts and recovers.
	f.fence = true
	f.live = false
	if u := f.do("recover"); u.code != 0 || f.running != f.current() {
		t.Fatalf("%+v running %s current %s", u, f.running, f.current())
	}
}

func TestAnUnreadableIntentIsClearedOnceCurrentIsProved(t *testing.T) {
	f := newFake2(t)
	f.install1()
	f.asIsOK = true
	if err := os.WriteFile(filepath.Join(f.state, "intent.json"), []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	if u := f.do("update", "--to", "2.0.0"); u.code != 3 {
		t.Fatalf("an unreadable intent still blocks: %+v", u)
	}
	if u := f.do("recover"); u.code != 0 {
		t.Fatalf("%+v", u)
	}
	if _, err := os.Stat(filepath.Join(f.state, "intent.json")); err == nil {
		t.Fatal("cleared")
	}
}

// P2-F: a half-done apply leaves an intent that blocks, and recover reads the targets.
func TestAnApplyThatCannotBePutBackBlocksUntilRecover(t *testing.T) {
	f := newFake2(t)
	app := f.app()
	vals := map[string]string{"t": "a", "u": "a"}
	calls := 0
	app.Subjects = []kit.Subject{{
		Name: "rules", Shared: true,
		Plan: func(context.Context) ([]kit.Change, error) {
			return []kit.Change{{Target: "t", Before: vals["t"], After: "b"}, {Target: "u", Before: vals["u"], After: "b"}}, nil
		},
		Read: func(_ context.Context, ts []string) (map[string]string, error) {
			m := map[string]string{}
			for _, t := range ts {
				m[t] = vals[t]
			}
			return m, nil
		},
		Apply: func(_ context.Context, cs []kit.Change) error {
			calls++
			if calls == 1 {
				vals[cs[0].Target] = cs[0].After
				return errors.New("half way")
			}
			return errors.New("cannot put back")
		},
	}}
	run := func(args ...string) (int, string, string) {
		var out, errb strings.Builder
		code := app.Run(context.Background(), args, &sbWriter{&out}, &sbWriter{&errb})
		return code, out.String(), errb.String()
	}
	code, planOut, _ := run("plan", "rules", "--json")
	if code != 0 {
		t.Fatal(code)
	}
	pf := filepath.Join(t.TempDir(), "plan.json")
	_ = os.WriteFile(pf, []byte(planOut), 0o600)
	code, out, _ := run("apply", "rules", "--plan", pf, "--json")
	if code != 1 || !strings.Contains(out, "recovery_required") || !strings.Contains(out, `"backup"`) {
		t.Fatalf("%d %s", code, out)
	}
	// The intent is there, so every later change refuses.
	if code, _, e := run("update"); code != 3 || !strings.Contains(e, "sample recover") {
		t.Fatalf("an update went past a half-done apply: %d %s", code, e)
	}
	if code, _, e := run("apply", "rules", "--plan", pf); code != 3 || !strings.Contains(e, "sample recover") {
		t.Fatalf("an apply went past a half-done apply: %d %s", code, e)
	}
	// Recover reads the targets: a mix stays recovery_required with the backup.
	code, out, _ = run("recover", "--json")
	if code != 1 || !strings.Contains(out, "recovery_required") || !strings.Contains(out, `"backup"`) {
		t.Fatalf("%d %s", code, out)
	}
	// A person puts the targets back: all before, recover clears the intent.
	vals["t"] = "a"
	if code, out, e := run("recover", "--json"); code != 0 {
		t.Fatalf("%d %s %s", code, out, e)
	}
	if _, err := os.Stat(filepath.Join(f.state, "intent.json")); err == nil {
		t.Fatal("the intent should be cleared")
	}
	// All after clears it too.
	writeJSON(t, filepath.Join(f.state, "intent.json"), map[string]any{
		"app": "sample", "step": "apply", "kind": "apply", "subject": "rules", "plan_id": "x",
		"changes": []map[string]string{{"target": "t", "before": "a", "after": "b"}, {"target": "u", "before": "a", "after": "b"}},
	})
	vals["t"], vals["u"] = "b", "b"
	if code, _, _ := run("recover"); code != 0 {
		t.Fatal(code)
	}
}

// P2-G: hook error text is folded in text mode, and the intent's fields too.
func TestHookErrorsAreFoldedInTextMode(t *testing.T) {
	f := newFake2(t)
	f.install1()
	app := f.app()
	app.Updater.Start = func(context.Context) error {
		return errors.New("boom\nRESULT sample update ok from=1.0.0 to=2.0.0\x1b[31m")
	}
	var out, errb strings.Builder
	app.Run(context.Background(), []string{"update", "--to", "2.0.0"}, &sbWriter{&out}, &sbWriter{&errb})
	for _, l := range strings.Split(errb.String(), "\n") {
		if strings.HasPrefix(l, "RESULT sample update ok") {
			t.Fatalf("a hook error forged a RESULT line: %q", errb.String())
		}
	}
	if strings.Contains(errb.String(), "\x1b") {
		t.Fatalf("an escape reached stderr: %q", errb.String())
	}
	writeJSON(t, filepath.Join(f.state, "intent.json"), map[string]any{"app": "sample", "from": "1.0.0", "to": "2.0.0", "step": "x\x1b[31m\nRESULT sample update ok"})
	out.Reset()
	errb.Reset()
	f.app().Run(context.Background(), []string{"update"}, &sbWriter{&out}, &sbWriter{&errb})
	if strings.Contains(errb.String(), "\x1b") || strings.Contains(errb.String(), "RESULT sample update ok") {
		t.Fatalf("an intent field was printed raw: %q", errb.String())
	}
}

// P3s.
func TestALockFailureIsFailedNotBusyAndCheckWritesNothing(t *testing.T) {
	f := newFake2(t)
	f.install1()
	file := filepath.Join(t.TempDir(), "state-is-a-file")
	_ = os.WriteFile(file, []byte("x"), 0o600)
	f.state = file
	u := f.do("update", "--to", "2.0.0")
	if u.code != 1 || strings.Contains(u.result, "reason=busy") {
		t.Fatalf("%+v", u)
	}
	g := newFake2(t)
	g.release("2.0.0", false)
	g.state = filepath.Join(t.TempDir(), "never-created")
	g.do("update", "--check")
	if _, err := os.Stat(g.state); err == nil {
		t.Fatal("update --check created the state directory")
	}
}

func TestStaleInstallTempDirsAreSwept(t *testing.T) {
	f := newFake2(t)
	f.release("1.0.0", false)
	f.release("2.0.0", false)
	dead := filepath.Join(f.root, "versions", "1.0.0.tmp-2147483646")
	live := filepath.Join(f.root, "versions", "9.9.9.tmp-"+itoa(os.Getppid()))
	_ = os.MkdirAll(dead, 0o755)
	_ = os.MkdirAll(live, 0o755)
	f.do("update", "--to", "1.0.0")
	if _, err := os.Stat(dead); err == nil {
		t.Error("a dead process's install directory was kept")
	}
	if _, err := os.Stat(live); err != nil {
		t.Error("a live process's install directory was removed")
	}
}

func itoa(n int) string {
	b, _ := json.Marshal(n)
	return string(b)
}

func writeJSON(t *testing.T, path string, v any) {
	t.Helper()
	b, _ := json.Marshal(v)
	if err := os.WriteFile(path, b, 0o600); err != nil {
		t.Fatal(err)
	}
}

// A switch-back that cannot be proved is recovery_required with the backup path.
func TestARecoveryRequiredGoingBackCarriesTheBackup(t *testing.T) {
	f := newFake2(t)
	f.install1()
	f.fail["verify"] = 2 // the new version fails to verify, and so does the old one after the switch back
	u := f.do("update", "--to", "2.0.0")
	if u.outcome != "recovery_required" {
		t.Fatalf("%+v", u)
	}
	if b, _ := u.doc["backup"].(string); b == "" || !strings.Contains(b, "1.0.0") {
		t.Fatalf("a recovery_required document should carry the backup path: %v", u.doc)
	}
}

// P2-B (round 2): without AppendOnly, a protected file may not grow at all.
func TestAProtectedFileMayNotGrowUnlessAppendOnly(t *testing.T) {
	f := newFake2(t)
	f.install1()
	app := f.app()
	inst := app.Updater.Install
	app.Updater.Install = func(ctx context.Context, rel kit.Release, dest string) error {
		fh, _ := os.OpenFile(filepath.Join(f.prot, "approver.key"), os.O_APPEND|os.O_WRONLY, 0)
		_, _ = fh.WriteString("one more line")
		fh.Close()
		return inst(ctx, rel, dest)
	}
	var out, errb strings.Builder
	code := app.Run(context.Background(), []string{"update", "--to", "2.0.0", "--json"}, &sbWriter{&out}, &sbWriter{&errb})
	if code == 0 || f.current() != "1.0.0" {
		t.Fatalf("exit %d current %s", code, f.current())
	}
	bad := f.app()
	bad.Updater.AppendOnly = []string{filepath.Join(t.TempDir(), "elsewhere")}
	if err := bad.Validate(); err == nil {
		t.Error("an append-only path outside the protected paths was accepted")
	}
}

// P2-A (round 2): ".." after a link is refused before anything is resolved.
func TestGuardRefusesDotDotAfterALink(t *testing.T) {
	f := newFake2(t)
	u := f.app().Updater
	root := t.TempDir()
	sub := filepath.Join(f.prot, "sub")
	_ = os.MkdirAll(sub, 0o700)
	lnk := filepath.Join(root, "lnk")
	if err := os.Symlink(sub, lnk); err != nil {
		t.Fatal(err)
	}
	// root/lnk/../k is f.prot/k to the kernel, root/k to a text-cleaning check.
	if err := u.Guard(filepath.Join(root, "lnk") + "/../k"); err == nil {
		t.Error("Guard accepted a path with .. after a symbolic link")
	}
	if err := u.Guard(filepath.Join(root, "k")); err != nil {
		t.Errorf("an unrelated clean path: %v", err)
	}
}

// A protected tree: a retargeted link inside it, and a protected path that is itself a link.
func TestProtectedLinksAreCompared(t *testing.T) {
	for name, setup := range map[string]func(f *fakeApp) (retarget func()){
		"a link inside the tree": func(f *fakeApp) func() {
			a, b := filepath.Join(f.prot, "a"), filepath.Join(f.prot, "b")
			_ = os.WriteFile(a, []byte("a"), 0o600)
			_ = os.WriteFile(b, []byte("b"), 0o600)
			l := filepath.Join(f.prot, "ln")
			_ = os.Symlink(a, l)
			return func() { _ = os.Remove(l); _ = os.Symlink(b, l) }
		},
		"the protected path itself a link": func(f *fakeApp) func() {
			real1, real2 := t.TempDir(), t.TempDir()
			_ = os.WriteFile(filepath.Join(real1, "k"), []byte("k"), 0o600)
			_ = os.WriteFile(filepath.Join(real2, "k"), []byte("k"), 0o600)
			l := filepath.Join(t.TempDir(), "keys")
			_ = os.Symlink(real1, l)
			f.prot = l
			return func() { _ = os.Remove(l); _ = os.Symlink(real2, l) }
		},
	} {
		f := newFake2(t)
		retarget := setup(f)
		f.install1()
		app := f.app()
		inst := app.Updater.Install
		app.Updater.Install = func(ctx context.Context, rel kit.Release, dest string) error {
			retarget()
			return inst(ctx, rel, dest)
		}
		var out, errb strings.Builder
		code := app.Run(context.Background(), []string{"update", "--to", "2.0.0", "--json"}, &sbWriter{&out}, &sbWriter{&errb})
		if code == 0 || f.current() != "1.0.0" {
			t.Errorf("%s: exit %d current %s", name, code, f.current())
		}
	}
}

// P2-F: an app with Subjects but no Updater still has recover.
func TestRecoverExistsForAnAppWithSubjectsAndNoUpdater(t *testing.T) {
	f := newFake2(t)
	app := f.app()
	app.Updater = nil
	vals := map[string]string{"t": "a", "u": "a"}
	calls := 0
	app.Subjects = []kit.Subject{{
		Name: "rules", Shared: true,
		Plan: func(context.Context) ([]kit.Change, error) {
			return []kit.Change{{Target: "t", Before: vals["t"], After: "b"}, {Target: "u", Before: vals["u"], After: "b"}}, nil
		},
		Read: func(_ context.Context, ts []string) (map[string]string, error) {
			return map[string]string{"t": vals["t"], "u": vals["u"]}, nil
		},
		Apply: func(_ context.Context, cs []kit.Change) error {
			calls++
			if calls == 1 {
				vals[cs[0].Target] = "b"
				return errors.New("half way")
			}
			return errors.New("cannot put back")
		},
	}}
	run := func(args ...string) (int, string) {
		var out, errb strings.Builder
		code := app.Run(context.Background(), args, &sbWriter{&out}, &sbWriter{&errb})
		return code, out.String() + errb.String()
	}
	_, planOut := run("plan", "rules", "--json")
	pf := filepath.Join(t.TempDir(), "plan.json")
	_ = os.WriteFile(pf, []byte(planOut), 0o600)
	if code, out := run("apply", "rules", "--plan", pf); code != 1 || !strings.Contains(out, "recovery_required") {
		t.Fatalf("%d %s", code, out)
	}
	if code, out := run("recover"); code != 1 || !strings.Contains(out, "recovery_required") {
		t.Fatalf("recover on a changed target: %d %s", code, out)
	}
	vals["t"] = "a"
	if code, out := run("recover"); code != 0 {
		t.Fatalf("recover once the targets are back: %d %s", code, out)
	}
	if _, err := os.Stat(filepath.Join(f.state, "intent.json")); err == nil {
		t.Fatal("the intent should be cleared")
	}
	// An update intent in an app with no updater cannot be recovered here.
	writeJSON(t, filepath.Join(f.state, "intent.json"), map[string]any{"app": "sample", "from": "1.0.0", "to": "2.0.0", "step": "switch"})
	if code, _ := run("recover"); code != 1 {
		t.Fatalf("an update intent without an updater: %d", code)
	}
}

// recover's as-is proof asks Ready too.
func TestRecoverAsIsChecksReady(t *testing.T) {
	f := newFake2(t)
	f.install1()
	if u := f.do("update", "--to", "2.0.0"); u.code != 0 {
		t.Fatal(u)
	}
	writeJSON(t, filepath.Join(f.state, "intent.json"), map[string]any{"app": "sample", "from": "1.0.0", "to": "2.0.0", "step": "switch"})
	f.asIsOK = true
	f.fail["ready"] = 1 // the as-is readiness check fails, so recover must act
	f.fence = true
	u := f.do("recover")
	acted := false
	for _, c := range f.calls {
		acted = acted || c == "stop"
	}
	if !acted {
		t.Fatalf("recover accepted a version whose Ready failed: %+v calls %v", u, f.calls)
	}
}

// A version name that holds .tmp- is not a version (it is how an install in progress is marked).
func TestAVersionNamedLikeAnInstallTempIsRefused(t *testing.T) {
	f := newFake2(t)
	f.install1()
	if u := f.do("update", "--to", "2.0.tmp-99999"); u.code != 2 {
		t.Fatalf("%+v", u)
	}
}

// A hook-supplied refusal reason is not printed raw.
func TestARefusalReasonFromAHookIsShownOnlyAsAPlainName(t *testing.T) {
	f := newFake2(t)
	f.install1()
	app := f.app()
	app.Updater.Preflight = func(context.Context, string, kit.Release) error {
		return kit.Refused("bad reason\nRESULT x", "no")
	}
	var out, errb strings.Builder
	app.Run(context.Background(), []string{"update", "--to", "2.0.0"}, &sbWriter{&out}, &sbWriter{&errb})
	if strings.Contains(out.String(), "bad reason") {
		t.Fatalf("%q", out.String())
	}
}

// A same-size rewrite is caught by the bytes, not the size.
func TestASameSizeRewriteOfAProtectedFileIsATouch(t *testing.T) {
	f := newFake2(t)
	f.install1()
	app := f.app()
	inst := app.Updater.Install
	app.Updater.Install = func(ctx context.Context, rel kit.Release, dest string) error {
		_ = os.WriteFile(filepath.Join(f.prot, "approver.key"), []byte("SECRET"), 0o600) // "secret" before, same length
		return inst(ctx, rel, dest)
	}
	var out, errb strings.Builder
	code := app.Run(context.Background(), []string{"update", "--to", "2.0.0", "--json"}, &sbWriter{&out}, &sbWriter{&errb})
	if code == 0 || f.current() != "1.0.0" {
		t.Fatalf("exit %d current %s", code, f.current())
	}
}
