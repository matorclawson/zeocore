package kit_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// TestAConsumerBuildsOfflineWithTheKitVendored is the council's q1 condition:
// an app pins the kit by commit and vendors it, so its gate runs with
// GOPROXY=off and -mod=readonly and takes no module from the network. The
// consumer here is a temp module that replaces the kit with this checkout,
// vendors it and builds from vendor/ alone.
func TestAConsumerBuildsOfflineWithTheKitVendored(t *testing.T) {
	kitDir, err := filepath.Abs(".")
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	write := func(name, body string) {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write("go.mod", "module example.test/consumer\n\ngo 1.27\n\ntoolchain go1.27.0\n\nrequire github.com/zeroemployeeorg/zeocore/kit v0.0.0\n\nreplace github.com/zeroemployeeorg/zeocore/kit => "+kitDir+"\n")
	write("main.go", `package main

import (
	"context"
	"os"

	"github.com/zeroemployeeorg/zeocore/kit"
)

func main() {
	app := &kit.App{
		Name: "consumer", Version: "1.0.0",
		Doctor: func(context.Context) []kit.Check { return nil },
		Status: func(context.Context) (kit.Status, error) { return kit.Status{State: "running", Healthy: true}, nil },
	}
	os.Exit(app.Run(context.Background(), os.Args[1:], os.Stdout, os.Stderr))
}
`)
	env := append(os.Environ(), "GOPROXY=off", "GOFLAGS=-mod=readonly", "GOTOOLCHAIN=local", "GOWORK=off")
	step := func(args ...string) []byte {
		cmd := exec.Command("go", args...)
		cmd.Dir, cmd.Env = dir, env
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("go %v: %v\n%s", args, err, out)
		}
		return out
	}
	step("mod", "vendor")
	if _, err := os.Stat(filepath.Join(dir, "vendor", "github.com", "zeroemployeeorg", "zeocore", "kit", "app.go")); err != nil {
		t.Fatalf("the kit was not vendored: %v", err)
	}
	// Build from vendor/ only: -mod=vendor never reads the replace target, and
	// GOPROXY=off refuses the network, so a pass proves neither is needed.
	cmd := exec.Command("go", "build", "-mod=vendor", "-o", filepath.Join(dir, "consumer"), ".")
	cmd.Dir, cmd.Env = dir, env
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("offline vendored build: %v\n%s", err, out)
	}
	run := exec.Command(filepath.Join(dir, "consumer"), "version", "--json")
	out, err := run.Output()
	if err != nil || len(out) == 0 {
		t.Fatalf("the vendored consumer did not run: %v %q", err, out)
	}
}
