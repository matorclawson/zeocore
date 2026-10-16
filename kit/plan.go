package kit

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
)

// Schemas of the plan and apply documents (rev d section 6).
const (
	SchemaPlan  = "zeo.plan/v1"
	SchemaApply = "zeo.apply/v1"
)

// maxPlanBytes bounds a plan file read by apply --plan.
const maxPlanBytes = 1 << 20

// Change is one target a plan would change: its value now and after the apply.
type Change struct {
	Target string `json:"target"`
	Before string `json:"before"`
	After  string `json:"after"`
}

// Subject is a thing an app can plan and apply a change to. The kit defines
// the mechanism (the plan document, the plan_id, the stale check, the backup
// and the undo); the app supplies the subjects.
type Subject struct {
	Name    string
	Summary string
	// Shared marks state outside the app (org rules, council config, seat
	// permissions): apply then runs only a reviewed plan, given by --plan.
	Shared bool
	// Plan reads the current state and returns what would change. It changes nothing.
	Plan func(context.Context) ([]Change, error)
	// Read returns the current value of each target, so apply can prove the
	// state is still the one a plan recorded.
	Read func(ctx context.Context, targets []string) (map[string]string, error)
	// Apply writes After for each change. It touches no other target.
	Apply func(context.Context, []Change) error
}

type planBody struct {
	App     string   `json:"app"`
	Subject string   `json:"subject"`
	Changes []Change `json:"changes"`
}

// planDoc is the zeo.plan/v1 document, and the file apply --plan reads.
type planDoc struct {
	Schema  string   `json:"schema"`
	App     string   `json:"app"`
	Subject string   `json:"subject"`
	PlanID  string   `json:"plan_id"`
	Changes []Change `json:"changes"`
	Empty   bool     `json:"empty"`
}

type applyDoc struct {
	Schema  string   `json:"schema"`
	App     string   `json:"app"`
	Subject string   `json:"subject"`
	PlanID  string   `json:"plan_id"`
	Outcome Outcome  `json:"outcome"`
	Reason  string   `json:"reason,omitempty"`
	Backup  string   `json:"backup,omitempty"`
	Undo    string   `json:"undo,omitempty"`
	Changes []Change `json:"changes"`
}

// newPlan builds the plan document. The plan_id is the sha256 of the body as
// canonical JSON, so every app, the kit and the council compute the same id.
func newPlan(app, subject string, changes []Change) (planDoc, error) {
	if changes == nil {
		changes = []Change{}
	}
	sort.SliceStable(changes, func(i, j int) bool { return changes[i].Target < changes[j].Target })
	body, err := json.Marshal(planBody{App: app, Subject: subject, Changes: changes})
	if err != nil {
		return planDoc{}, err
	}
	id, err := Digest(body)
	if err != nil {
		return planDoc{}, err
	}
	return planDoc{Schema: SchemaPlan, App: app, Subject: subject, PlanID: id, Changes: changes, Empty: len(changes) == 0}, nil
}

func (a *App) subject(name string) (Subject, error) {
	for _, s := range a.Subjects {
		if s.Name == name {
			return s, nil
		}
	}
	names := make([]string, 0, len(a.Subjects))
	for _, s := range a.Subjects {
		names = append(names, s.Name)
	}
	sort.Strings(names)
	return Subject{}, Usagef("unknown subject %q; this app has %v", name, names)
}

func (a *App) planCommands() []Command {
	if len(a.Subjects) == 0 {
		return nil
	}
	return []Command{
		{Name: "plan", Summary: "Show what apply would change to a subject; changes nothing.", Args: "<subject>", MaxArgs: 1, Output: SchemaPlan, Run: a.runPlan},
		{Name: "apply", Summary: "Apply a subject's change: back up first, print the undo. Shared state needs --plan FILE.", Args: "<subject>", MaxArgs: 1,
			Flags:  []Flag{{Name: "plan", Summary: "The reviewed plan file `plan --json` printed.", TakesValue: true}},
			Output: SchemaApply, Exits: []ExitCode{ExitFailed, ExitRefused}, Run: a.runApply},
	}
}

