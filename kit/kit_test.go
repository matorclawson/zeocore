package kit_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"testing"

	"github.com/zeroemployeeorg/zeocore/kit"
	"github.com/zeroemployeeorg/zeocore/kit/internal/sampleapp"
)

func run(t *testing.T, app *kit.App, args ...string) (int, string, string) {
	t.Helper()
	var out, errb bytes.Buffer
	code := app.Run(context.Background(), args, &out, &errb)
	return code, out.String(), errb.String()
}

func TestExitTableIsTheContractTable(t *testing.T) {
	want := []struct {
		code kit.ExitCode
		name string
	}{{0, "ok"}, {1, "failed"}, {2, "usage"}, {3, "refused"}, {4, "not_found"}, {5, "timeout"}, {6, "unavailable"}, {7, "not_ready"}}
	got := kit.ExitTable()
	if len(got) != len(want) {
		t.Fatalf("table has %d rows, want %d", len(got), len(want))
	}
	for i, w := range want {
		if got[i].Code != w.code || got[i].Name != w.name || got[i].Meaning == "" {
			t.Errorf("row %d = %+v, want %d %s", i, got[i], w.code, w.name)
		}
	}
}

func TestErrorsMapToTheirCodes(t *testing.T) {
	cases := []struct {
		name string
		err  error
		code int
	}{
		{"failed", kit.Failed("x"), 1}, {"usage", kit.Usagef("x"), 2}, {"refused", kit.Refused("busy", "x"), 3},
		{"not found", kit.NotFound("x"), 4}, {"timeout", kit.Timeout("x"), 5}, {"unavailable", kit.Unavailable("x"), 6},
		{"plain error is a failure", errors.New("boom"), 1}, {"exit only", kit.ExitOnly(kit.ExitNotReady), 7},
	}
	for _, c := range cases {
		app := sampleapp.New(false)
		app.Commands = append(app.Commands, kit.Command{Name: "boom", Run: func(*kit.Context) error { return c.err }})
		code, out, _ := run(t, app, "boom")
		if code != c.code || out != "" {
			t.Errorf("%s: exit %d stdout %q, want exit %d and nothing on stdout", c.name, code, out, c.code)
		}
	}
	if kit.ExitOnly(kit.ExitOK) != nil {
		t.Error("ExitOnly(ok) must be nil")
	}
}

func TestRefusalKeepsItsReason(t *testing.T) {
	var e *kit.Error
	if !errors.As(kit.Refused("plan_stale", "the plan is stale"), &e) || e.Reason != "plan_stale" || e.Code != kit.ExitRefused {
		t.Fatalf("got %+v", e)
	}
}

func TestSingleDashFlagsAreRefusedWithTheDoubleDashForm(t *testing.T) {
	app := sampleapp.New(false)
	for _, args := range [][]string{{"-json"}, {"help", "-json"}, {"greet", "-loud"}, {"greet", "-to=x"}, {"version", "-x"}} {
		code, out, errb := run(t, app, args...)
		if code != 2 || out != "" {
			t.Errorf("%v: exit %d stdout %q", args, code, out)
		}
		name := strings.TrimLeft(strings.SplitN(args[len(args)-1], "=", 2)[0], "-")
		if !strings.Contains(errb, "--"+name) {
			t.Errorf("%v: stderr %q does not name --%s", args, errb, name)
		}
	}
}

func TestFlagParsing(t *testing.T) {
	app := sampleapp.New(false)
	cases := []struct {
		args []string
		out  string
		code int
	}{
		{[]string{"greet"}, "hello world\n", 0},
		{[]string{"greet", "ada"}, "hello ada\n", 0},
		{[]string{"greet", "--to", "bob"}, "hello bob\n", 0},
		{[]string{"greet", "--to=bob", "--loud"}, "hello bob!\n", 0},
		{[]string{"greet", "--", "--odd"}, "hello --odd\n", 0},
		{[]string{"greet", "a", "b"}, "", 2},
		{[]string{"greet", "--to"}, "", 2},
		{[]string{"greet", "--loud=1"}, "", 2},
		{[]string{"greet", "--nope"}, "", 2},
		{[]string{"--json", "help"}, "", 2},
		{[]string{"nope"}, "", 2},
		{[]string{}, "", 2},
	}
	for _, c := range cases {
		code, out, _ := run(t, app, c.args...)
		if code != c.code || out != c.out {
			t.Errorf("%v: exit %d stdout %q, want exit %d stdout %q", c.args, code, out, c.code, c.out)
		}
	}
	if code, out, _ := run(t, app, "--help"); code != 0 || !strings.Contains(out, "greet") {
		t.Errorf("--help should be help: exit %d out %q", code, out)
	}
}

