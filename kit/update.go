package kit

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// Schema of the update, rollback and recover document (rev d section 6).
const SchemaUpdate = "zeo.update/v1"

// Release is one fetched release, staged in a user-owned directory.
type Release struct {
	Version string
	// Dir is the staged tree the Install hook copies from.
	Dir string
	// SHA256 is the artifact's digest, as the Fetch hook computed it.
	SHA256 string
	// Migrates marks a release that changes a persistent state format.
	Migrates bool
	// Size is the installed size in bytes, for the free-space check; zero skips it.
	Size int64
}

// Updater is an app's update, rollback and recover: the six steps of rev d
// section 7 as one state machine over the app's hooks. The install root is
// user-owned; a root install goes through the helper (a later slice).
type Updater struct {
	// Root holds versions/<version>/ and the current symlink.
	Root string
	// Protected are paths no step may touch: key custody and append-only state.
	Protected []string
	// LiveReason is the refusal token when the app is live and holds no idle
	// fence. Default "live"; a studio sets "studio_live".
	LiveReason string

	// Fetch stages the release named by to ("" is the newest) and verifies it.
	Fetch func(ctx context.Context, to string) (Release, error)
	// Preflight adds the app's own refusals; return Refused(reason, ...).
	Preflight func(ctx context.Context, current string, rel Release) error
	// Install writes the release into dest. Nil copies Release.Dir.
	Install func(ctx context.Context, rel Release, dest string) error
	// Verify proves the version in dir by behaviour (its version, doctor, smoke).
	Verify func(ctx context.Context, dir string) error

	// Fence takes the app's idle fence; it is held through stop, switch and restart.
	Fence func(ctx context.Context) (release func(), err error)
	// Stop, Start and Ready are for a service. Ready must prove the running
	// process is version, with a readiness event at or after t0.
	Stop  func(context.Context) error
	Start func(context.Context) error
	Ready func(ctx context.Context, t0 time.Time, version string) error
	// Down reverses the state migration of the release being left, for a rollback.
	Down func(ctx context.Context, from, to string) error

	// Now is the clock; nil is time.Now.
	Now func() time.Time
}

var versionRe = regexp.MustCompile(`^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$`)

type step struct {
	Name   string `json:"name"`
	OK     bool   `json:"ok"`
	Detail string `json:"detail,omitempty"`
}

type updateDoc struct {
	Schema    string  `json:"schema"`
	App       string  `json:"app"`
	Verb      string  `json:"verb"`
	Outcome   Outcome `json:"outcome"`
	From      string  `json:"from"`
	To        string  `json:"to"`
	CheckOnly bool    `json:"check_only"`
	Steps     []step  `json:"steps"`
	Reason    string  `json:"reason,omitempty"`
	Error     string  `json:"error,omitempty"`
	Backup    string  `json:"backup,omitempty"`
	Undo      string  `json:"undo,omitempty"`
}

// intent is the durable record written before stop and switch. While it
// exists, every state-changing verb refuses; only recover clears it.
type intent struct {
	App  string `json:"app"`
	From string `json:"from"`
	To   string `json:"to"`
	Step string `json:"step"`
}

type history struct {
	Current  string `json:"current"`
	Previous string `json:"previous"`
	Migrated bool   `json:"migrated"`
}

func (u *Updater) now() time.Time {
	if u.Now != nil {
		return u.Now()
	}
	return time.Now()
}

func (a *App) updateCommands() []Command {
	if a.Updater == nil {
		return nil
	}
	exits := []ExitCode{ExitFailed, ExitRefused}
	return []Command{
		{Name: "update", Summary: "Fetch, verify, install beside and switch to a release; back out on any failure.",
			Flags:  []Flag{{Name: "to", Summary: "The version to install; default the newest.", TakesValue: true}, {Name: "check", Summary: "Stop after the preflight and change nothing."}},
			Output: SchemaUpdate, Exits: exits, Run: func(c *Context) error { return a.runChange(c, "update") }},
		{Name: "rollback", Summary: "Switch back to the previous version; refused across a state migration.",
			Output: SchemaUpdate, Exits: exits, Run: func(c *Context) error { return a.runChange(c, "rollback") }},
		{Name: "recover", Summary: "Prove one version is running after recovery_required, then clear the lock.",
			Output: SchemaUpdate, Exits: exits, Run: func(c *Context) error { return a.runChange(c, "recover") }},
	}
}