func (a *App) validateSubjects() error {
	if len(a.Subjects) == 0 {
		return nil
	}
	if a.StateDir == "" {
		return errors.New("kit: StateDir is required when the app has subjects (the backup is written there)")
	}
	seen := map[string]bool{}
	for _, s := range a.Subjects {
		switch {
		case !nameRe.MatchString(s.Name):
			return fmt.Errorf("kit: subject name %q must be lowercase letters, digits and dashes", s.Name)
		case seen[s.Name]:
			return fmt.Errorf("kit: subject %q is declared twice", s.Name)
		case s.Plan == nil || s.Read == nil || s.Apply == nil:
			return fmt.Errorf("kit: subject %q needs Plan, Read and Apply", s.Name)
		}
		seen[s.Name] = true
	}
	return nil
}

func subjectArg(c *Context) (string, error) {
	if len(c.Args) != 1 {
		return "", Usagef("%s needs a subject: `%s %s <subject>`", c.Verb, c.App.Name, c.Verb)
	}
	return c.Args[0], nil
}

func (a *App) runPlan(c *Context) error {
	name, err := subjectArg(c)
	if err != nil {
		return err
	}
	s, err := a.subject(name)
	if err != nil {
		return err
	}
	changes, err := s.Plan(c.Context)
	if err != nil {
		return Failed("plan %s: %v", name, err)
	}
	doc, err := newPlan(a.Name, name, changes)
	if err != nil {
		return Failed("plan %s: %v", name, err)
	}
	if c.JSON {
		return c.WriteJSON(doc)
	}
	if doc.Empty {
		fmt.Fprintf(c.Stdout, "%s: nothing to change (plan %s)\n", name, doc.PlanID[:12])
		return nil
	}
	fmt.Fprintf(c.Stdout, "%s: %d change(s), plan %s\n", name, len(doc.Changes), doc.PlanID[:12])
	for _, ch := range doc.Changes {
		fmt.Fprintf(c.Stdout, "  %s: %q -> %q\n", ch.Target, ch.Before, ch.After)
	}
	return nil
}

// readPlanFile reads a plan file and proves it is intact: the same schema,
// app and subject, and a plan_id that matches its own body.
func readPlanFile(a *App, path, subject string) (planDoc, error) {
	f, err := os.Open(path)
	if err != nil {
		return planDoc{}, NotFound("plan file %s: %v", path, err)
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, maxPlanBytes+1))
	if err != nil {
		return planDoc{}, Failed("plan file %s: %v", path, err)
	}
	if len(raw) > maxPlanBytes {
		return planDoc{}, Usagef("plan file %s is larger than %d bytes", path, maxPlanBytes)
	}
	var doc planDoc
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&doc); err != nil {
		return planDoc{}, Usagef("plan file %s is not a %s document: %v", path, SchemaPlan, err)
	}
	if _, err := Canonical(raw); err != nil {
		return planDoc{}, Usagef("plan file %s: %v", path, err)
	}
	switch {
	case doc.Schema != SchemaPlan:
		return planDoc{}, Usagef("plan file %s has schema %q, want %s", path, doc.Schema, SchemaPlan)
	case doc.App != a.Name || doc.Subject != subject:
		return planDoc{}, Usagef("plan file %s is for %s %s, not %s %s", path, doc.App, doc.Subject, a.Name, subject)
	}
	want, err := newPlan(doc.App, doc.Subject, append([]Change(nil), doc.Changes...))
	if err != nil {
		return planDoc{}, Usagef("plan file %s: %v", path, err)
	}
	if want.PlanID != doc.PlanID || want.Empty != doc.Empty {
		return planDoc{}, Usagef("plan file %s does not match its own plan_id; it was edited after `plan` printed it", path)
	}
	return doc, nil
}

func (a *App) runApply(c *Context) error {
	name, err := subjectArg(c)
	if err != nil {
		return err
	}
	s, err := a.subject(name)
	if err != nil {
		return err
	}
	var plan planDoc
	if path, ok := c.Flag("plan"); ok {
		if plan, err = readPlanFile(a, path, name); err != nil {
			return err
		}
	} else {
		if s.Shared {
			return Usagef("%s changes shared state, so apply runs only a reviewed plan: `%s plan %s --json > plan.json`, review it, then `%s apply %s --plan plan.json`", name, a.Name, name, a.Name, name)
		}
		changes, err := s.Plan(c.Context)
		if err != nil {
			return Failed("apply %s: %v", name, err)
		}
		if plan, err = newPlan(a.Name, name, changes); err != nil {
			return Failed("apply %s: %v", name, err)
		}
	}
	return a.applyPlan(c, s, plan)
}

