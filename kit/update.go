package kit

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
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
	// AppendOnly lists protected paths (files or trees) whose files may GROW
	// during an operation: append-only state. Every other protected file keeps
	// its exact size and bytes.
	AppendOnly []string
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

// validVersion is a version word that is safe as a path element. A name that
// holds ".tmp-" is refused: that is how an install in progress is marked, and
// the sweep of crashed installs would otherwise be able to remove it.
func validVersion(v string) bool {
	return versionRe.MatchString(v) && !strings.Contains(v, ".tmp-")
}

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
	// An apply leaves an intent too: Kind is "apply", and the subject, plan_id,
	// the backup and the changes let recover read the targets and tell
	// all-before from all-after.
	Kind    string   `json:"kind,omitempty"`
	Subject string   `json:"subject,omitempty"`
	PlanID  string   `json:"plan_id,omitempty"`
	Backup  string   `json:"backup,omitempty"`
	Changes []Change `json:"changes,omitempty"`
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
	exits := []ExitCode{ExitFailed, ExitRefused}
	recoverCmd := Command{Name: "recover", Summary: "Prove one version is running after recovery_required, or read an unfinished apply's targets, then clear the lock.",
		Output: SchemaUpdate, Exits: exits, Run: func(c *Context) error { return a.runChange(c, "recover") }}
	if a.Updater == nil {
		// An app with subjects but no updater still needs recover: a half-done
		// apply leaves an intent that only recover clears.
		if len(a.Subjects) > 0 {
			return []Command{recoverCmd}
		}
		return nil
	}
	return []Command{
		{Name: "update", Summary: "Fetch, verify, install beside and switch to a release; back out on any failure.",
			Flags:  []Flag{{Name: "to", Summary: "The version to install; default the newest.", TakesValue: true}, {Name: "check", Summary: "Stop after the preflight and change nothing."}},
			Output: SchemaUpdate, Exits: exits, Run: func(c *Context) error { return a.runChange(c, "update") }},
		{Name: "rollback", Summary: "Switch back to the previous version; refused across a state migration.",
			Output: SchemaUpdate, Exits: exits, Run: func(c *Context) error { return a.runChange(c, "rollback") }},
		recoverCmd,
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
		if overlaps(u.Root, p) {
			return fmt.Errorf("kit: protected path %q overlaps the install root", p)
		}
	}
	for _, p := range u.AppendOnly {
		in := false
		for _, q := range u.Protected {
			in = in || within(q, p)
		}
		if !filepath.IsAbs(p) || !in {
			return fmt.Errorf("kit: append-only path %q must be absolute and inside a protected path", p)
		}
	}
	return nil
}