func TestJSONIsOneDocumentWithSchemaFirst(t *testing.T) {
	app := sampleapp.New(false)
	for _, verb := range []string{"help", "version", "doctor", "status"} {
		code, out, errb := run(t, app, verb, "--json")
		if code != 0 || errb != "" {
			t.Fatalf("%s: exit %d stderr %q", verb, code, errb)
		}
		dec := json.NewDecoder(strings.NewReader(out))
		tok, _ := dec.Token()
		key, _ := dec.Token()
		if tok != json.Delim('{') || key != "schema" {
			t.Errorf("%s: first field is %v", verb, key)
		}
		var doc map[string]any
		if err := json.Unmarshal([]byte(out), &doc); err != nil {
			t.Errorf("%s: %v", verb, err)
		}
	}
}

// help --json is built from the command table, so it can list nothing that
// does not exist and omit nothing that does.
func TestCatalogueEqualsTheCommandTable(t *testing.T) {
	app := sampleapp.New(false)
	_, out, _ := run(t, app, "help", "--json")
	var doc struct {
		Commands []struct {
			Name  string `json:"name"`
			Kit   bool   `json:"kit"`
			Args  string `json:"args"`
			Exits []int  `json:"exits"`
			Flags []struct {
				Name       string `json:"name"`
				TakesValue bool   `json:"takes_value"`
			} `json:"flags"`
		} `json:"commands"`
		Schemas []string `json:"schemas"`
	}
	if err := json.Unmarshal([]byte(out), &doc); err != nil {
		t.Fatal(err)
	}
	want := []string{"help", "version", "doctor", "status", "greet"}
	if len(doc.Commands) != len(want) {
		t.Fatalf("catalogue lists %d commands, want %d", len(doc.Commands), len(want))
	}
	for i, name := range want {
		c := doc.Commands[i]
		if c.Name != name || c.Kit != (i < 4) {
			t.Errorf("command %d = %s kit=%v", i, c.Name, c.Kit)
		}
		if !slices.Contains(c.Exits, 0) || !slices.Contains(c.Exits, 2) || !slices.IsSorted(c.Exits) {
			t.Errorf("%s exits %v must be sorted and include 0 and 2", c.Name, c.Exits)
		}
		if code, _, _ := run(t, app, name, "--json", "--no-such-flag"); code != 2 {
			t.Errorf("listed command %s did not dispatch", name)
		}
	}
	greet := doc.Commands[4]
	if greet.Args != "[name]" || len(greet.Flags) != 3 || greet.Flags[0].Name != "json" || !greet.Flags[2].TakesValue {
		t.Errorf("greet flags not carried: %+v", greet)
	}
	if !strings.Contains(strings.Join(doc.Schemas, " "), "sample.greet/v1") {
		t.Errorf("app schema missing from %v", doc.Schemas)
	}
}

func TestDoctorExitsSevenWhenNotReadyAndStillPrintsTheDocument(t *testing.T) {
	code, out, _ := run(t, sampleapp.New(true), "doctor", "--json")
	if code != 7 || !strings.Contains(out, `"ready":false`) {
		t.Fatalf("exit %d out %q", code, out)
	}
	code, out, _ = run(t, sampleapp.New(true), "doctor")
	if code != 7 || !strings.Contains(out, "next: sample setup") || !strings.Contains(out, "FAIL") || !strings.Contains(out, "skip") {
		t.Fatalf("text doctor: exit %d out %q", code, out)
	}
	if code, out, _ = run(t, sampleapp.New(false), "doctor", "--json"); code != 0 || !strings.Contains(out, `"skipped":true`) || !strings.Contains(out, `"ready":true`) {
		t.Fatalf("a skipped check is not a failure: exit %d out %q", code, out)
	}
}

