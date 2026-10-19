package kit_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func custodyFile(t *testing.T, f *fakeApp) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "approver.key")
	if err := os.WriteFile(p, []byte("custody"), 0o600); err != nil {
		t.Fatal(err)
	}
	// far in the past, so no write in a test can land on the same mtime
	past := time.Unix(1_000_000_000, 0)
	if err := os.Chtimes(p, past, past); err != nil {
		t.Fatal(err)
	}
	f.custody = []string{p}
	return p
}

// Slice 3c: a custody file is never opened. Mode 000 would fail an open for anyone but root.
func TestCustodyIsNeverRead(t *testing.T) {
	f := newFake2(t)
	p := custodyFile(t, f)
	if err := os.Chmod(p, 0); err != nil {
		t.Fatal(err)
	}
	f.install1()
	if u := f.do("update"); u.code != 0 || f.current() != "2.0.0" {
		t.Fatalf("an unreadable custody file stopped an update: %+v", u)
	}
	if u := f.do("rollback"); u.code != 0 || f.current() != "1.0.0" {
		t.Fatalf("an unreadable custody file stopped a rollback: %+v", u)
	}
}

// Slice 3c: any change to a custody entry's identity ends the operation as protected.
func TestACustodyChangeIsATouch(t *testing.T) {
	for _, act := range []string{"write", "replace", "chtimes", "remove"} {
		f := newFake2(t)
		custodyFile(t, f)
		f.install1()
		f.custodyAct = map[string]string{"install": act}
		u := f.do("update")
		if u.code != 1 || u.outcome != "failed" || u.reason != "protected" || f.current() != "1.0.0" {
			t.Errorf("%s in install: %+v current %s", act, u, f.current())
		}
		if _, err := os.Stat(filepath.Join(f.root, "versions", "2.0.0")); err == nil {
			t.Errorf("%s: a refused install left the new version behind", act)
		}
	}
}

// A change after the switch goes back.
func TestACustodyChangeAfterTheSwitchGoesBack(t *testing.T) {
	f := newFake2(t)
	custodyFile(t, f)
	f.install1()
	f.custodyAct = map[string]string{"ready": "write"}
	u := f.do("update")
	if u.outcome != "undone" || u.reason != "protected" || f.current() != "1.0.0" {
		t.Fatalf("%+v current %s", u, f.current())
	}
}

// A custody path that is absent is part of the identity: it may not appear.
func TestACustodyPathMayNotAppear(t *testing.T) {
	f := newFake2(t)
	f.custody = []string{filepath.Join(t.TempDir(), "later.key")}
	f.install1()
	f.custodyAct = map[string]string{"install": "create"}
	if u := f.do("update"); u.reason != "protected" || f.current() != "1.0.0" {
		t.Fatalf("%+v current %s", u, f.current())
	}
}

func TestCustodyPathsAreValidatedAndGuarded(t *testing.T) {
	f := newFake2(t)
	app := f.app()
	app.Updater.Custody = []string{"relative/key"}
	if err := app.Validate(); err == nil {
		t.Error("a relative custody path was accepted")
	}
	app.Updater.Custody = []string{filepath.Join(f.prot, "approver.key")}
	if err := app.Validate(); err == nil || !strings.Contains(err.Error(), "overlaps protected") {
		t.Errorf("a custody path inside Protected was accepted: %v", err)
	}
	app.Updater.Custody = []string{filepath.Join(f.root, "k")}
	if err := app.Validate(); err == nil {
		t.Error("a custody path inside the root was accepted")
	}
	p := filepath.Join(t.TempDir(), "k")
	app.Updater.Custody = []string{p}
	if err := app.Validate(); err != nil {
		t.Errorf("a valid custody path was refused: %v", err)
	}
	if err := app.Updater.Guard(p); err == nil {
		t.Error("Guard accepted a custody path")
	}
	if err := app.Updater.Guard(filepath.Join(p, "child")); err == nil {
		t.Error("Guard accepted a path under a custody path")
	}
	target := filepath.Join(t.TempDir(), "real.key")
	if err := os.WriteFile(target, []byte("k"), 0o600); err != nil {
		t.Fatal(err)
	}
	app.Updater.Custody = []string{link(t, target, filepath.Join(t.TempDir(), "k.link"))}
	if err := app.Validate(); err == nil || !strings.Contains(err.Error(), "symbolic link") {
		t.Errorf("a custody path that is a link was accepted: %v", err)
	}
	dangling := link(t, filepath.Join(t.TempDir(), "nothing"), filepath.Join(t.TempDir(), "l"))
	app.Updater.Custody = []string{dangling}
	if err := app.Validate(); err == nil || !strings.Contains(err.Error(), "does not resolve") {
		t.Errorf("a dangling custody link was not named: %v", err)
	}
}