func (a *App) validateUpdater() error {
	u := a.Updater
	if u == nil {
		return nil
	}
	switch {
	case a.StateDir == "":
		return errors.New("kit: StateDir is required when the app has an Updater (the lock, intent and history live there)")
	case u.Root == "" || !filepath.IsAbs(u.Root):
		return errors.New("kit: Updater.Root must be an absolute path")
	case u.Fetch == nil || u.Verify == nil:
		return errors.New("kit: Updater needs Fetch and Verify")
	case (u.Stop == nil) != (u.Start == nil):
		return errors.New("kit: Updater Stop and Start come together")
	case u.Ready != nil && u.Start == nil:
		return errors.New("kit: Updater.Ready needs Stop and Start")
	}
	for _, p := range u.Protected {
		if !filepath.IsAbs(p) {
			return fmt.Errorf("kit: protected path %q must be absolute", p)
		}
		if within(u.Root, p) || within(p, u.Root) {
			return fmt.Errorf("kit: protected path %q overlaps the install root", p)
		}
	}
	return nil
}

// within reports whether path is dir or below it.
func within(dir, path string) bool {
	rel, err := filepath.Rel(filepath.Clean(dir), filepath.Clean(path))
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

// Guard refuses a path a step must not touch. Hooks call it before writing.
func (u *Updater) Guard(path string) error {
	for _, p := range u.Protected {
		if within(p, path) || within(path, p) {
			return Refused("protected", "%s is protected and no update step may touch it", path)
		}
	}
	return nil
}

// ---- state files --------------------------------------------------------

func (a *App) statePath(name string) string { return filepath.Join(a.StateDir, name) }

func writeFileAtomic(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".tmp-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmp.Name(), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), path)
}

func (a *App) readIntent() (*intent, error) {
	raw, err := os.ReadFile(a.statePath("intent.json"))
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var in intent
	if err := json.Unmarshal(raw, &in); err != nil {
		// An unreadable intent is still an intent: refuse rather than guess.
		return &intent{App: a.Name, Step: "unreadable"}, nil
	}
	return &in, nil
}

func (a *App) writeIntent(in intent) error {
	raw, _ := json.Marshal(in)
	return writeFileAtomic(a.statePath("intent.json"), raw)
}

func (a *App) clearIntent() error {
	err := os.Remove(a.statePath("intent.json"))
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	return err
}

func (a *App) readHistory() history {
	var h history
	if raw, err := os.ReadFile(a.statePath("history.json")); err == nil {
		_ = json.Unmarshal(raw, &h)
	}
	return h
}

func (a *App) writeHistory(h history) error {
	raw, _ := json.Marshal(h)
	return writeFileAtomic(a.statePath("history.json"), raw)
}

// lockOp takes the per-app operation lock shared by update, rollback, recover
// and apply. It is a flock, so it dies with the process: a crash never leaves
// the lock held, and the durable intent record is what outlives it.
func (a *App) lockOp() (func(), error) {
	if err := os.MkdirAll(a.StateDir, 0o700); err != nil {
		return nil, Failed("state directory: %s", oneLine(err.Error()))
	}
	f, err := os.OpenFile(a.statePath("op.lock"), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, Failed("operation lock: %s", oneLine(err.Error()))
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return nil, Refused("busy", "another operation is running for %s", a.Name)
	}
	return func() { _ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN); f.Close() }, nil
}

// refuseIfRecovery refuses every state-changing verb while an intent exists.
func (a *App) refuseIfRecovery() error {
	in, err := a.readIntent()
	if err != nil {
		return Failed("intent record: %s", oneLine(err.Error()))
	}
	if in != nil {
		return Refused("recovery_required", "an earlier operation did not finish (%s %s to %s, at %s); run `%s recover`", in.App, in.From, in.To, in.Step, a.Name)
	}
	return nil
}

// ---- layout -------------------------------------------------------------

func (u *Updater) versionDir(v string) string { return filepath.Join(u.Root, "versions", v) }
func (u *Updater) current() string            { return filepath.Join(u.Root, "current") }