func TestADoctorFailureMustNameItsNextStep(t *testing.T) {
	app := sampleapp.New(false)
	app.Doctor = func(context.Context) []kit.Check { return []kit.Check{{Name: "x", Detail: "bad"}} }
	if code, _, errb := run(t, app, "doctor"); code != 1 || !strings.Contains(errb, "next step") {
		t.Fatalf("exit %d stderr %q", code, errb)
	}
}

func TestStatusExitsOneWhenUnhealthy(t *testing.T) {
	app := sampleapp.New(false)
	app.Status = func(context.Context) (kit.Status, error) { return kit.Status{State: "degraded", Detail: "x"}, nil }
	if code, out, _ := run(t, app, "status", "--json"); code != 1 || !strings.Contains(out, `"live":null`) || !strings.Contains(out, `"counts":{}`) {
		t.Fatalf("exit %d out %q", code, out)
	}
	app.Status = func(context.Context) (kit.Status, error) { return kit.Status{State: "bogus", Healthy: true}, nil }
	if code, _, _ := run(t, app, "status"); code != 1 {
		t.Fatalf("an unknown state must fail, exit %d", code)
	}
	app.Status = func(context.Context) (kit.Status, error) { return kit.Status{}, errors.New("db down") }
	if code, _, errb := run(t, app, "status"); code != 1 || !strings.Contains(errb, "db down") {
		t.Fatalf("exit %d stderr %q", code, errb)
	}
}

func TestStatusCountsAndLive(t *testing.T) {
	code, out, _ := run(t, sampleapp.New(false), "status", "--json")
	if code != 0 || !strings.Contains(out, `"counts":{"ready_rows":3}`) || !strings.Contains(out, `"live":false`) {
		t.Fatalf("exit %d out %q", code, out)
	}
}

func TestVersionSignatureDefaultsToUnsignedAndIsChecked(t *testing.T) {
	app := sampleapp.New(false)
	if _, out, _ := run(t, app, "version", "--json"); !strings.Contains(out, `"signature":"unsigned"`) {
		t.Fatalf("out %q", out)
	}
	app.Signature = func() (string, string) { return "verified", "council" }
	if _, out, _ := run(t, app, "version", "--json"); !strings.Contains(out, `"signer":"council"`) {
		t.Fatalf("out %q", out)
	}
	app.Signature = func() (string, string) { return "trusted", "" }
	if code, _, _ := run(t, app, "version"); code != 1 {
		t.Fatalf("a state outside the enum must fail, exit %d", code)
	}
}

func TestValidateRefusesABadApp(t *testing.T) {
	good := func() *kit.App { return sampleapp.New(false) }
	cases := map[string]func(*kit.App){
		"bad name":   func(a *kit.App) { a.Name = "Bad Name" },
		"no version": func(a *kit.App) { a.Version = "" },
		"no doctor":  func(a *kit.App) { a.Doctor = nil },
		"no status":  func(a *kit.App) { a.Status = nil },
		"shadows a kit verb": func(a *kit.App) {
			a.Commands = append(a.Commands, kit.Command{Name: "doctor", Run: func(*kit.Context) error { return nil }})
		},
		"duplicate command": func(a *kit.App) { a.Commands = append(a.Commands, a.Commands[0]) },
		"no run":            func(a *kit.App) { a.Commands = append(a.Commands, kit.Command{Name: "x"}) },
		"flag named json":   func(a *kit.App) { a.Commands[0].Flags = append(a.Commands[0].Flags, kit.Flag{Name: "json"}) },
		"duplicate flag":    func(a *kit.App) { a.Commands[0].Flags = append(a.Commands[0].Flags, kit.Flag{Name: "loud"}) },
	}
	for name, mutate := range cases {
		a := good()
		mutate(a)
		if err := a.Validate(); err == nil {
			t.Errorf("%s: Validate accepted it", name)
		}
		if code, _, _ := run(t, a, "help"); code != 1 {
			t.Errorf("%s: Run must refuse to start, exit %d", name, code)
		}
	}
	if err := good().Validate(); err != nil {
		t.Fatal(err)
	}
}

