// Package conformance is the suite an app runs against its own binary to prove
// it speaks zeo-cli/v1: the shapes of help, version, doctor and status, the
// exit table's rows and the exit codes 0, 2 and 7 (and 1 for a failing check),
// single-dash refusal, and --json purity. ResultLine checks a RESULT line
// against the grammar; the suite does not run an app's state-changing verbs, so
// it never captures a RESULT line itself. zeocore runs it against a sample app;
// zeocouncil adopts it.
package conformance

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"
)

// Config names the binary under test.
type Config struct {
	// Binary is the path of the app's executable.
	Binary string
	// Env is added to the process environment (the binary gets nothing else
	// unless it is listed here).
	Env []string
	// Name is the app name help --json must report.
	Name string
	// FailingDoctorEnv, when set, is an environment entry that makes doctor
	// fail, so exit 7 can be checked. Leave it empty to skip that check.
	FailingDoctorEnv string
	// SharedSubject, when set, names a subject that changes shared state and
	// has something to plan; the suite then drives plan, apply and the undo.
	// The app must start with that subject in its planned-for-change state.
	SharedSubject string
}

type outcome struct {
	stdout, stderr string
	code           int
}

func run(t *testing.T, cfg Config, env []string, args ...string) outcome {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, cfg.Binary, args...)
	cmd.Env = append(append([]string{}, cfg.Env...), env...)
	var so, se bytes.Buffer
	cmd.Stdout, cmd.Stderr = &so, &se
	// stdin is nil (the null device): a verb that waits for input would see EOF
	// at once and so can never hang the suite, and no verb may prompt.
	err := cmd.Run()
	code := 0
	if err != nil {
		ee, ok := err.(*exec.ExitError)
		if !ok {
			t.Fatalf("running %s %v: %v", cfg.Binary, args, err)
		}
		code = ee.ExitCode()
	}
	return outcome{so.String(), se.String(), code}
}

// one decodes stdout as exactly one JSON document and nothing else.
func one(t *testing.T, label, stdout string) map[string]any {
	t.Helper()
	dec := json.NewDecoder(strings.NewReader(stdout))
	var doc map[string]any
	if err := dec.Decode(&doc); err != nil {
		t.Fatalf("%s: stdout is not a JSON document: %v\n%q", label, err, stdout)
	}
	if dec.More() {
		t.Fatalf("%s: stdout holds more than one document: %q", label, stdout)
	}
	if rest := strings.TrimSpace(stdout[dec.InputOffset():]); rest != "" {
		t.Fatalf("%s: text after the document: %q", label, rest)
	}
	return doc
}

func firstKey(t *testing.T, label, stdout string) string {
	t.Helper()
	dec := json.NewDecoder(strings.NewReader(stdout))
	if tok, err := dec.Token(); err != nil || tok != json.Delim('{') {
		t.Fatalf("%s: not an object", label)
	}
	tok, err := dec.Token()
	if err != nil {
		t.Fatalf("%s: %v", label, err)
	}
	key, _ := tok.(string)
	return key
}

// word is printable ASCII without a quote or an equals sign, so no control
// character, Unicode space or line separator is ever part of a RESULT line.
var resultRe = regexp.MustCompile(`^RESULT [!#-<>-~]+ [!#-<>-~]+ (ok|refused|failed|undone|recovery_required)( [a-z][a-z0-9_]*=([!#-<>-~]+|"([^"\\\x00-\x1f\x7f]|\\[^\x00-\x1f\x7f])*"))*$`)

// ResultLine reports whether a line follows the RESULT grammar of rev d section 5.
func ResultLine(line string) bool { return resultRe.MatchString(line) }

