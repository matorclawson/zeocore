package kit_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"github.com/zeroemployeeorg/zeocore/kit"
	"github.com/zeroemployeeorg/zeocore/kit/conformance"
)

// TestSampleAppConforms builds the sample app as a real binary and runs the
// conformance suite against it: the suite zeocouncil and every other app adopts.
func TestSampleAppConforms(t *testing.T) {
	bin := filepath.Join(t.TempDir(), "zeo-kit-sample")
	build := exec.Command("go", "build", "-o", bin, "./cmd/zeo-kit-sample")
	build.Env = append(os.Environ(), "GOPROXY=off", "GOFLAGS=-mod=readonly")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}
	conformance.Run(t, conformance.Config{
		Binary: bin, Name: "sample", FailingDoctorEnv: "ZEO_KIT_SAMPLE_FAIL_DOCTOR=1",
		Env: []string{"ZEO_KIT_SAMPLE_DIR=" + t.TempDir()}, SharedSubject: "motd",
	})
}

func TestResultLineGrammar(t *testing.T) {
	good := []string{"RESULT a update ok from=1 to=2", `RESULT a apply ok undo="x y"`, "RESULT zeo survey ok repos=4 open=9"}
	bad := []string{"RESULT a update done", "result a update ok", "RESULT a update ok extra", "RESULT a update ok reason=x y",
		"RESULT a\vRESULT update ok", "RESULT a up\u00a0date ok", "RESULT a update ok from=a\u2028b", "RESULT a update ok from=a\x1bb",
		"RESULT a apply ok undo=\"x\ny\"", "RESULT a apply ok undo=\"x\x1by\"", "RESULT a apply ok undo=\"caf\u00e9\"", "RESULT a apply ok undo=\"a\u2028b\""}
	for _, l := range good {
		if !conformance.ResultLine(l) {
			t.Errorf("rejected %q", l)
		}
	}
	for _, l := range bad {
		if conformance.ResultLine(l) {
			t.Errorf("accepted %q", l)
		}
	}
}

// Line and the checker agree for every undo text: Line escapes all non-ASCII,
// and the checker accepts only ASCII (zeocouncil P3, PR 134).
func TestEveryLineTheKitWritesPassesTheChecker(t *testing.T) {
	for _, undo := range []string{"a b", `say "hi"`, "café     \u0085 \t \\ x", "emoji \U0001F600"} {
		line, err := kit.Result{App: "a", Verb: "apply", Outcome: kit.OutcomeOK, Undo: undo}.Line()
		if err != nil {
			continue // a newline in undo is refused by Line itself
		}
		if !conformance.ResultLine(line) {
			t.Errorf("Line wrote %q, which the checker rejects", line)
		}
		for _, r := range line {
			if r > 0x7e || r < 0x20 {
				t.Errorf("Line wrote a non-ASCII or control rune %U in %q", r, line)
			}
		}
	}
}