// A Context has no stdin, so no verb can prompt; every verb runs to the end
// with no input at all.
func TestEveryVerbRunsWithNoInput(t *testing.T) {
	app := sampleapp.New(false)
	for _, verb := range []string{"help", "version", "doctor", "status", "greet"} {
		if code, _, _ := run(t, app, verb); code != 0 {
			t.Errorf("%s exit %d", verb, code)
		}
	}
}

func TestResultLine(t *testing.T) {
	ok := []struct {
		r    kit.Result
		want string
	}{
		{kit.Result{App: "council", Verb: "update", Outcome: kit.OutcomeOK, From: "1.0.0", To: "1.1.0"}, "RESULT council update ok from=1.0.0 to=1.1.0"},
		{kit.Result{App: "council", Verb: "update", Outcome: kit.OutcomeRefused, From: "1.0.0", To: "1.1.0", Reason: "studio_live"}, "RESULT council update refused from=1.0.0 to=1.1.0 reason=studio_live"},
		{kit.Result{App: "a", Verb: "apply", Outcome: kit.OutcomeOK, From: "x", To: "y", Undo: `a org rules apply --plan "p q"`}, `RESULT a apply ok from=x to=y undo="a org rules apply --plan \"p q\""`},
		{kit.Result{App: "zeo", Verb: "survey", Outcome: kit.OutcomeOK, Totals: []kit.Pair{{"repos", "4"}, {"open", "9"}}}, "RESULT zeo survey ok repos=4 open=9"},
		{kit.Result{App: "a", Verb: "update", Outcome: kit.OutcomeRecoveryRequired, Reason: "verify"}, "RESULT a update recovery_required reason=verify"},
	}
	for _, c := range ok {
		got, err := c.r.Line()
		if err != nil || got != c.want {
			t.Errorf("got %q, %v; want %q", got, err, c.want)
		}
	}
	bad := map[string]kit.Result{
		"unknown outcome":     {App: "a", Verb: "v", Outcome: "done", Reason: "x"},
		"refused no reason":   {App: "a", Verb: "v", Outcome: kit.OutcomeRefused},
		"ok with reason":      {App: "a", Verb: "v", Outcome: kit.OutcomeOK, Reason: "x"},
		"reason with space":   {App: "a", Verb: "v", Outcome: kit.OutcomeFailed, Reason: "two words"},
		"reason upper":        {App: "a", Verb: "v", Outcome: kit.OutcomeFailed, Reason: "Busy"},
		"app with space":      {App: "a b", Verb: "v", Outcome: kit.OutcomeOK},
		"from with space":     {App: "a", Verb: "v", Outcome: kit.OutcomeOK, From: "1 0"},
		"undo with newline":   {App: "a", Verb: "v", Outcome: kit.OutcomeOK, Undo: "x\ny"},
		"total with equals":   {App: "a", Verb: "v", Outcome: kit.OutcomeOK, Totals: []kit.Pair{{"k", "a=b"}}},
		"total key uppercase": {App: "a", Verb: "v", Outcome: kit.OutcomeOK, Totals: []kit.Pair{{"K", "1"}}},
	}
	for name, r := range bad {
		if line, err := r.Line(); err == nil {
			t.Errorf("%s: accepted as %q", name, line)
		}
	}
}

func TestOutcomeExitAndRetry(t *testing.T) {
	want := map[kit.Outcome]struct {
		code  kit.ExitCode
		retry bool
	}{
		kit.OutcomeOK: {0, true}, kit.OutcomeRefused: {3, true}, kit.OutcomeFailed: {1, true},
		kit.OutcomeUndone: {1, true}, kit.OutcomeRecoveryRequired: {1, false},
	}
	for o, w := range want {
		if o.Exit() != w.code || o.SafeToRetry() != w.retry {
			t.Errorf("%s: exit %d retry %v, want %d %v", o, o.Exit(), o.SafeToRetry(), w.code, w.retry)
		}
	}
}