func threeVersions(t *testing.T, migrateTwo bool) *fakeApp {
	f := newFake2(t)
	f.release("1.0.0", false)
	f.release("2.0.0", migrateTwo)
	f.release("3.0.0", false)
	for _, v := range []string{"1.0.0", "2.0.0", "3.0.0"} {
		if u := f.do("update", "--to", v); u.code != 0 {
			t.Fatalf("update %s: %+v", v, u)
		}
	}
	return f
}

// Slice 3c: rollback --to goes to an installed version further back.
func TestRollbackToAnInstalledVersion(t *testing.T) {
	f := threeVersions(t, false)
	u := f.do("rollback", "--to", "1.0.0")
	if u.code != 0 || f.current() != "1.0.0" || f.running != "1.0.0" || !strings.Contains(u.result, "RESULT sample rollback ok from=3.0.0 to=1.0.0") {
		t.Fatalf("%+v current %s", u, f.current())
	}
	f.ran("preflight", "stop", "start", "ready", "verify")
}

func TestRollbackToThePreviousVersionIsThePlainRollback(t *testing.T) {
	f := threeVersions(t, false)
	if u := f.do("rollback", "--to", "2.0.0"); u.code != 0 || f.current() != "2.0.0" {
		t.Fatalf("%+v current %s", u, f.current())
	}
}

func TestRollbackToRefusals(t *testing.T) {
	f := threeVersions(t, false)
	if u := f.do("rollback", "--to", "9.9.9"); u.code != 4 || u.reason != "not_found" || f.current() != "3.0.0" {
		t.Errorf("unknown version: %+v", u)
	}
	_ = os.RemoveAll(filepath.Join(f.root, "versions", "1.0.0"))
	if u := f.do("rollback", "--to", "1.0.0"); u.reason != "not_found" {
		t.Errorf("pruned version: %+v", u)
	}
	if u := f.do("rollback", "--to", "3.0.0"); u.code != 3 || u.reason != "current" {
		t.Errorf("the current version: %+v", u)
	}
	if err := os.WriteFile(filepath.Join(f.root, "versions", "8.0.0"), []byte("not a directory"), 0o600); err != nil {
		t.Fatal(err)
	}
	if u := f.do("rollback", "--to", "8.0.0"); u.reason != "not_found" || u.code != 4 {
		t.Errorf("a file named like a version: %+v", u)
	}
	if u := f.do("rollback", "--to", "../x"); u.code != 2 {
		t.Errorf("an unsafe word: %+v", u)
	}
	if u := f.do("rollback", "--to", "a.tmp-1"); u.code != 2 {
		t.Errorf("an install-in-progress name: %+v", u)
	}
}

// A migration between the current version and the target cannot be undone by a single Down.
func TestRollbackToAcrossAMigrationIsRefusedEvenWithADownMigration(t *testing.T) {
	f := threeVersions(t, true)
	f.down = true
	if u := f.do("rollback", "--to", "1.0.0"); u.code != 3 || u.reason != "migration" || f.current() != "3.0.0" {
		t.Fatalf("%+v current %s", u, f.current())
	}
	// One step at a time works: 3.0.0 to 2.0.0 crossed no migration.
	if u := f.do("rollback"); u.code != 0 || f.current() != "2.0.0" {
		t.Fatalf("%+v", u)
	}
}

