package kit_test

import (
	"os"
	"path/filepath"
	"testing"
)

func mk(t *testing.T, p string) string {
	t.Helper()
	if err := os.MkdirAll(p, 0o700); err != nil {
		t.Fatal(err)
	}
	return p
}

func link(t *testing.T, target, name string) string {
	t.Helper()
	if err := os.Symlink(target, name); err != nil {
		t.Fatal(err)
	}
	return name
}

// Round 4, P2-B3: paths that merely share an ancestor, or do not exist yet, do not overlap.
func TestGuardAcceptsPathsThatOnlyShareAnAncestor(t *testing.T) {
	f := newFake2(t)
	base := t.TempDir()
	app := mk(t, filepath.Join(base, "app"))
	keys := mk(t, filepath.Join(app, "keys"))
	u := f.app().Updater
	u.Protected = []string{keys}
	for _, ok := range []string{
		filepath.Join(app, "new-state.json"),
		filepath.Join(app, "data", "x"),
		filepath.Join(base, "other", "x"),
	} {
		if err := u.Guard(ok); err != nil {
			t.Errorf("Guard(%s) refused a path outside the protected tree: %v", ok, err)
		}
	}
	for _, bad := range []string{keys, filepath.Join(keys, "new", "deep")} {
		if err := u.Guard(bad); err == nil {
			t.Errorf("Guard(%s) accepted a protected path", bad)
		}
	}
	// A protected file that does not exist yet.
	u.Protected = []string{filepath.Join(app, "custody", "approver.key")}
	for _, ok := range []string{filepath.Join(app, "new.txt"), filepath.Join(app, "custody", "other.txt")} {
		if err := u.Guard(ok); err != nil {
			t.Errorf("Guard(%s) refused a sibling of a not-yet-existing protected file: %v", ok, err)
		}
	}
	for _, bad := range []string{u.Protected[0], filepath.Join(u.Protected[0], "x")} {
		if err := u.Guard(bad); err == nil {
			t.Errorf("Guard(%s) accepted a not-yet-existing protected path", bad)
		}
	}
	// A protected tree whose parent is missing.
	u.Protected = []string{filepath.Join(base, "nothere", "keys")}
	if err := u.Guard(filepath.Join(app, "existing.json")); err != nil {
		t.Errorf("a missing protected parent refused an unrelated path: %v", err)
	}
	if err := u.Guard(filepath.Join(base, "nothere", "keys", "k")); err == nil {
		t.Error("a path below a missing protected tree was accepted")
	}
}

// Round 4, P2-B3: Root and keys side by side may be missing on a first run.
func TestRootAndKeysSideBySideMayBeMissing(t *testing.T) {
	base := t.TempDir()
	for _, c := range []struct{ root, keys string }{
		{filepath.Join(base, "a1", "releases"), mk(t, filepath.Join(base, "a1", "keys"))},
		{mk(t, filepath.Join(base, "a2", "releases")), filepath.Join(base, "a2", "keys")},
		{filepath.Join(base, "a3", "releases"), filepath.Join(base, "a3", "keys")},
	} {
		f := newFake2(t)
		app := f.app()
		app.Updater.Root, app.Updater.Protected = c.root, []string{c.keys}
		if err := app.Validate(); err != nil {
			t.Errorf("root %s keys %s: %v", c.root, c.keys, err)
		}
	}
	f := newFake2(t)
	app := f.app()
	app.Updater.Root, app.Updater.Protected = filepath.Join(base, "a1"), []string{filepath.Join(base, "a1", "keys")}
	if err := app.Validate(); err == nil {
		t.Error("keys inside the root were accepted")
	}
}

