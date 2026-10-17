// Command zeo-kit-sample is the sample app as a binary, so the conformance
// suite can drive a real process: exit codes, stdout and stderr, no stdin.
package main

import (
	"context"
	"os"

	"github.com/zeroemployeeorg/zeocore/kit/internal/sampleapp"
)

func main() {
	os.Exit(sampleapp.NewWithState(os.Getenv("ZEO_KIT_SAMPLE_FAIL_DOCTOR") == "1", os.Getenv("ZEO_KIT_SAMPLE_DIR")).Run(context.Background(), os.Args[1:], os.Stdout, os.Stderr))
}