func (a *App) applyPlan(c *Context, s Subject, plan planDoc) error {
	out := applyDoc{Schema: SchemaApply, App: a.Name, Subject: s.Name, PlanID: plan.PlanID, Changes: plan.Changes}
	finish := func(o Outcome, reason string) error {
		out.Outcome, out.Reason = o, reason
		if c.JSON {
			if err := c.WriteJSON(out); err != nil {
				return err
			}
		} else if o == OutcomeOK {
			fmt.Fprintf(c.Stdout, "%s: applied %d change(s), plan %s\n", s.Name, len(plan.Changes), plan.PlanID[:12])
			if out.Undo != "" {
				fmt.Fprintf(c.Stdout, "undo: %s\n", out.Undo)
			}
		}
		code, err := c.WriteResult(Result{Outcome: o, From: a.Version, To: a.Version, Reason: reason, Undo: out.Undo})
		if err != nil {
			return err
		}
		return ExitOnly(code)
	}
	// One operation at a time per app, and none while an update is unfinished.
	unlock, err := a.lockOp()
	if err != nil {
		fmt.Fprintf(c.Stderr, "%s: %v\n", a.Name, err)
		return finish(OutcomeRefused, reasonOf(err, "busy"))
	}
	defer unlock()
	if err := a.refuseIfRecovery(); err != nil {
		fmt.Fprintf(c.Stderr, "%s: %v\n", a.Name, err)
		return finish(OutcomeRefused, reasonOf(err, "recovery_required"))
	}
	targets := make([]string, 0, len(plan.Changes))
	for _, ch := range plan.Changes {
		targets = append(targets, ch.Target)
	}
	now, err := s.Read(c.Context, targets)
	if err != nil {
		fmt.Fprintf(c.Stderr, "%s: read %s: %v\n", a.Name, s.Name, err)
		return finish(OutcomeFailed, "read")
	}
	for _, ch := range plan.Changes {
		if v, ok := now[ch.Target]; !ok || v != ch.Before {
			fmt.Fprintf(c.Stderr, "%s: %s changed since the plan was made; run `%s plan %s` again\n", a.Name, ch.Target, a.Name, s.Name)
			return finish(OutcomeRefused, "plan_stale")
		}
	}
	if len(plan.Changes) == 0 {
		return finish(OutcomeOK, "")
	}
	// The undo is the backup written as a plan, checked the same way: it
	// refuses unless the state is still what this apply leaves.
	undoChanges := make([]Change, len(plan.Changes))
	for i, ch := range plan.Changes {
		undoChanges[i] = Change{Target: ch.Target, Before: ch.After, After: ch.Before}
	}
	undo, err := newPlan(a.Name, s.Name, undoChanges)
	if err != nil {
		return Failed("apply %s: %v", s.Name, err)
	}
	path, err := a.writeBackup(s.Name, plan.PlanID, undo)
	if err != nil {
		fmt.Fprintf(c.Stderr, "%s: backup: %v\n", a.Name, err)
		return finish(OutcomeFailed, "backup")
	}
	out.Backup = path
	out.Undo = fmt.Sprintf("%s apply %s --plan %s", a.Name, s.Name, path)
	if err := s.Apply(c.Context, plan.Changes); err != nil {
		fmt.Fprintf(c.Stderr, "%s: apply %s: %v\n", a.Name, s.Name, err)
		out.Undo = ""
		return finish(OutcomeFailed, "apply")
	}
	return finish(OutcomeOK, "")
}

// writeBackup stores the undo plan under StateDir, owner-only, and returns its path.
func (a *App) writeBackup(subject, planID string, undo planDoc) (string, error) {
	dir := filepath.Join(a.StateDir, "backups", subject)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	raw, err := json.MarshalIndent(undo, "", "  ")
	if err != nil {
		return "", err
	}
	path := filepath.Join(dir, planID+".undo.json")
	tmp, err := os.CreateTemp(dir, ".undo-*")
	if err != nil {
		return "", err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(append(raw, '\n')); err != nil {
		tmp.Close()
		return "", err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return "", err
	}
	if err := tmp.Close(); err != nil {
		return "", err
	}
	if err := os.Chmod(tmp.Name(), 0o600); err != nil {
		return "", err
	}
	return path, os.Rename(tmp.Name(), path)
}