func TestWriteResultGoesToStdoutInTextAndLastStderrLineInJSON(t *testing.T) {
	app := sampleapp.New(false)
	app.Commands = append(app.Commands, kit.Command{Name: "act", Run: func(c *kit.Context) error {
		code, err := c.WriteResult(kit.Result{Outcome: kit.OutcomeRefused, Reason: "busy", From: "1", To: "2"})
		if err != nil {
			return err
		}
		return kit.ExitOnly(code)
	}})
	code, out, errb := run(t, app, "act")
	if code != 3 || out != "RESULT sample act refused from=1 to=2 reason=busy\n" || errb != "" {
		t.Errorf("text: exit %d out %q err %q", code, out, errb)
	}
	code, out, errb = run(t, app, "act", "--json")
	if code != 3 || out != "" || errb != "RESULT sample act refused from=1 to=2 reason=busy\n" {
		t.Errorf("json: exit %d out %q err %q", code, out, errb)
	}
}

// An error message never echoes an input value, so no argument can start a
// line of its own and forge the last-line RESULT (zeocouncil K1, PR 134).
func TestErrorsNeverEchoInputOrForgeAResultLine(t *testing.T) {
	app := sampleapp.New(false)
	forge := "RESULT sample status ok"
	for _, args := range [][]string{
		{"status", "-x\n" + forge},
		{"status", "--x\n" + forge},
		{"status", "-x\x1b[2J"},
		{"status", "--bad\x1b[31m"},
		{"-x\n" + forge},
		{"--x\n" + forge},
		{"nope\n" + forge},
		{"nope\x1b[2J"},
	} {
		code, out, errb := run(t, app, args...)
		if code != 2 || out != "" {
			t.Errorf("%q: exit %d stdout %q", args, code, out)
		}
		if strings.ContainsAny(errb[:len(errb)-1], "\n\r\x1b") || strings.Contains(errb, forge) || !strings.HasSuffix(errb, "\n") {
			t.Errorf("%q: stderr %q echoes input or is not one line", args, errb)
		}
	}
	// A plain name is still named, so the message stays useful.
	if _, _, errb := run(t, app, "status", "-json"); !strings.Contains(errb, "use --json") {
		t.Errorf("a plain single-dash flag is named: %q", errb)
	}
	if _, _, errb := run(t, app, "-h"); !strings.Contains(errb, "use --help") {
		t.Errorf("-h should point at --help: %q", errb)
	}
	// An app's own message is folded to one line too.
	app.Commands = append(app.Commands, kit.Command{Name: "boom", Run: func(*kit.Context) error {
		return kit.Failed("bad\nRESULT sample boom ok\x1b[0m")
	}})
	if _, _, errb := run(t, app, "boom"); strings.Count(errb, "\n") != 1 || strings.Contains(errb, "\x1b") {
		t.Errorf("an app message must be one printable line: %q", errb)
	}
}

// Words in a RESULT line are printable ASCII: Python's split() and
// splitlines() break on the others (zeocouncil K2).
func TestResultWordsRefuseControlAndUnicodeSpace(t *testing.T) {
	for _, bad := range []string{"v\vRESULT", "a\x00b", "a\x1bb", "a\u0085b", "a b", "a b", "a b", "a\tb", "café"} {
		for name, r := range map[string]kit.Result{
			"app":   {App: bad, Verb: "v", Outcome: kit.OutcomeOK},
			"verb":  {App: "a", Verb: bad, Outcome: kit.OutcomeOK},
			"from":  {App: "a", Verb: "v", Outcome: kit.OutcomeOK, From: bad},
			"to":    {App: "a", Verb: "v", Outcome: kit.OutcomeOK, To: bad},
			"total": {App: "a", Verb: "v", Outcome: kit.OutcomeOK, Totals: []kit.Pair{{"k", bad}}},
		} {
			if line, err := r.Line(); err == nil {
				t.Errorf("%q as %s accepted: %q", bad, name, line)
			}
		}
	}
}