func (u *Updater) currentVersion() string {
	target, err := os.Readlink(u.current())
	if err != nil {
		return ""
	}
	return filepath.Base(target)
}

// switchTo points current at version v with one rename(2) of a new symlink
// over it. A directory cannot be renamed over a non-empty one, so the link is
// the atomic part, and the previous version stays installed.
func (u *Updater) switchTo(v string) error {
	if err := os.MkdirAll(u.Root, 0o755); err != nil {
		return err
	}
	tmp := filepath.Join(u.Root, ".current-"+strconv.Itoa(os.Getpid()))
	_ = os.Remove(tmp)
	if err := os.Symlink(filepath.Join("versions", v), tmp); err != nil {
		return err
	}
	return os.Rename(tmp, u.current())
}

func compareVersions(a, b string) int {
	split := func(s string) []string {
		return strings.FieldsFunc(s, func(r rune) bool { return r == '.' || r == '-' || r == '+' || r == '_' })
	}
	pa, pb := split(a), split(b)
	for i := 0; i < len(pa) || i < len(pb); i++ {
		var x, y string
		if i < len(pa) {
			x = pa[i]
		}
		if i < len(pb) {
			y = pb[i]
		}
		nx, ex := strconv.Atoi(x)
		ny, ey := strconv.Atoi(y)
		switch {
		case ex == nil && ey == nil && nx != ny:
			if nx < ny {
				return -1
			}
			return 1
		case (ex != nil || ey != nil) && x != y:
			if x < y {
				return -1
			}
			return 1
		}
	}
	return 0
}

// snapshot fingerprints the protected paths, so a step that touched one is caught.
func (u *Updater) snapshot() (string, error) {
	var b strings.Builder
	for _, p := range u.Protected {
		err := filepath.WalkDir(p, func(path string, d fs.DirEntry, err error) error {
			if errors.Is(err, fs.ErrNotExist) {
				fmt.Fprintf(&b, "%s missing\n", path)
				return nil
			}
			if err != nil {
				return err
			}
			info, err := d.Info()
			if err != nil {
				return err
			}
			fmt.Fprintf(&b, "%s %s %d %d\n", path, info.Mode(), info.Size(), info.ModTime().UnixNano())
			return nil
		})
		if err != nil {
			return "", err
		}
	}
	return b.String(), nil
}

func copyTree(src, dst string) error {
	return filepath.WalkDir(src, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(src, path)
		target := filepath.Join(dst, rel)
		info, err := d.Info()
		if err != nil {
			return err
		}
		switch {
		case d.IsDir():
			return os.MkdirAll(target, 0o755)
		case info.Mode()&fs.ModeSymlink != 0:
			return fmt.Errorf("%s is a symlink; a release tree holds regular files only", path)
		case !info.Mode().IsRegular():
			return fmt.Errorf("%s is not a regular file", path)
		}
		in, err := os.Open(path)
		if err != nil {
			return err
		}
		defer in.Close()
		out, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, info.Mode().Perm())
		if err != nil {
			return err
		}
		if _, err := io.Copy(out, in); err != nil {
			out.Close()
			return err
		}
		return out.Close()
	})
}

// ---- the verb -----------------------------------------------------------

type run struct {
	a     *App
	u     *Updater
	c     *Context
	doc   updateDoc
	fence func()
}

func (r *run) step(name string, err error, detail string) {
	s := step{Name: name, OK: err == nil, Detail: detail}
	if err != nil {
		s.Detail = err.Error()
	}
	r.doc.Steps = append(r.doc.Steps, s)
}

// finish prints the document and the RESULT line, and returns the exit.
func (r *run) finish(o Outcome, reason string, cause error) error {
	r.doc.Outcome, r.doc.Reason = o, reason
	if cause != nil {
		r.doc.Error = cause.Error()
	}
	if r.c.JSON {
		if err := r.c.WriteJSON(r.doc); err != nil {
			return err
		}
	} else {
		fmt.Fprintf(r.c.Stdout, "%s %s: %s", r.a.Name, r.doc.Verb, o)
		if reason != "" {
			fmt.Fprintf(r.c.Stdout, " (%s)", reason)
		}
		fmt.Fprintln(r.c.Stdout)
		if cause != nil {
			fmt.Fprintf(r.c.Stderr, "%s: %v\n", r.a.Name, cause)
		}
	}
	if r.doc.CheckOnly && o == OutcomeOK {
		return nil
	}
	code, err := r.c.WriteResult(Result{Verb: r.doc.Verb, Outcome: o, From: r.doc.From, To: r.doc.To, Reason: reason, Undo: r.doc.Undo})
	if err != nil {
		return err
	}
	return ExitOnly(code)
}