// within reports whether path is dir or below it.
func within(dir, path string) bool {
	rel, err := filepath.Rel(filepath.Clean(dir), filepath.Clean(path))
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

// resolved is an absolute path with every symbolic link in its longest
// existing prefix evaluated and the rest appended, so a link into a protected
// area is seen for what it is.
func resolved(p string) string {
	p = filepath.Clean(p)
	rest := ""
	for q := p; ; q = filepath.Dir(q) {
		if r, err := filepath.EvalSymlinks(q); err == nil {
			return filepath.Join(r, rest)
		}
		if q == filepath.Dir(q) {
			return p
		}
		rest = filepath.Join(filepath.Base(q), rest)
	}
}

// overlaps says whether two absolute paths are the same or one is below the
// other, compared as written and with symbolic links resolved.
func overlaps(a, b string) bool {
	if within(a, b) || within(b, a) {
		return true
	}
	ra, rb := resolved(a), resolved(b)
	return within(ra, rb) || within(rb, ra) || within(a, rb) || within(rb, a) || within(ra, b) || within(b, ra)
}

// Guard refuses a path a step must not touch. Hooks call it before writing.
// The path must be absolute; links are resolved on both sides.
func (u *Updater) Guard(path string) error {
	if !filepath.IsAbs(path) {
		return Refused("protected", "a path to guard must be absolute")
	}
	// ".." is refused outright: collapsing it as text before resolving a link
	// would hide a path that the kernel opens elsewhere.
	for _, el := range strings.Split(path, string(filepath.Separator)) {
		if el == ".." {
			return Refused("protected", "a path to guard must not contain ..")
		}
	}
	for _, p := range u.Protected {
		if overlaps(p, path) {
			return Refused("protected", "a path overlaps a protected path and no update step may touch it")
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
	if err := os.Rename(tmp.Name(), path); err != nil {
		return err
	}
	return syncDir(filepath.Dir(path))
}

// syncDir makes a rename or a removal in dir durable. (File.Sync is F_FULLFSYNC
// on macOS.)
func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
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
	if err != nil {
		return err
	}
	return syncDir(a.StateDir)
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
		what := "an update"
		if in.Kind == "apply" {
			what = "an apply of " + shown(in.Subject)
		}
		return Refused("recovery_required", "an earlier operation did not finish (%s, at %s); run `%s recover`", what, shown(in.Step), a.Name)
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
	if err := os.Rename(tmp, u.current()); err != nil {
		return err
	}
	return syncDir(u.Root)
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

// protectedState is what the protected paths held at one moment: every entry
// with its mode, and for a regular file its size and the sha256 of those
// bytes. A file may GROW (append-only state is appended to) but its first
// bytes may not change; an entry may not vanish or appear.
type protectedState map[string]fileState

type fileState struct {
	Mode fs.FileMode
	Size int64
	Sum  string // sha256 of the first Size bytes, for a regular file
	Link string // the target of a symbolic link
}

func (u *Updater) snapshot() (protectedState, error) {
	st := protectedState{}
	for _, p := range u.Protected {
		// A protected path that is itself a link is followed: the entries are
		// keyed by the resolved path, so a link retargeted elsewhere changes
		// every key and is seen as a touch.
		root := p
		if r, err := filepath.EvalSymlinks(p); err == nil {
			root = r
		}
		err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
			if errors.Is(err, fs.ErrNotExist) {
				st[path] = fileState{Mode: fs.ModeIrregular}
				return nil
			}
			if err != nil {
				return err
			}
			info, err := d.Info()
			if err != nil {
				return err
			}
			state := fileState{Mode: info.Mode()}
			switch {
			case info.Mode().IsRegular():
				sum, err := prefixSum(path, info.Size())
				if err != nil {
					return err
				}
				state.Size, state.Sum = info.Size(), sum
			case info.Mode()&fs.ModeSymlink != 0:
				if target, err := os.Readlink(path); err == nil {
					state.Link = target
				}
			}
			st[path] = state
			return nil
		})
		if err != nil {
			return nil, err
		}
	}
	return st, nil
}

func prefixSum(path string, n int64) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.CopyN(h, f, n); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func (u *Updater) appendOnly(path string) bool {
	for _, p := range u.AppendOnly {
		if within(p, path) {
			return true
		}
	}
	return false
}

// touched says whether the protected paths changed since before: an entry that
// vanished, appeared or changed mode or link target, or a file whose bytes
// changed. Only a file under an AppendOnly path may grow, and then its first
// bytes may not change.
func (u *Updater) touched(before protectedState) (bool, error) {
	now, err := u.snapshot()
	if err != nil {
		return true, err
	}
	if len(now) != len(before) {
		return true, nil
	}
	for path, was := range before {
		is, ok := now[path]
		if !ok || is.Mode != was.Mode || is.Link != was.Link {
			return true, nil
		}
		if was.Mode.IsRegular() {
			if is.Size != was.Size && !u.appendOnly(path) {
				return true, nil
			}
			// The first Size bytes must be unchanged. A file that shrank cannot
			// supply them (the read comes up short and fails), so it is a touch.
			sum, err := prefixSum(path, was.Size)
			if err != nil || sum != was.Sum {
				return true, nil
			}
		}
	}
	return false, nil
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
	// protectedBefore is taken before Fetch (and before a rollback's preflight):
	// no step may touch a protected path.
	protectedBefore protectedState
}

// touchedProtected reports a step that changed a protected path since the
// snapshot. A snapshot that cannot be read counts as touched.
func (r *run) touchedProtected() bool {
	if r.protectedBefore == nil {
		return false
	}
	t, _ := r.u.touched(r.protectedBefore)
	return t
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
			fmt.Fprintf(r.c.Stdout, " (%s)", shown(reason))
		}
		fmt.Fprintln(r.c.Stdout)
		if cause != nil {
			fmt.Fprintf(r.c.Stderr, "%s: %s\n", r.a.Name, oneLine(cause.Error()))
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
	if u != nil {
		r.doc.From = u.currentVersion()
	}

	// update --check writes nothing, so it takes no lock and creates no state.
	if !(r.doc.CheckOnly && verb == "update") {
		unlock, err := a.lockOp()
		if err != nil {
			var e *Error
			if errors.As(err, &e) && e.Code == ExitFailed {
				return r.finish(OutcomeFailed, "state", err)
			}
			return r.finish(OutcomeRefused, reasonOf(err, "busy"), err)
		}
		defer unlock()
	}

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
	if reason, err := r.liveGate(); err != nil {
		return err, reason
	}
	if u.Preflight != nil {
		if err := u.Preflight(r.c.Context, r.doc.From, rel); err != nil {
			return err, reasonOf(err, "preflight")
		}
	}
	return nil, ""
}

// liveGate applies the live rule. A Status that cannot answer is treated as
// live: while live is unknown the operation refuses (fail closed). A live app
// with an idle fence takes it, held until the run ends; one without refuses.
func (r *run) liveGate() (string, error) {
	u := r.u
	st, err := r.a.Status(r.c.Context)
	if err == nil && (st.Live == nil || !*st.Live) {
		return "", nil
	}
	reason := u.LiveReason
	if reason == "" {
		reason = "live"
	}
	if u.Fence == nil {
		if err != nil {
			return reason, Refused(reason, "%s cannot say whether it is live and has no idle fence; stop it first", r.a.Name)
		}
		return reason, Refused(reason, "%s is live and has no idle fence; stop it first", r.a.Name)
	}
	release, ferr := u.Fence(r.c.Context)
	if ferr != nil {
		return reason, Refused(reason, "the idle fence is not free: %s", oneLine(ferr.Error()))
	}
	r.fence = release
	return "", nil
}

func (r *run) update() error {
	u, ctx := r.u, r.c.Context
	to, _ := r.c.Flag("to")
	if to != "" && !validVersion(to) {
		return Usagef("--to is not a version (letters, digits and . _ + - only, at most 64)")
	}
	if snap, err := u.snapshot(); err != nil {
		return r.finish(OutcomeFailed, "protected", err)
	} else {
		r.protectedBefore = snap
	}
	rel, err := u.Fetch(ctx, to)
	r.step("fetch", err, "")
	if err != nil {
		return r.finish(OutcomeFailed, "fetch", err)
	}
	if !validVersion(rel.Version) {
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
	dest := u.versionDir(rel.Version)
	if err := u.Guard(dest); err != nil {
		return r.finish(OutcomeRefused, "protected", err)
	}
	if err := r.install(rel, dest); err != nil {
		return r.finish(OutcomeFailed, "install", err)
	}
	if r.touchedProtected() {
		_ = os.RemoveAll(dest)
		return r.finish(OutcomeFailed, "protected", errors.New("a step touched a protected path; nothing was switched. The touch is detected, not undone"))
	}
	return r.switchOver(rel.Version, rel.Migrates, false)
}

// sweepInstallTemps removes install directories (versions/*.tmp-<pid>) that a
// crashed install left behind. One whose process is still alive is kept.
func (u *Updater) sweepInstallTemps() {
	ents, err := os.ReadDir(filepath.Join(u.Root, "versions"))
	if err != nil {
		return
	}
	for _, e := range ents {
		i := strings.LastIndex(e.Name(), ".tmp-")
		if i < 0 {
			continue
		}
		pid, err := strconv.Atoi(e.Name()[i+len(".tmp-"):])
		if err != nil || pid <= 0 || pid == os.Getpid() {
			continue
		}
		if err := syscall.Kill(pid, 0); err == nil || errors.Is(err, syscall.EPERM) {
			continue
		}
		_ = os.RemoveAll(filepath.Join(u.Root, "versions", e.Name()))
	}
}

func (r *run) install(rel Release, dest string) error {
	u := r.u
	u.sweepInstallTemps()
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
	if snap, err := u.snapshot(); err != nil {
		return r.finish(OutcomeFailed, "protected", err)
	} else {
		r.protectedBefore = snap
	}
	h := r.a.readHistory()
	if h.Previous == "" || !validVersion(h.Previous) {
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
	if r.touchedProtected() {
		err := errors.New("a step touched a protected path; nothing was switched")
		r.step("protected", err, "")
		return r.restoreBeforeSwitch(err, "protected")
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
	if err == nil && r.touchedProtected() {
		err = Refused("protected", "a step touched a protected path; going back")
		r.step("protected", err, "")
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
		if from != "" {
			r.doc.Backup = u.versionDir(from)
		}
		return r.finish(OutcomeRecoveryRequired, "recovery_required", fmt.Errorf("%v; and going back failed: %v", cause, err))
	}
	if err := r.a.clearIntent(); err != nil {
		return r.finish(OutcomeRecoveryRequired, "state", err)
	}
	return r.finish(OutcomeUndone, reasonOf(cause, "verify"), cause)
}

// recover clears an unfinished operation, and it looks BEFORE it acts.
//
// First it proves the version that is current as it stands, without stopping
// anything: if one of the two versions of the intent is running and passes
// ready and verify, the crash came after the proof and only the record is
// left, so the record is cleared and nothing is stopped. Only if that fails
// does it act: it applies the live rule (a live app with no idle fence is
// refused, one with a fence takes it), then goes through the two versions as
// before, From first. An apply intent is recovered by reading the targets:
// all at their before values, or all at their after values, clears it; a mix
// stays recovery_required with the backup path.
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
	if in.Kind == "apply" {
		return r.recoverApply(in)
	}
	if u == nil {
		return r.finish(OutcomeRecoveryRequired, "recovery_required", errors.New("this app has no updater, so an unfinished update cannot be recovered here"))
	}
	r.doc.From, r.doc.To = in.From, in.To
	if snap, err := u.snapshot(); err != nil {
		return r.finish(OutcomeFailed, "protected", err)
	} else {
		r.protectedBefore = snap
	}
	// 1. Prove what is running now, without touching it.
	if cur := u.currentVersion(); cur != "" && validVersion(cur) &&
		(in.Step == "unreadable" || cur == in.From || cur == in.To) {
		err := r.proveAsIs(cur)
		if err == nil && !r.touchedProtected() {
			other := in.To
			if cur == in.To {
				other = in.From
			}
			if err := a.writeHistory(history{Current: cur, Previous: other}); err != nil {
				return r.finish(OutcomeRecoveryRequired, "state", err)
			}
			if err := a.clearIntent(); err != nil {
				return r.finish(OutcomeRecoveryRequired, "state", err)
			}
			r.doc.To = cur
			return r.finish(OutcomeOK, "", nil)
		}
	}
	// 2. It must act, so the live rule applies.
	if reason, err := r.liveGate(); err != nil {
		return r.finish(OutcomeRefused, reason, err)
	}
	if r.fence != nil {
		defer r.fence()
	}
	var lastErr error
	for _, v := range []string{in.From, in.To} {
		if v == "" || !validVersion(v) {
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
		if r.touchedProtected() {
			lastErr = Refused("protected", "a step touched a protected path")
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
	if in.From != "" && validVersion(in.From) {
		r.doc.Backup = u.versionDir(in.From)
	}
	return r.finish(OutcomeRecoveryRequired, "recovery_required", lastErr)
}

// proveAsIs checks the running version without stopping it. A readiness event
// at any time counts (the zero t0): the process has been up since the crash.
func (r *run) proveAsIs(v string) error {
	u := r.u
	if u.Ready != nil {
		err := u.Ready(r.c.Context, time.Time{}, v)
		r.step("ready", err, "as it stands")
		if err != nil {
			return err
		}
	}
	err := u.Verify(r.c.Context, u.current())
	r.step("verify", err, v+" as it stands")
	return err
}

// recoverApply recovers an apply that did not finish.
func (r *run) recoverApply(in *intent) error {
	a := r.a
	s, err := a.subject(in.Subject)
	if err != nil {
		return r.finish(OutcomeRecoveryRequired, "recovery_required", err)
	}
	targets := make([]string, 0, len(in.Changes))
	for _, ch := range in.Changes {
		targets = append(targets, ch.Target)
	}
	now, err := s.Read(r.c.Context, targets)
	if err != nil {
		return r.finish(OutcomeRecoveryRequired, "recovery_required", fmt.Errorf("the targets cannot be read: %s", oneLine(err.Error())))
	}
	allBefore, allAfter := true, true
	for _, ch := range in.Changes {
		v, ok := now[ch.Target]
		allBefore = allBefore && ok && v == ch.Before
		allAfter = allAfter && ok && v == ch.After
	}
	r.step("targets", nil, map[bool]string{true: "all at their after values", false: "read"}[allAfter && !allBefore])
	if allBefore || allAfter {
		if err := a.clearIntent(); err != nil {
			return r.finish(OutcomeRecoveryRequired, "state", err)
		}
		return r.finish(OutcomeOK, "", nil)
	}
	r.doc.Backup = in.Backup
	return r.finish(OutcomeRecoveryRequired, "recovery_required", errors.New("the targets are partly changed; the backup holds the plan that put them back"))
}
