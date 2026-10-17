// Package sampleapp is the smallest app that uses the kit. It is what the
// conformance suite runs against inside zeocore's own gate; a real app
// replaces it with its own commands and hooks.
package sampleapp

import (
	"context"
	"errors"
	"os"
	"path/filepath"

	"github.com/zeroemployeeorg/zeocore/kit"
)

// New returns the sample app. failDoctor makes one check fail, so the exit-7
// path can be driven. It has no plan or apply subject.
func New(failDoctor bool) *kit.App { return build(failDoctor, "") }

// NewWithState is New plus one shared subject, "motd", whose state is the file
// stateDir/motd.txt, so plan, apply and the undo can be driven for real.
func NewWithState(failDoctor bool, stateDir string) *kit.App { return build(failDoctor, stateDir) }

// MOTD is the text the "motd" subject plans to set.
const MOTD = "hello from the kit"

func motd(stateDir string) kit.Subject {
	file := filepath.Join(stateDir, "motd.txt")
	read := func() (string, error) {
		b, err := os.ReadFile(file)
		if errors.Is(err, os.ErrNotExist) {
			return "", nil
		}
		return string(b), err
	}
	return kit.Subject{
		Name: "motd", Summary: "The message of the day.", Shared: true,
		Plan: func(context.Context) ([]kit.Change, error) {
			cur, err := read()
			if err != nil || cur == MOTD {
				return nil, err
			}
			return []kit.Change{{Target: "motd", Before: cur, After: MOTD}}, nil
		},
		Read: func(_ context.Context, targets []string) (map[string]string, error) {
			cur, err := read()
			m := map[string]string{}
			for _, t := range targets {
				m[t] = cur
			}
			return m, err
		},
		Apply: func(_ context.Context, cs []kit.Change) error {
			return os.WriteFile(file, []byte(cs[len(cs)-1].After), 0o600)
		},
	}
}

func build(failDoctor bool, stateDir string) *kit.App {
	live := false
	app := &kit.App{
		Name:    "sample",
		Version: "0.0.1",
		Commit:  "0000000",
		Commands: []kit.Command{{
			Name: "greet", Summary: "Print a greeting.", Args: "[name]", MaxArgs: 1,
			Flags: []kit.Flag{{Name: "loud", Summary: "Shout."}, {Name: "to", Summary: "Who to greet.", TakesValue: true}},
			Run: func(c *kit.Context) error {
				who := "world"
				if v, ok := c.Flag("to"); ok {
					who = v
				}
				if len(c.Args) == 1 {
					who = c.Args[0]
				}
				if _, loud := c.Flag("loud"); loud {
					who += "!"
				}
				if c.JSON {
					return c.WriteJSON(map[string]string{"schema": "sample.greet/v1", "greeting": "hello " + who})
				}
				_, err := c.Stdout.Write([]byte("hello " + who + "\n"))
				return err
			},
		}},
		Schemas: []string{"sample.greet/v1"},
		Doctor: func(context.Context) []kit.Check {
			cs := []kit.Check{
				{Name: "runtime", OK: true, Detail: "ready"},
				{Name: "optional", Skipped: true, Detail: "not configured"},
			}
			if failDoctor {
				cs = append(cs, kit.Check{Name: "store", Detail: "missing", Next: "sample setup"})
			}
			return cs
		},
		Status: func(context.Context) (kit.Status, error) {
			return kit.Status{State: "running", Healthy: true, Live: &live, Counts: map[string]int{"ready_rows": 3}}, nil
		},
	}
	if stateDir != "" {
		app.StateDir = filepath.Join(stateDir, "state")
		app.Subjects = []kit.Subject{motd(stateDir)}
		app.Updater = updater(stateDir)
	}
	return app
}

// updater is the sample app's update: releases are staged as directories
// stateDir/releases/<version>/ holding a VERSION file, and installing copies
// one beside the others under stateDir/install. ZEO_KIT_SAMPLE_DIE_IN_VERIFY=1
// makes Verify kill the process, to prove a crash leaves recovery_required.
func updater(stateDir string) *kit.Updater {
	return &kit.Updater{
		Root: filepath.Join(stateDir, "install"),
		Fetch: func(_ context.Context, to string) (kit.Release, error) {
			if to == "" {
				return kit.Release{}, errors.New("name a version with --to: the sample app has no release feed")
			}
			dir := filepath.Join(stateDir, "releases", to)
			if _, err := os.Stat(dir); err != nil {
				return kit.Release{}, err
			}
			return kit.Release{Version: to, Dir: dir}, nil
		},
		Verify: func(_ context.Context, dir string) error {
			if os.Getenv("ZEO_KIT_SAMPLE_DIE_IN_VERIFY") == "1" {
				os.Exit(9)
			}
			_, err := os.ReadFile(filepath.Join(dir, "VERSION"))
			return err
		},
	}
}
