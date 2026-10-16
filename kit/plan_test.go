package kit_test

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/zeroemployeeorg/zeocore/kit"
	"github.com/zeroemployeeorg/zeocore/kit/internal/sampleapp"
)

// fakeState is the subject under test: a map the plan reads and the apply writes.
type fakeState struct {
	vals      map[string]string
	want      map[string]string
	applyErr  error
	applied   int
	readErr   error
	readCalls int
}

func (f *fakeState) subject(name string, shared bool) kit.Subject {
	return kit.Subject{
		Name: name, Summary: "fake", Shared: shared,
		Plan: func(context.Context) ([]kit.Change, error) {
			// Deliberately in descending order: the kit, not the subject, sorts.
			var cs []kit.Change
			for _, k := range []string{"c", "b", "a"} {
				if v, ok := f.want[k]; ok && f.vals[k] != v {
					cs = append(cs, kit.Change{Target: k, Before: f.vals[k], After: v})
				}
			}
			return cs, nil
		},
		Read: func(_ context.Context, ts []string) (map[string]string, error) {
			f.readCalls++
			if f.readErr != nil {
				return nil, f.readErr
			}
			m := map[string]string{}
			for _, t := range ts {
				m[t] = f.vals[t]
			}
			return m, nil
		},
		Apply: func(_ context.Context, cs []kit.Change) error {
			f.applied++
			if f.applyErr != nil {
				return f.applyErr
			}
			for _, c := range cs {
				f.vals[c.Target] = c.After
			}
			return nil
		},
	}
}

func planApp(t *testing.T, f *fakeState, shared bool) *kit.App {
	app := sampleapp.New(false)
	app.StateDir = t.TempDir()
	app.Subjects = []kit.Subject{f.subject("rules", shared)}
	return app
}

func newFake() *fakeState {
	return &fakeState{vals: map[string]string{"a": "1", "b": "2"}, want: map[string]string{"a": "9", "b": "2", "c": "new"}}
}