// A total cannot take a key the line already uses (zeocouncil K3).
func TestResultTotalsRefuseReservedAndRepeatedKeys(t *testing.T) {
	for _, k := range []string{"reason", "undo", "from", "to"} {
		r := kit.Result{App: "a", Verb: "v", Outcome: kit.OutcomeOK, From: "1", To: "2", Totals: []kit.Pair{{k, "x"}}}
		if line, err := r.Line(); err == nil {
			t.Errorf("total key %q accepted: %q", k, line)
		}
	}
	r := kit.Result{App: "a", Verb: "v", Outcome: kit.OutcomeOK, Totals: []kit.Pair{{"n", "1"}, {"n", "2"}}}
	if line, err := r.Line(); err == nil {
		t.Errorf("a repeated total key accepted: %q", line)
	}
}

// help lists the exits a command can really end with, and the table stays
// closed (zeocouncil K4).
func TestHelpListsTheExitsAKitVerbCanReach(t *testing.T) {
	app := sampleapp.New(false)
	_, out, _ := run(t, app, "help", "--json")
	var doc struct {
		Commands []struct {
			Name  string `json:"name"`
			Exits []int  `json:"exits"`
		} `json:"commands"`
	}
	if err := json.Unmarshal([]byte(out), &doc); err != nil {
		t.Fatal(err)
	}
	want := map[string][]int{"help": {0, 2}, "version": {0, 1, 2}, "doctor": {0, 1, 2, 7}, "status": {0, 1, 2}}
	for _, c := range doc.Commands {
		if w, ok := want[c.Name]; ok && !slices.Equal(c.Exits, w) {
			t.Errorf("%s lists exits %v, want %v", c.Name, c.Exits, w)
		}
	}
}

func TestACommandCannotEndWithACodeOutsideTheTable(t *testing.T) {
	for name, err := range map[string]error{
		"code 42":            &kit.Error{Code: 42, Message: "x"},
		"code 42 no message": kit.ExitOnly(42),
		"code 0 and message": &kit.Error{Code: 0, Message: "x"},
		"negative":           &kit.Error{Code: -1},
	} {
		app := sampleapp.New(false)
		app.Commands = append(app.Commands, kit.Command{Name: "odd", Run: func(*kit.Context) error { return err }})
		if code, _, errb := run(t, app, "odd"); code != 1 || !strings.Contains(errb, "exit table") {
			t.Errorf("%s: exit %d stderr %q, want exit 1 naming the table", name, code, errb)
		}
	}
	app := sampleapp.New(false)
	app.Commands = append(app.Commands, kit.Command{Name: "odd", Exits: []kit.ExitCode{42}, Run: func(*kit.Context) error { return nil }})
	if code, _, errb := run(t, app, "help"); code != 1 || !strings.Contains(errb, "42") {
		t.Errorf("a command listing exit 42 must fail Validate: exit %d stderr %q", code, errb)
	}
}

func TestCommandNamesAreValidated(t *testing.T) {
	for _, name := range []string{"Bad", "has space", "1x", ""} {
		app := sampleapp.New(false)
		app.Commands = append(app.Commands, kit.Command{Name: name, Run: func(*kit.Context) error { return nil }})
		if code, _, _ := run(t, app, "help"); code != 1 {
			t.Errorf("command name %q accepted: exit %d", name, code)
		}
	}
}

func TestDoctorWithNoChecksPrintsAnEmptyListNotNull(t *testing.T) {
	app := sampleapp.New(false)
	app.Doctor = func(context.Context) []kit.Check { return nil }
	if code, out, _ := run(t, app, "doctor", "--json"); code != 0 || !strings.Contains(out, `"checks":[]`) {
		t.Fatalf("exit %d out %q", code, out)
	}
}

func TestAnUnhealthyStatusNamesItOnStderr(t *testing.T) {
	app := sampleapp.New(false)
	app.Status = func(context.Context) (kit.Status, error) { return kit.Status{State: "degraded"}, nil }
	for _, args := range [][]string{{"status"}, {"status", "--json"}} {
		if code, _, errb := run(t, app, args...); code != 1 || !strings.Contains(errb, "sample: status: not healthy (state=degraded)") {
			t.Errorf("%v: exit %d stderr %q", args, code, errb)
		}
	}
}