// Run runs the whole suite.
func Run(t *testing.T, cfg Config) {
	t.Helper()
	if cfg.Binary == "" || cfg.Name == "" {
		t.Fatal("conformance: Binary and Name are required")
	}
	var commands []map[string]any
	t.Run("help_json_is_the_catalogue", func(t *testing.T) {
		o := run(t, cfg, nil, "help", "--json")
		if o.code != 0 {
			t.Fatalf("help --json exit %d: %s", o.code, o.stderr)
		}
		if k := firstKey(t, "help", o.stdout); k != "schema" {
			t.Fatalf("first field is %q, not schema", k)
		}
		doc := one(t, "help", o.stdout)
		if doc["schema"] != "zeo.help/v1" || doc["contract"] != "zeo-cli/v1" || doc["app"] != cfg.Name {
			t.Fatalf("help header wrong: %v", doc)
		}
		exits, _ := doc["exits"].([]any)
		if len(exits) != 8 {
			t.Fatalf("exit table has %d rows, want 8", len(exits))
		}
		for i, row := range exits {
			m, _ := row.(map[string]any)
			if code, ok := m["code"].(float64); !ok || int(code) != i {
				t.Fatalf("exit table row %d is %v; the table is codes 0 to 7 in order", i, row)
			}
		}
		list, _ := doc["commands"].([]any)
		have := map[string]bool{}
		for _, c := range list {
			m := c.(map[string]any)
			commands = append(commands, m)
			have[m["name"].(string)] = true
		}
		for _, verb := range []string{"help", "version", "doctor", "status"} {
			if !have[verb] {
				t.Fatalf("kit verb %q is missing from help", verb)
			}
		}
	})
	t.Run("help_text_is_one_line_per_command", func(t *testing.T) {
		o := run(t, cfg, nil, "help")
		if o.code != 0 {
			t.Fatalf("help exit %d", o.code)
		}
		for _, c := range commands {
			name := c["name"].(string)
			n := 0
			for _, line := range strings.Split(o.stdout, "\n") {
				if f := strings.Fields(line); len(f) > 0 && f[0] == name {
					n++
				}
			}
			if n != 1 {
				t.Errorf("command %q appears on %d help lines, want 1", name, n)
			}
		}
	})
	t.Run("every_command_in_help_exists", func(t *testing.T) {
		// An unknown command also exits 2, so exit 2 alone proves nothing.
		// The control is a command that cannot exist: its refusal must not be
		// the one a real command gives for an unknown flag.
		control := run(t, cfg, nil, "zz-no-such-command-zz", "--no-such-flag")
		if control.code != 2 {
			t.Fatalf("an unknown command exits %d, want 2", control.code)
		}
		for _, c := range commands {
			name := c["name"].(string)
			o := run(t, cfg, nil, name, "--no-such-flag")
			if o.code != 2 {
				t.Errorf("%s --no-such-flag: exit %d, want 2", name, o.code)
			}
			if !strings.Contains(o.stderr, "flag") || o.stderr == control.stderr {
				t.Errorf("%s --no-such-flag: stderr %q does not refuse the flag; a listed command must exist", name, o.stderr)
			}
		}
	})
	t.Run("version", func(t *testing.T) {
		o := run(t, cfg, nil, "version", "--json")
		doc := one(t, "version", o.stdout)
		if firstKey(t, "version", o.stdout) != "schema" || doc["schema"] != "zeo.version/v1" {
			t.Fatalf("version document wrong: %v", doc)
		}
		switch doc["signature"] {
		case "verified", "unsigned", "invalid", "unknown_signer":
		default:
			t.Fatalf("signature %v is not in the enum", doc["signature"])
		}
	})
	t.Run("doctor", func(t *testing.T) {
		o := run(t, cfg, nil, "doctor", "--json")
		doc := one(t, "doctor", o.stdout)
		if doc["schema"] != "zeo.doctor/v1" {
			t.Fatalf("doctor schema %v", doc["schema"])
		}
		ready, _ := doc["ready"].(bool)
		if (o.code == 0) != ready || (o.code != 0 && o.code != 7) {
			t.Fatalf("doctor exit %d disagrees with ready=%v", o.code, ready)
		}
		checks, isList := doc["checks"].([]any)
		if !isList {
			t.Fatalf("doctor checks is %v, want a list (empty is [], never null)", doc["checks"])
		}
		for _, ck := range checks {
			m, _ := ck.(map[string]any)
			next, _ := m["next"].(string)
			if ok, _ := m["ok"].(bool); !ok && m["skipped"] != true && next == "" {
				t.Errorf("failing check %v names no next step", m["name"])
			}
		}
		if cfg.FailingDoctorEnv != "" {
			f := run(t, cfg, []string{cfg.FailingDoctorEnv}, "doctor", "--json")
			if f.code != 7 {
				t.Errorf("a failing doctor exits %d, want 7", f.code)
			}
			if d := one(t, "failing doctor", f.stdout); d["ready"] != false {
				t.Errorf("failing doctor still prints a document with ready=false: %v", d)
			}
		}
	})
	t.Run("status", func(t *testing.T) {
		o := run(t, cfg, nil, "status", "--json")
		doc := one(t, "status", o.stdout)
		if doc["schema"] != "zeo.status/v1" {
			t.Fatalf("status schema %v", doc["schema"])
		}
		healthy, _ := doc["healthy"].(bool)
		if (o.code == 0) != healthy {
			t.Fatalf("status exit %d disagrees with healthy=%v", o.code, healthy)
		}
		for _, k := range []string{"state", "live", "counts"} {
			if _, ok := doc[k]; !ok {
				t.Errorf("status document lacks %q", k)
			}
		}
	})
	t.Run("single_dash_is_refused", func(t *testing.T) {
		for _, args := range [][]string{{"-json"}, {"help", "-json"}, {"version", "-x=1"}} {
			o := run(t, cfg, nil, args...)
			if o.code != 2 || !strings.Contains(o.stderr, "--") || o.stdout != "" {
				t.Errorf("%v: exit %d stdout %q stderr %q; want exit 2, nothing on stdout, and a message naming the double-dash form", args, o.code, o.stdout, o.stderr)
			}
		}
	})
	t.Run("usage_errors", func(t *testing.T) {
		for _, args := range [][]string{{}, {"no-such-verb"}, {"help", "--json=true"}, {"help", "extra"}} {
			if o := run(t, cfg, nil, args...); o.code != 2 {
				t.Errorf("%v: exit %d, want 2", args, o.code)
			}
		}
	})
	t.Run("json_stdout_is_one_document_and_errors_stay_on_stderr", func(t *testing.T) {
		for _, c := range commands {
			name := c["name"].(string)
			if name != "help" && name != "version" && name != "doctor" && name != "status" {
				continue
			}
			o := run(t, cfg, nil, name, "--json")
			one(t, name, o.stdout)
		}
		o := run(t, cfg, nil, "version", "--json", "--bogus")
		if o.stdout != "" || o.code != 2 {
			t.Errorf("a refused call must print nothing on stdout: exit %d stdout %q", o.code, o.stdout)
		}
	})
	if cfg.SharedSubject != "" {
		t.Run("plan_apply_undo", func(t *testing.T) { planApplyUndo(t, cfg) })
	}
}