// Round 4, P2-A2: every directory the kernel passes through is compared, so a link into a
// subdirectory of the protected tree, a relative link and a chain of links are all caught.
func TestGuardFollowsLinksComponentByComponent(t *testing.T) {
	f := newFake2(t)
	base := t.TempDir()
	keys := mk(t, filepath.Join(base, "keys"))
	mk(t, filepath.Join(keys, "sub"))
	other := mk(t, filepath.Join(base, "other"))
	u := f.app().Updater
	u.Protected = []string{keys}
	into := link(t, filepath.Join(keys, "sub"), filepath.Join(other, "into-sub"))
	rel := link(t, filepath.Join("..", "keys"), filepath.Join(other, "rel"))
	l1 := link(t, rel, filepath.Join(other, "chain"))
	up := link(t, filepath.Join(other, "..", "keys", "sub"), filepath.Join(other, "dotdot"))
	for _, p := range []string{into, filepath.Join(into, "s"), rel, filepath.Join(rel, "k"), filepath.Join(l1, "k"), filepath.Join(up, "s")} {
		if err := u.Guard(p); err == nil {
			t.Errorf("Guard(%s) accepted a path into the protected tree through a link", p)
		}
	}
	// A link elsewhere is fine, and so is the directory that holds the links.
	away := link(t, filepath.Join(base, "other"), filepath.Join(other, "self"))
	for _, p := range []string{filepath.Join(away, "x"), filepath.Join(other, "x")} {
		if err := u.Guard(p); err != nil {
			t.Errorf("Guard(%s) refused: %v", p, err)
		}
	}
	// A link that loops is refused.
	loop := link(t, filepath.Join(other, "loop"), filepath.Join(other, "loop"))
	if err := u.Guard(filepath.Join(loop, "x")); err == nil {
		t.Error("a looping link was accepted")
	}
}

// A protected root that is a link: a path through the target is caught, and the reverse.
func TestGuardWithALinkedProtectedRootAndAnAbsoluteLink(t *testing.T) {
	f := newFake2(t)
	base := t.TempDir()
	real := mk(t, filepath.Join(base, "real"))
	mk(t, filepath.Join(real, "sub"))
	lnk := link(t, real, filepath.Join(base, "keys"))
	u := f.app().Updater
	u.Protected = []string{lnk}
	if err := u.Guard(filepath.Join(real, "sub", "x")); err == nil {
		t.Error("the real path under a linked protected root was accepted")
	}
	abs := link(t, filepath.Join(real, "sub"), filepath.Join(mk(t, filepath.Join(base, "o")), "abs"))
	if err := u.Guard(filepath.Join(abs, "x")); err == nil {
		t.Error("an absolute link into the protected tree was accepted")
	}
}

// A protected path that cannot be resolved fails closed.
func TestAProtectedDanglingLinkRefusesEverything(t *testing.T) {
	f := newFake2(t)
	base := t.TempDir()
	u := f.app().Updater
	u.Protected = []string{link(t, filepath.Join(base, "gone"), filepath.Join(base, "keys"))}
	if err := u.Guard(filepath.Join(t.TempDir(), "x")); err == nil {
		t.Error("an unresolvable protected path accepted a write")
	}
}

// A path through a link that does not resolve is refused even with nothing protected.
func TestGuardRefusesADanglingLinkWithNothingProtected(t *testing.T) {
	f := newFake2(t)
	base := t.TempDir()
	u := f.app().Updater
	u.Protected = nil
	l := link(t, filepath.Join(base, "gone"), filepath.Join(base, "lnk"))
	if err := u.Guard(filepath.Join(l, "x")); err == nil {
		t.Error("a dangling link was accepted")
	}
}

// ".." is refused outright, even where resolving it would be harmless.
func TestGuardRefusesDotDotEvenWhenHarmless(t *testing.T) {
	f := newFake2(t)
	base := t.TempDir()
	u := f.app().Updater
	u.Protected = []string{mk(t, filepath.Join(base, "keys"))}
	mk(t, filepath.Join(base, "free"))
	if err := u.Guard(base + "/free/../free/x"); err == nil {
		t.Error("a path with .. was accepted")
	}
}