func reasonOf(err error, def string) string {
	var e *Error
	if errors.As(err, &e) && e.Reason != "" {
		return e.Reason
	}
	return def
}

func (a *App) runChange(c *Context, verb string) error {
	u := a.Updater
	r := &run{a: a, u: u, c: c, doc: updateDoc{Schema: SchemaUpdate, App: a.Name, Verb: verb, Steps: []step{}}}
	_, r.doc.CheckOnly = c.Flag("check")
	r.doc.From = u.currentVersion()

	unlock, err := a.lockOp()
	if err != nil {
		return r.finish(OutcomeRefused, reasonOf(err, "busy"), err)
	}
	defer unlock()

	if verb == "recover" {
		return r.recover()
	}
	if err := a.refuseIfRecovery(); err != nil {
		return r.finish(OutcomeRefused, "recovery_required", err)
	}
	if verb == "rollback" {
		return r.rollback()
	}
	return r.update()
}

func (r *run) preflight(rel Release) (error, string) {
	u := r.u
	if cur := r.doc.From; cur != "" {
		switch cmp := compareVersions(rel.Version, cur); {
		case cmp == 0:
			return Refused("current", "%s is already the installed version", cur), "current"
		case cmp < 0 && r.doc.Verb == "update":
			return Refused("downgrade", "%s is older than the installed %s; use rollback", rel.Version, cur), "downgrade"
		}
	}
	if rel.Size > 0 {
		var st syscall.Statfs_t
		dir := u.Root
		for {
			if err := syscall.Statfs(dir, &st); err == nil || dir == filepath.Dir(dir) {
				break
			}
			dir = filepath.Dir(dir)
		}
		if free := int64(st.Bavail) * int64(st.Bsize); free < 2*rel.Size {
			return Refused("space", "%d bytes free, need %d", free, 2*rel.Size), "space"
		}
	}
	if st, err := r.a.Status(r.c.Context); err == nil && st.Live != nil && *st.Live {
		reason := u.LiveReason
		if reason == "" {
			reason = "live"
		}
		if u.Fence == nil {
			return Refused(reason, "%s is live and has no idle fence; stop it first", r.a.Name), reason
		}
		release, err := u.Fence(r.c.Context)
		if err != nil {
			return Refused(reason, "the idle fence is not free: %s", oneLine(err.Error())), reason
		}
		r.fence = release
	}
	if u.Preflight != nil {
		if err := u.Preflight(r.c.Context, r.doc.From, rel); err != nil {
			return err, reasonOf(err, "preflight")
		}
	}
	return nil, ""
}

func (r *run) update() error {
	u, ctx := r.u, r.c.Context
	to, _ := r.c.Flag("to")
	if to != "" && !versionRe.MatchString(to) {
		return Usagef("--to is not a version (letters, digits and . _ + - only, at most 64)")
	}
	rel, err := u.Fetch(ctx, to)
	r.step("fetch", err, "")
	if err != nil {
		return r.finish(OutcomeFailed, "fetch", err)
	}
	if !versionRe.MatchString(rel.Version) {
		err := fmt.Errorf("the release names version %q, which is not a safe path element", rel.Version)
		return r.finish(OutcomeRefused, "version", err)
	}
	r.doc.To = rel.Version
	pre, reason := r.preflight(rel)
	if r.fence != nil {
		defer r.fence()
	}
	r.step("preflight", pre, "")
	if pre != nil {
		return r.finish(OutcomeRefused, reason, pre)
	}
	if rel.Migrates {
		r.doc.Steps[len(r.doc.Steps)-1].Detail = "a state migration and a backup will run"
	}
	if r.doc.CheckOnly {
		return r.finish(OutcomeOK, "", nil)
	}
	before, err := u.snapshot()
	if err != nil {
		return r.finish(OutcomeFailed, "protected", err)
	}
	dest := u.versionDir(rel.Version)
	if err := u.Guard(dest); err != nil {
		return r.finish(OutcomeRefused, "protected", err)
	}
	if err := r.install(rel, dest); err != nil {
		return r.finish(OutcomeFailed, "install", err)
	}
	if after, err := u.snapshot(); err != nil || after != before {
		_ = os.RemoveAll(dest)
		return r.finish(OutcomeRefused, "protected", errors.New("a step touched a protected path; nothing was switched"))
	}
	return r.switchOver(rel.Version, rel.Migrates, false)
}