func planApplyUndo(t *testing.T, cfg Config) {
	subj := cfg.SharedSubject
	p := run(t, cfg, nil, "plan", subj, "--json")
	if p.code != 0 || firstKey(t, "plan", p.stdout) != "schema" {
		t.Fatalf("plan exit %d: %s", p.code, p.stderr)
	}
	plan := one(t, "plan", p.stdout)
	if plan["schema"] != "zeo.plan/v1" || plan["empty"] != false {
		t.Fatalf("plan document wrong: %v", plan)
	}
	if id, _ := plan["plan_id"].(string); len(id) != 64 {
		t.Fatalf("plan_id %q is not a sha256", id)
	}
	if o := run(t, cfg, nil, "apply", subj); o.code != 2 {
		t.Errorf("apply of shared state without --plan: exit %d, want 2", o.code)
	}
	file := filepath.Join(t.TempDir(), "plan.json")
	if err := os.WriteFile(file, []byte(p.stdout), 0o600); err != nil {
		t.Fatal(err)
	}
	a := run(t, cfg, nil, "apply", subj, "--plan", file, "--json")
	if a.code != 0 {
		t.Fatalf("apply exit %d: %s", a.code, a.stderr)
	}
	applied := one(t, "apply", a.stdout)
	undo, _ := applied["undo"].(string)
	last := a.stderr[strings.LastIndex(strings.TrimRight(a.stderr, "\n"), "\n")+1:]
	if applied["schema"] != "zeo.apply/v1" || applied["outcome"] != "ok" || undo == "" || !ResultLine(strings.TrimSpace(last)) || !strings.Contains(last, "undo=") {
		t.Fatalf("apply document %v, last stderr line %q", applied, last)
	}
	if again := run(t, cfg, nil, "apply", subj, "--plan", file); again.code != 3 || !strings.Contains(again.stdout, "reason=plan_stale") {
		t.Errorf("the same plan applied twice: exit %d %q, want exit 3 plan_stale", again.code, again.stdout)
	}
	if e := run(t, cfg, nil, "plan", subj, "--json"); !strings.Contains(e.stdout, `"empty":true`) {
		t.Errorf("after apply the plan must be empty: %s", e.stdout)
	}
	fields := strings.Fields(undo)
	if len(fields) < 4 || fields[0] != cfg.Name || fields[1] != "apply" {
		t.Fatalf("undo %q is not a runnable `%s apply ...` command", undo, cfg.Name)
	}
	if u := run(t, cfg, nil, fields[1:]...); u.code != 0 {
		t.Fatalf("undo exit %d: %s", u.code, u.stdout+u.stderr)
	}
	if r := run(t, cfg, nil, "plan", subj, "--json"); !strings.Contains(r.stdout, `"empty":false`) {
		t.Errorf("after the undo the original plan applies again: %s", r.stdout)
	}
}
