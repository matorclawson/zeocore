package kit

import (
	"fmt"
	"sort"
	"strings"
)

// Catalogue is the document help --json prints, built from the command table
// so help can never list a command that does not exist.
func (a *App) catalogue() helpDoc {
	doc := helpDoc{Schema: SchemaHelp, App: a.Name, Version: a.Version, Contract: Contract, Exits: ExitTable(), Schemas: a.schemas()}
	add := func(c Command, kit bool) {
		hc := helpCommand{Name: c.Name, Summary: c.Summary, Args: c.Args, Output: c.Output, Kit: kit, Flags: []helpFlag{{Name: "json", Summary: "Print one JSON document.", TakesValue: false}}}
		for _, f := range c.Flags {
			hc.Flags = append(hc.Flags, helpFlag{Name: f.Name, Summary: f.Summary, TakesValue: f.TakesValue})
		}
		codes := map[int]bool{int(ExitOK): true, int(ExitUsage): true}
		for _, e := range c.Exits {
			codes[int(e)] = true
		}
		for code := range codes {
			hc.Exits = append(hc.Exits, code)
		}
		sort.Ints(hc.Exits)
		doc.Commands = append(doc.Commands, hc)
	}
	for _, c := range a.kitVerbs() {
		add(c, true)
	}
	for _, c := range a.Commands {
		add(c, false)
	}
	return doc
}

func (a *App) runHelp(c *Context) error {
	doc := a.catalogue()
	if c.JSON {
		return c.WriteJSON(doc)
	}
	width := 0
	for _, cmd := range doc.Commands {
		if n := len(cmd.Name); n > width {
			width = n
		}
	}
	fmt.Fprintf(c.Stdout, "%s %s (%s)\n", a.Name, a.Version, Contract)
	for _, cmd := range doc.Commands {
		fmt.Fprintf(c.Stdout, "  %-*s  %s\n", width, cmd.Name, cmd.Summary)
	}
	fmt.Fprintf(c.Stdout, "Every command takes --json. `%s help --json` lists flags, exit codes and schemas.\n", a.Name)
	return nil
}

func (a *App) runVersion(c *Context) error {
	state, signer := SignatureUnsigned, ""
	if a.Signature != nil {
		state, signer = a.Signature()
	}
	switch state {
	case SignatureVerified, SignatureUnsigned, SignatureInvalid, SignatureUnknownSigner:
	default:
		return Failed("signature state %q is not one of verified, unsigned, invalid, unknown_signer", state)
	}
	doc := versionDoc{Schema: SchemaVersion, App: a.Name, Version: a.Version, Commit: a.Commit, BuiltAt: a.BuiltAt, Signature: state, Signer: signer}
	if c.JSON {
		return c.WriteJSON(doc)
	}
	fmt.Fprintf(c.Stdout, "%s %s commit=%s signature=%s\n", a.Name, a.Version, orDash(a.Commit), state)
	return nil
}

func (a *App) runDoctor(c *Context) error {
	checks := a.Doctor(c.Context)
	if checks == nil {
		checks = []Check{}
	}
	ready := true
	for _, ck := range checks {
		if !ck.OK && !ck.Skipped {
			ready = false
			if ck.Next == "" {
				return Failed("doctor check %q failed without naming its next step", ck.Name)
			}
		}
	}
	doc := doctorDoc{Schema: SchemaDoctor, App: a.Name, Ready: ready, Checks: checks}
	if c.JSON {
		if err := c.WriteJSON(doc); err != nil {
			return err
		}
	} else {
		for _, ck := range checks {
			mark := "ok  "
			switch {
			case ck.Skipped:
				mark = "skip"
			case !ck.OK:
				mark = "FAIL"
			}
			fmt.Fprintf(c.Stdout, "%s %s: %s\n", mark, ck.Name, ck.Detail)
			if !ck.OK && !ck.Skipped {
				fmt.Fprintf(c.Stdout, "     next: %s\n", ck.Next)
			}
		}
		fmt.Fprintf(c.Stdout, "ready=%t\n", ready)
	}
	if !ready {
		return ExitOnly(ExitNotReady)
	}
	return nil
}

func (a *App) runStatus(c *Context) error {
	st, err := a.Status(c.Context)
	if err != nil {
		return Failed("status: %v", err)
	}
	if !validStates[st.State] {
		return Failed("status state %q is not one of running, stopped, degraded, unknown", st.State)
	}
	if st.Counts == nil {
		st.Counts = map[string]int{}
	}
	doc := statusDoc{Schema: SchemaStatus, App: a.Name, Version: a.Version, State: st.State, Healthy: st.Healthy, Live: st.Live, Detail: st.Detail, Counts: st.Counts}
	if c.JSON {
		if err := c.WriteJSON(doc); err != nil {
			return err
		}
	} else {
		live := "n/a"
		if st.Live != nil {
			live = fmt.Sprint(*st.Live)
		}
		fmt.Fprintf(c.Stdout, "%s %s state=%s healthy=%t live=%s", a.Name, a.Version, st.State, st.Healthy, live)
		if len(st.Counts) > 0 {
			keys := make([]string, 0, len(st.Counts))
			for k := range st.Counts {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			for _, k := range keys {
				fmt.Fprintf(c.Stdout, " %s=%d", k, st.Counts[k])
			}
		}
		fmt.Fprintln(c.Stdout)
		if st.Detail != "" {
			fmt.Fprintln(c.Stdout, strings.TrimSpace(st.Detail))
		}
	}
	if !st.Healthy {
		return ExitOnly(ExitFailed)
	}
	return nil
}

func orDash(s string) string {
	if s == "" {
		return "-"
	}
	return s
}
