// Package sampleapp is the smallest app that uses the kit. It is what the
// conformance suite runs against inside zeocore's own gate; a real app
// replaces it with its own commands and hooks.
package sampleapp

import (
	"context"

	"github.com/zeroemployeeorg/zeocore/kit"
)

// New returns the sample app. failDoctor makes one check fail, so the exit-7
// path can be driven.
func New(failDoctor bool) *kit.App {
	live := false
	return &kit.App{
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
}