// Without a chain (a history written before this slice, or after a recover) the kit cannot
// prove the way, so it refuses anything but the previous version.
func TestRollbackToWithoutAChainIsRefused(t *testing.T) {
	f := threeVersions(t, false)
	raw := `{"current":"3.0.0","previous":"2.0.0","migrated":false}`
	if err := os.WriteFile(filepath.Join(f.state, "history.json"), []byte(raw), 0o600); err != nil {
		t.Fatal(err)
	}
	if u := f.do("rollback", "--to", "1.0.0"); u.code != 3 || u.reason != "migration" {
		t.Fatalf("%+v", u)
	}
}

func TestHelpShowsRollbackTo(t *testing.T) {
	f := newFake2(t)
	var out strings.Builder
	f.app().Run(t.Context(), []string{"help", "--json"}, &sbWriter{&out}, &sbWriter{&strings.Builder{}})
	if !strings.Contains(out.String(), "An installed version to go back to") {
		t.Error("help does not list rollback --to")
	}
}

// A chain that does not end at the current version proves nothing.
func TestRollbackToWithABrokenChainIsRefused(t *testing.T) {
	f := threeVersions(t, false)
	raw := `{"current":"3.0.0","previous":"2.0.0","chain":[{"from":"1.0.0","to":"2.0.0","migrated":false}]}`
	if err := os.WriteFile(filepath.Join(f.state, "history.json"), []byte(raw), 0o600); err != nil {
		t.Fatal(err)
	}
	if u := f.do("rollback", "--to", "1.0.0"); u.code != 3 || u.reason != "migration" || f.current() != "3.0.0" {
		t.Fatalf("%+v", u)
	}
}

// The chain is bounded.
func TestTheChainIsBounded(t *testing.T) {
	f := newFake2(t)
	f.install1()
	f.do("update")
	for i := 0; i < 70; i++ {
		if u := f.do("rollback"); u.code != 0 {
			t.Fatalf("rollback %d: %+v", i, u)
		}
	}
	raw, err := os.ReadFile(filepath.Join(f.state, "history.json"))
	if err != nil {
		t.Fatal(err)
	}
	if n := strings.Count(string(raw), `"from"`); n != 64 {
		t.Errorf("the chain holds %d hops, want 64", n)
	}
}

// A down-migration crosses a migration on the way back, so a later rollback --to over it is refused.
func TestADownMigrationCountsAsCrossedInTheChain(t *testing.T) {
	f := newFake2(t)
	f.release("1.0.0", false)
	f.release("2.0.0", true)
	f.release("3.0.0", false)
	f.do("update", "--to", "1.0.0")
	f.do("update", "--to", "2.0.0")
	f.down = true
	if u := f.do("rollback"); u.code != 0 || f.current() != "1.0.0" {
		t.Fatalf("%+v", u)
	}
	if u := f.do("update", "--to", "3.0.0"); u.code != 0 {
		t.Fatalf("%+v", u)
	}
	if u := f.do("rollback", "--to", "2.0.0"); u.code != 3 || u.reason != "migration" {
		t.Fatalf("%+v", u)
	}
}

// A same-size write whose mtime is put back is still seen: the change time cannot be restored.
func TestACustodyWriteWithTheMtimeRestoredIsATouch(t *testing.T) {
	f := newFake2(t)
	p := custodyFile(t, f)
	past := time.Unix(1_000_000_000, 0)
	if err := os.Chtimes(p, past, past); err != nil {
		t.Fatal(err)
	}
	f.install1()
	f.custodyAct = map[string]string{"install": "rewrite"}
	if u := f.do("update"); u.reason != "protected" || f.current() != "1.0.0" {
		t.Fatalf("%+v current %s", u, f.current())
	}
}