func (r *run) install(rel Release, dest string) error {
	u := r.u
	tmp := dest + ".tmp-" + strconv.Itoa(os.Getpid())
	_ = os.RemoveAll(tmp)
	if err := os.MkdirAll(tmp, 0o755); err != nil {
		r.step("install", err, "")
		return err
	}
	install := u.Install
	if install == nil {
		install = func(_ context.Context, rel Release, dest string) error { return copyTree(rel.Dir, dest) }
	}
	err := install(r.c.Context, rel, tmp)
	if err == nil {
		_ = os.RemoveAll(dest)
		err = os.Rename(tmp, dest)
	}
	if err != nil {
		_ = os.RemoveAll(tmp)
	}
	r.step("install", err, "beside the current version")
	return err
}

func (r *run) rollback() error {
	u := r.u
	h := r.a.readHistory()
	if h.Previous == "" || !versionRe.MatchString(h.Previous) {
		err := errors.New("there is no previous version to roll back to")
		return r.finish(OutcomeRefused, "no_previous", err)
	}
	r.doc.To = h.Previous
	if _, err := os.Stat(u.versionDir(h.Previous)); err != nil {
		return r.finish(OutcomeRefused, "no_previous", fmt.Errorf("version %s is no longer installed", h.Previous))
	}
	if h.Migrated && u.Down == nil {
		err := fmt.Errorf("%s migrated the state; the previous binary alone is not enough", r.doc.From)
		r.step("preflight", err, "")
		return r.finish(OutcomeRefused, "migration", err)
	}
	pre, reason := r.preflight(Release{Version: h.Previous})
	if r.fence != nil {
		defer r.fence()
	}
	r.step("preflight", pre, "")
	if pre != nil {
		return r.finish(OutcomeRefused, reason, pre)
	}
	return r.switchOver(h.Previous, false, h.Migrated)
}

// stopStart runs the optional service hooks.
func (r *run) hook(name string, f func(context.Context) error) error {
	if f == nil {
		return nil
	}
	err := f(r.c.Context)
	r.step(name, err, "")
	return err
}

// prove runs the checks that show version v is what runs now.
func (r *run) prove(v string, t0 time.Time) error {
	u := r.u
	if u.Ready != nil {
		err := u.Ready(r.c.Context, t0, v)
		r.step("ready", err, "")
		if err != nil {
			return err
		}
	}
	err := u.Verify(r.c.Context, u.current())
	r.step("verify", err, v)
	return err
}

// switchOver is steps 3 to 6 against version to.
func (r *run) switchOver(to string, migrates bool, down bool) error {
	u, a := r.u, r.a
	from := r.doc.From
	t0 := u.now()
	if err := a.writeIntent(intent{App: a.Name, From: from, To: to, Step: "switch"}); err != nil {
		return r.finish(OutcomeFailed, "intent", err)
	}
	// Before the switch nothing has changed in the installed state: a failure
	// here restarts the old version and ends failed.
	if err := r.hook("stop", u.Stop); err != nil {
		return r.restoreBeforeSwitch(err, "stop")
	}
	if down {
		if err := u.Down(r.c.Context, from, to); err != nil {
			r.step("down-migration", err, "")
			return r.restoreBeforeSwitch(err, "migration")
		}
		r.step("down-migration", nil, "")
	}
	if err := u.switchTo(to); err != nil {
		r.step("switch", err, "")
		return r.restoreBeforeSwitch(err, "switch")
	}
	r.step("switch", nil, from+" -> "+to)
	// From here the new version is current: any failure goes back.
	err := r.hook("start", u.Start)
	if err == nil {
		err = r.prove(to, t0)
	}
	if err != nil {
		return r.goBack(from, err)
	}
	h := history{Current: to, Previous: from, Migrated: migrates}
	if err := a.writeHistory(h); err != nil {
		return r.finish(OutcomeRecoveryRequired, "state", fmt.Errorf("switched, but the history record failed: %w", err))
	}
	if err := a.clearIntent(); err != nil {
		return r.finish(OutcomeRecoveryRequired, "state", fmt.Errorf("switched, but the intent record would not clear: %w", err))
	}
	if from != "" {
		r.doc.Backup = u.versionDir(from)
		r.doc.Undo = a.Name + " rollback"
	}
	return r.finish(OutcomeOK, "", nil)
}

