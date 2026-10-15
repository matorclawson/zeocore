package kit_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"

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
	conformance.Run(t, conformance.Config{Binary: bin, Name: "sample", FailingDoctorEnv: "ZEO_KIT_SAMPLE_FAIL_DOCTOR=1"})
}

func TestResultLineGrammar(t *testing.T) {
	good := []string{"RESULT a update ok from=1 to=2", `RESULT a apply ok undo="x y"`, "RESULT zeo survey ok repos=4 open=9"}
	bad := []string{"RESULT a update done", "result a update ok", "RESULT a update ok extra", "RESULT a update ok reason=x y"}
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