func writePlan(t *testing.T, app *kit.App) string {
	t.Helper()
	code, out, _ := run(t, app, "plan", "rules", "--json")
	if code != 0 {
		t.Fatalf("plan exit %d", code)
	}
	p := filepath.Join(t.TempDir(), "plan.json")
	if err := os.WriteFile(p, []byte(out), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

var resultRe = regexp.MustCompile(`(?m)^RESULT .*$`)

func TestPlanChangesNothingAndHasAStableID(t *testing.T) {
	f := newFake()
	app := planApp(t, f, true)
	code, out, _ := run(t, app, "plan", "rules", "--json")
	if code != 0 || f.applied != 0 || f.vals["a"] != "1" {
		t.Fatalf("plan exit %d applied %d vals %v", code, f.applied, f.vals)
	}
	var doc struct {
		Schema, App, Subject string
		PlanID               string `json:"plan_id"`
		Changes              []kit.Change
		Empty                bool
	}
	if err := json.Unmarshal([]byte(out), &doc); err != nil {
		t.Fatal(err)
	}
	if doc.Schema != kit.SchemaPlan || doc.Empty || len(doc.Changes) != 2 || doc.Changes[0].Target != "a" || doc.Changes[1].Target != "c" {
		t.Fatalf("plan doc = %+v", doc)
	}
	if !strings.HasPrefix(out, `{"schema":"zeo.plan/v1"`) {
		t.Errorf("schema is not first: %s", out)
	}
	body := `{"app":"sample","subject":"rules","changes":[{"target":"a","before":"1","after":"9"},{"target":"c","before":"","after":"new"}]}`
	want, err := kit.Digest([]byte(body))
	if err != nil || doc.PlanID != want {
		t.Errorf("plan_id = %s, want the canonical digest %s (%v)", doc.PlanID, want, err)
	}
	_, out2, _ := run(t, app, "plan", "rules", "--json")
	if out2 != out {
		t.Error("the same state must give the same plan, byte for byte")
	}
}

func TestEmptyPlan(t *testing.T) {
	f := newFake()
	f.want = map[string]string{"a": "1"}
	app := planApp(t, f, true)
	code, out, _ := run(t, app, "plan", "rules", "--json")
	if code != 0 || !strings.Contains(out, `"empty":true`) || !strings.Contains(out, `"changes":[]`) {
		t.Fatalf("exit %d out %s", code, out)
	}
	code, out, _ = run(t, app, "plan", "rules")
	if code != 0 || !strings.Contains(out, "nothing to change") {
		t.Errorf("text plan: %d %q", code, out)
	}
}

func TestSharedApplyRefusesWithoutAPlan(t *testing.T) {
	f := newFake()
	app := planApp(t, f, true)
	code, out, errs := run(t, app, "apply", "rules")
	if code != 2 || out != "" || f.applied != 0 || !strings.Contains(errs, "--plan") {
		t.Fatalf("exit %d stdout %q stderr %q applied %d", code, out, errs, f.applied)
	}
}

func TestApplyRunsAReviewedPlanBacksUpAndPrintsAnUndoThatRoundTrips(t *testing.T) {
	f := newFake()
	app := planApp(t, f, true)
	plan := writePlan(t, app)
	code, out, errs := run(t, app, "apply", "rules", "--plan", plan, "--json")
	if code != 0 {
		t.Fatalf("apply exit %d: %s", code, errs)
	}
	if f.vals["a"] != "9" || f.vals["c"] != "new" || f.vals["b"] != "2" {
		t.Fatalf("state after apply = %v", f.vals)
	}
	var doc struct {
		Schema, Outcome, Backup, Undo string
	}
	if err := json.Unmarshal([]byte(out), &doc); err != nil || doc.Schema != kit.SchemaApply || doc.Outcome != "ok" {
		t.Fatalf("apply doc %s (%v)", out, err)
	}
	info, err := os.Stat(doc.Backup)
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("backup %s: %v %v", doc.Backup, info, err)
	}
	res := resultRe.FindString(errs)
	if !strings.Contains(res, ` ok `) || !strings.Contains(res, `undo="`+doc.Undo+`"`) || !strings.HasSuffix(doc.Undo, doc.Backup) {
		t.Fatalf("RESULT line %q, undo %q", res, doc.Undo)
	}
	// The undo is the backup as a plan, run with the same command.
	code, _, errs = run(t, app, "apply", "rules", "--plan", doc.Backup)
	if code != 0 || f.vals["a"] != "1" || f.vals["c"] != "" || f.vals["b"] != "2" {
		t.Fatalf("undo exit %d state %v: %s", code, f.vals, errs)
	}
}

func TestUndoRefusesWhenTheStateMovedOn(t *testing.T) {
	f := newFake()
	app := planApp(t, f, true)
	_, out, _ := run(t, app, "apply", "rules", "--plan", writePlan(t, app), "--json")
	var doc struct{ Backup string }
	_ = json.Unmarshal([]byte(out), &doc)
	f.vals["a"] = "edited-by-someone-else"
	code, out, _ := run(t, app, "apply", "rules", "--plan", doc.Backup)
	if code != 3 || !strings.Contains(out, "reason=plan_stale") || f.vals["a"] != "edited-by-someone-else" {
		t.Fatalf("exit %d state %v stdout %q", code, f.vals, out)
	}
}

func TestStalePlanIsRefusedAndChangesNothing(t *testing.T) {
	f := newFake()
	app := planApp(t, f, true)
	plan := writePlan(t, app)
	f.vals["b"] = "3"
	f.vals["a"] = "7" // a planned target moved
	code, out, errs := run(t, app, "apply", "rules", "--plan", plan, "--json")
	if code != 3 || f.applied != 0 || f.vals["a"] != "7" {
		t.Fatalf("exit %d applied %d state %v", code, f.applied, f.vals)
	}
	if !strings.Contains(out, `"outcome":"refused"`) || !strings.Contains(out, `"reason":"plan_stale"`) || !strings.Contains(errs, "reason=plan_stale") {
		t.Errorf("stdout %s stderr %s", out, errs)
	}
	if _, err := os.Stat(filepath.Join(app.StateDir, "backups")); err == nil {
		t.Error("a refused apply must not write a backup")
	}
}

func TestAnEditedPlanFileIsRefusedAsUsage(t *testing.T) {
	f := newFake()
	app := planApp(t, f, true)
	plan := writePlan(t, app)
	raw, _ := os.ReadFile(plan)
	edited := strings.Replace(string(raw), `"after":"9"`, `"after":"666"`, 1)
	if edited == string(raw) {
		t.Fatal("test edit did not change the plan")
	}
	_ = os.WriteFile(plan, []byte(edited), 0o600)
	code, _, errs := run(t, app, "apply", "rules", "--plan", plan)
	if code != 2 || f.applied != 0 || !strings.Contains(errs, "plan_id") {
		t.Fatalf("exit %d applied %d stderr %q", code, f.applied, errs)
	}
}

func TestPlanFileChecks(t *testing.T) {
	f := newFake()
	app := planApp(t, f, true)
	good, _ := os.ReadFile(writePlan(t, app))
	dir := t.TempDir()
	put := func(s string) string {
		p := filepath.Join(dir, "p.json")
		_ = os.WriteFile(p, []byte(s), 0o600)
		return p
	}
	cases := map[string]string{
		"wrong schema":   strings.Replace(string(good), "zeo.plan/v1", "zeo.plan/v2", 1),
		"wrong app":      signed(t, "other", "rules"),
		"wrong subject":  signed(t, "sample", "other"),
		"unknown field":  strings.Replace(string(good), `"empty":false`, `"empty":false,"extra":1`, 1),
		"duplicate key":  strings.Replace(string(good), `"empty":false`, `"empty":false,"empty":false`, 1),
		"not json":       "not json",
		"empty file":     "",
		"trailing bytes": string(good) + "{}",
	}
	for name, body := range cases {
		code, _, _ := run(t, app, "apply", "rules", "--plan", put(body))
		if code != 2 {
			t.Errorf("%s: exit %d, want 2", name, code)
		}
	}
	if code, _, _ := run(t, app, "apply", "rules", "--plan", filepath.Join(dir, "missing.json")); code != 4 {
		t.Errorf("missing plan file: exit %d, want 4", code)
	}
	big := put(strings.Repeat(" ", 1<<20+10))
	if code, _, errs := run(t, app, "apply", "rules", "--plan", big); code != 2 || !strings.Contains(errs, "larger than") {
		t.Errorf("oversized plan: exit %d %q", code, errs)
	}
	if f.applied != 0 {
		t.Error("no refused plan may reach Apply")
	}
}

func TestUnsharedApplyPlansAndAppliesItself(t *testing.T) {
	f := newFake()
	app := planApp(t, f, false)
	code, _, _ := run(t, app, "apply", "rules")
	if code != 0 || f.vals["a"] != "9" {
		t.Fatalf("exit %d state %v", code, f.vals)
	}
}

func TestApplyFailureIsFailedAndPrintsNoUndo(t *testing.T) {
	f := newFake()
	f.applyErr = errors.New("disk full")
	app := planApp(t, f, true)
	code, out, errs := run(t, app, "apply", "rules", "--plan", writePlan(t, app), "--json")
	if code != 1 || !strings.Contains(out, `"outcome":"failed"`) || strings.Contains(out, `"undo"`) || strings.Contains(errs, "undo=") || !strings.Contains(errs, "reason=apply") {
		t.Fatalf("exit %d stdout %s stderr %s", code, out, errs)
	}
}

func TestReadFailureIsFailedBeforeAnyChange(t *testing.T) {
	f := newFake()
	app := planApp(t, f, true)
	plan := writePlan(t, app)
	f.readErr = errors.New("unreadable")
	code, out, _ := run(t, app, "apply", "rules", "--plan", plan)
	if code != 1 || f.applied != 0 || !strings.Contains(out, "reason=read") {
		t.Fatalf("exit %d applied %d stdout %s", code, f.applied, out)
	}
}

func TestEmptyPlanAppliesAsOKWithoutBackup(t *testing.T) {
	f := newFake()
	f.want = map[string]string{"a": "1"}
	app := planApp(t, f, true)
	code, out, _ := run(t, app, "apply", "rules", "--plan", writePlan(t, app))
	if code != 0 || f.applied != 0 || strings.Contains(out, "undo") {
		t.Fatalf("exit %d applied %d: %s", code, f.applied, out)
	}
}

func TestSubjectArgumentAndUnknownSubject(t *testing.T) {
	app := planApp(t, newFake(), true)
	for _, args := range [][]string{{"plan"}, {"apply"}, {"plan", "nope"}, {"apply", "nope", "--plan", "x"}} {
		if code, _, _ := run(t, app, args...); code != 2 {
			t.Errorf("%v: exit %d, want 2", args, code)
		}
	}
}

func TestAppsWithoutSubjectsHaveNoPlanOrApply(t *testing.T) {
	app := sampleapp.New(false)
	for _, v := range []string{"plan", "apply"} {
		if code, _, _ := run(t, app, v, "x"); code != 2 {
			t.Errorf("%s with no subjects: exit %d, want 2", v, code)
		}
	}
	_, out, _ := run(t, app, "help", "--json")
	if strings.Contains(out, "zeo.plan/v1") {
		t.Error("help lists a plan schema the app cannot print")
	}
}

func TestHelpListsPlanAndApplyWhenThereAreSubjects(t *testing.T) {
	app := planApp(t, newFake(), true)
	_, out, _ := run(t, app, "help", "--json")
	for _, want := range []string{`"name":"plan"`, `"name":"apply"`, `"zeo.plan/v1"`, `"zeo.apply/v1"`, `"name":"plan","summary"`} {
		if !strings.Contains(out, want) {
			t.Errorf("help --json lacks %s", want)
		}
	}
}

func TestValidateRefusesBadSubjects(t *testing.T) {
	ok := func() *kit.App { return planApp(t, newFake(), true) }
	cases := map[string]func(*kit.App){
		"no state dir": func(a *kit.App) { a.StateDir = "" },
		"bad name":     func(a *kit.App) { a.Subjects[0].Name = "Bad Name" },
		"duplicate":    func(a *kit.App) { a.Subjects = append(a.Subjects, a.Subjects[0]) },
		"no Plan":      func(a *kit.App) { a.Subjects[0].Plan = nil },
		"no Read":      func(a *kit.App) { a.Subjects[0].Read = nil },
		"no Apply":     func(a *kit.App) { a.Subjects[0].Apply = nil },
		"command shadow": func(a *kit.App) {
			a.Commands = append(a.Commands, kit.Command{Name: "plan", Run: func(*kit.Context) error { return nil }})
		},
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

// signed is a well-formed plan file, with a correct plan_id, for another app or subject.
func signed(t *testing.T, app, subject string) string {
	t.Helper()
	body := `{"app":"` + app + `","subject":"` + subject + `","changes":[{"target":"a","before":"1","after":"9"}]}`
	id, err := kit.Digest([]byte(body))
	if err != nil {
		t.Fatal(err)
	}
	return `{"schema":"zeo.plan/v1","app":"` + app + `","subject":"` + subject + `","plan_id":"` + id + `","changes":[{"target":"a","before":"1","after":"9"}],"empty":false}`
}