func (r *run) restoreBeforeSwitch(cause error, reason string) error {
	u := r.u
	if u.Start != nil {
		if err := r.hook("start-old", u.Start); err != nil {
			return r.finish(OutcomeRecoveryRequired, "recovery_required", fmt.Errorf("%v; and the old version would not restart: %v", cause, err))
		}
	}
	if err := r.a.clearIntent(); err != nil {
		return r.finish(OutcomeRecoveryRequired, "state", err)
	}
	return r.finish(OutcomeFailed, reason, cause)
}

// goBack switches back to from, restarts and proves it. Proved: undone.
// Anything else: recovery_required, with the intent kept.
func (r *run) goBack(from string, cause error) error {
	u := r.u
	t0 := u.now()
	var err error
	if from == "" {
		err = os.Remove(u.current())
		r.step("switch-back", err, "no earlier version: current removed")
		if err == nil && u.Stop != nil {
			_ = r.hook("stop-new", u.Stop)
		}
	} else {
		if u.Stop != nil {
			_ = r.hook("stop-new", u.Stop)
		}
		err = u.switchTo(from)
		r.step("switch-back", err, "-> "+from)
		if err == nil {
			err = r.hook("start-old", u.Start)
		}
		if err == nil {
			err = r.prove(from, t0)
		}
	}
	if err != nil {
		return r.finish(OutcomeRecoveryRequired, "recovery_required", fmt.Errorf("%v; and going back failed: %v", cause, err))
	}
	if err := r.a.clearIntent(); err != nil {
		return r.finish(OutcomeRecoveryRequired, "state", err)
	}
	return r.finish(OutcomeUndone, reasonOf(cause, "verify"), cause)
}

// recover re-runs verify against both versions of the unfinished operation and
// clears the intent only when one is proved running.
func (r *run) recover() error {
	u, a := r.u, r.a
	in, err := a.readIntent()
	if err != nil {
		return r.finish(OutcomeFailed, "intent", err)
	}
	if in == nil {
		r.step("intent", nil, "nothing to recover")
		return r.finish(OutcomeOK, "", nil)
	}
	r.doc.From, r.doc.To = in.From, in.To
	var lastErr error
	for _, v := range []string{in.From, in.To} {
		if v == "" || !versionRe.MatchString(v) {
			continue
		}
		if _, err := os.Stat(u.versionDir(v)); err != nil {
			continue
		}
		t0 := u.now()
		if u.Stop != nil {
			_ = r.hook("stop", u.Stop)
		}
		if err := u.switchTo(v); err != nil {
			lastErr = err
			r.step("switch", err, v)
			continue
		}
		if err := r.hook("start", u.Start); err != nil {
			lastErr = err
			continue
		}
		if err := r.prove(v, t0); err != nil {
			lastErr = err
			continue
		}
		other := in.To
		if v == in.To {
			other = in.From
		}
		if err := a.writeHistory(history{Current: v, Previous: other}); err != nil {
			return r.finish(OutcomeRecoveryRequired, "state", err)
		}
		if err := a.clearIntent(); err != nil {
			return r.finish(OutcomeRecoveryRequired, "state", err)
		}
		r.doc.To = v
		return r.finish(OutcomeOK, "", nil)
	}
	if lastErr == nil {
		lastErr = errors.New("neither version is installed")
	}
	return r.finish(OutcomeRecoveryRequired, "recovery_required", lastErr)
}
