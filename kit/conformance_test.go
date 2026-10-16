package kit_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
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
	conformance.Run(t, conformance.Config{
		Binary: bin, Name: "sample", FailingDoctorEnv: "ZEO_KIT_SAMPLE_FAIL_DOCTOR=1",
		Env: []string{"ZEO_KIT_SAMPLE_DIR=" + t.TempDir()}, SharedSubject: "motd",
	})
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

// TestACrashBetweenSwitchAndVerifyLeavesRecoveryRequired kills the process
// inside Verify, after the switch: the next run must report recovery_required,
// not ok, and recover is the only way out.
func TestACrashBetweenSwitchAndVerifyLeavesRecoveryRequired(t *testing.T) {
	bin := filepath.Join(t.TempDir(), "zeo-kit-sample")
	build := exec.Command("go", "build", "-o", bin, "./cmd/zeo-kit-sample")
	build.Env = append(os.Environ(), "GOPROXY=off", "GOFLAGS=-mod=readonly")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}
	dir := t.TempDir()
	for _, v := range []string{"1.0.0", "2.0.0"} {
		_ = os.MkdirAll(filepath.Join(dir, "releases", v), 0o755)
		_ = os.WriteFile(filepath.Join(dir, "releases", v, "VERSION"), []byte(v), 0o644)
	}
	sample := func(env string, args ...string) (int, string) {
		cmd := exec.Command(bin, args...)
		cmd.Env = []string{"ZEO_KIT_SAMPLE_DIR=" + dir}
		if env != "" {
			cmd.Env = append(cmd.Env, env)
		}
		out, err := cmd.CombinedOutput()
		code := 0
		if ee, ok := err.(*exec.ExitError); ok {
			code = ee.ExitCode()
		}
		return code, string(out)
	}
	if code, out := sample("", "update", "--to", "1.0.0"); code != 0 {
		t.Fatalf("first install: %d %s", code, out)
	}
	if code, _ := sample("ZEO_KIT_SAMPLE_DIE_IN_VERIFY=1", "update", "--to", "2.0.0"); code != 9 {
		t.Fatalf("the process should have died inside Verify, exit %d", code)
	}
	code, out := sample("", "update", "--to", "2.0.0")
	if code != 3 || !strings.Contains(out, "reason=recovery_required") || strings.Contains(out, " ok ") {
		t.Fatalf("after a crash: exit %d %s", code, out)
	}
	if code, out := sample("", "recover"); code != 0 || !strings.Contains(out, "RESULT sample recover ok") {
		t.Fatalf("recover: %d %s", code, out)
	}
	if code, out := sample("", "update", "--to", "2.0.0"); code != 0 {
		t.Fatalf("after recover: %d %s", code, out)
	}
}
