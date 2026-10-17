package kit

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// Flag is a double-dash flag a command accepts.
type Flag struct {
	Name       string // without the dashes
	Summary    string
	TakesValue bool
}

// Command is an app's own verb. Kit verbs are registered by the kit.
type Command struct {
	Name    string
	Summary string
	// Args is the positional usage, for example "<subject>"; empty for none.
	Args string
	// MaxArgs is how many positional arguments the command takes.
	MaxArgs int
	Flags   []Flag
	// Output is the schema its --json document carries, if it prints one.
	Output string
	// Exits are the codes the command can end with, besides 0 and 2.
	Exits []ExitCode
	Run   func(*Context) error
}

// Context is what a command's Run receives. The kit never reads stdin, so no
// verb can wait on a prompt.
type Context struct {
	Context context.Context
	App     *App
	Verb    string
	Args    []string
	JSON    bool
	Stdout  io.Writer
	Stderr  io.Writer
	flags   map[string]string
}

// Flag returns the value of a flag the command declared, and whether it was given.
func (c *Context) Flag(name string) (string, bool) {
	v, ok := c.flags[name]
	return v, ok
}

// WriteJSON prints one document on stdout and nothing else.
func (c *Context) WriteJSON(doc any) error {
	enc := json.NewEncoder(c.Stdout)
	enc.SetEscapeHTML(false)
	return enc.Encode(doc)
}

// WriteResult prints the RESULT line: on stdout in text mode, and as the last
// stderr line with --json, so stdout stays one document. It returns the exit
// code the outcome ends the process with.
func (c *Context) WriteResult(r Result) (ExitCode, error) {
	if r.App == "" {
		r.App = c.App.Name
	}
	if r.Verb == "" {
		r.Verb = c.Verb
	}
	line, err := r.Line()
	if err != nil {
		return ExitFailed, err
	}
	w := c.Stdout
	if c.JSON {
		w = c.Stderr
	}
	_, err = fmt.Fprintln(w, line)
	return r.Outcome.Exit(), err
}

// App is one zeo or dt app. Name, Version and the two hooks are required.
type App struct {
	Name    string
	Version string
	Commit  string
	BuiltAt string
	// Signature reports the install's signature state. Until the kit verifies
	// releases itself (a later slice) it reports what the app supplies, and an
	// app that supplies nothing reports "unsigned": nothing verified it.
	Signature func() (state, signer string)
	Commands  []Command
	// Doctor runs every prerequisite, in order.
	Doctor func(context.Context) []Check
	// Status reports what runs and whether it is healthy.
	Status func(context.Context) (Status, error)
	// Schemas names the documents the app's own commands print.
	Schemas []string
	// Subjects are the things plan and apply work on. With none, the app has
	// no plan or apply verb.
	Subjects []Subject
	// StateDir is where apply writes its backup and undo plan; required with Subjects.
	StateDir string
}

var nameRe = regexp.MustCompile(`^[a-z][a-z0-9-]*$`)

// kitVerbs are the verbs every app has. An app never changes them.
func (a *App) kitVerbs() []Command {
	return append([]Command{
		{Name: "help", Summary: "List the commands.", Output: SchemaHelp, Exits: nil, Run: a.runHelp},
		{Name: "version", Summary: "Version, source commit and signature state.", Output: SchemaVersion, Exits: []ExitCode{ExitFailed}, Run: a.runVersion},
		{Name: "doctor", Summary: "Check every prerequisite, in order; each failure names its next step.", Output: SchemaDoctor, Exits: []ExitCode{ExitFailed, ExitNotReady}, Run: a.runDoctor},
		{Name: "status", Summary: "What runs, at which version, healthy or not, live or not.", Output: SchemaStatus, Exits: []ExitCode{ExitFailed}, Run: a.runStatus},
	}, a.planCommands()...)
}

// Validate refuses an app that cannot be registered: a missing hook, a bad or
// duplicate name, or an app command that shadows a kit verb.
func (a *App) Validate() error {
	switch {
	case !nameRe.MatchString(a.Name):
		return fmt.Errorf("kit: app name %q must be lowercase letters, digits and dashes", a.Name)
	case a.Version == "":
		return errors.New("kit: app version is required")
	case !wordRe.MatchString(a.Version):
		return errors.New("kit: the app version must be printable ASCII without a quote, space or equals sign, because the RESULT line carries it")
	case a.Doctor == nil:
		return errors.New("kit: the Doctor hook is required")
	case a.Status == nil:
		return errors.New("kit: the Status hook is required")
	}
	if err := a.validateSubjects(); err != nil {
		return err
	}
	seen := map[string]bool{}
	for _, k := range a.kitVerbs() {
		seen[k.Name] = true
	}
	for _, c := range a.Commands {
		switch {
		case !nameRe.MatchString(c.Name):
			return fmt.Errorf("kit: command name %q must be lowercase letters, digits and dashes", c.Name)
		case seen[c.Name]:
			return fmt.Errorf("kit: command %q is a kit verb or declared twice; an app adds verbs beside the kit's, never changes them", c.Name)
		case c.Run == nil:
			return fmt.Errorf("kit: command %q has no Run", c.Name)
		}
		for _, e := range c.Exits {
			if !e.inTable() || e == ExitOK {
				return fmt.Errorf("kit: command %q lists exit code %d, which is not a failure code in the exit table", c.Name, e)
			}
		}
		seen[c.Name] = true
		fs := map[string]bool{"json": true}
		for _, f := range c.Flags {
			if !nameRe.MatchString(f.Name) || fs[f.Name] {
				return fmt.Errorf("kit: command %q flag %q must be a new lowercase name", c.Name, f.Name)
			}
			fs[f.Name] = true
		}
	}
	return nil
}

func (a *App) lookup(name string) (Command, bool) {
	for _, c := range a.kitVerbs() {
		if c.Name == name {
			return c, true
		}
	}
	for _, c := range a.Commands {
		if c.Name == name {
			return c, true
		}
	}
	return Command{}, false
}

// Run runs one command line (without the binary name) and returns the exit
// code. Errors go to stderr and never echo an input value.
func (a *App) Run(ctx context.Context, args []string, stdout, stderr io.Writer) int {
	if err := a.Validate(); err != nil {
		fmt.Fprintln(stderr, err)
		return int(ExitFailed)
	}
	err := a.dispatch(ctx, args, stdout, stderr)
	if err == nil {
		return int(ExitOK)
	}
	var e *Error
	if errors.As(err, &e) && !e.Code.inTable() || e != nil && e.Code == ExitOK {
		// The table stays closed, and a failure is never exit 0.
		fmt.Fprintf(stderr, "%s: a command ended with exit code %d, which is not a failure code in the exit table\n", a.Name, e.Code)
		return int(ExitFailed)
	}
	if e != nil && e.Message == "" {
		return int(e.Code)
	}
	fmt.Fprintf(stderr, "%s: %s\n", a.Name, oneLine(err.Error()))
	return int(codeOf(err))
}

// oneLine makes a message safe to print as one line: every rune that is not
// printable, a newline or an escape among them, becomes "?", so no message can
// start a line of its own, least of all a RESULT line.
func oneLine(s string) string {
	return strings.Map(func(r rune) rune {
		if strconv.IsPrint(r) {
			return r
		}
		return '?'
	}, s)
}

// flagNameRe is what a flag or command name looks like. Only a string that
// matches is ever echoed in an error: anything else is named by its position.
var flagNameRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`)

// shown returns s when it is a plain name, and says it is not shown otherwise.
func shown(s string) string {
	if flagNameRe.MatchString(s) {
		return s
	}
	return "(not a plain name; not shown)"
}

func (a *App) dispatch(ctx context.Context, args []string, stdout, stderr io.Writer) error {
	if len(args) == 0 {
		return Usagef("no command given; run `%s help`", a.Name)
	}
	if args[0] == "--help" {
		args = append([]string{"help"}, args[1:]...)
	}
	verb := args[0]
	if strings.HasPrefix(verb, "-") && verb != "-" {
		if !strings.HasPrefix(verb, "--") {
			return singleDash(verb)
		}
		return Usagef("flags go after the command: `%s <command> --flag`", a.Name)
	}
	cmd, ok := a.lookup(verb)
	if !ok {
		return Usagef("unknown command %s; run `%s help`", shown(verb), a.Name)
	}
	known := map[string]Flag{"json": {Name: "json", Summary: "Print one JSON document.", TakesValue: false}}
	for _, f := range cmd.Flags {
		known[f.Name] = f
	}
	c := &Context{Context: ctx, App: a, Verb: verb, Stdout: stdout, Stderr: stderr, flags: map[string]string{}}
	rest := args[1:]
	for i := 0; i < len(rest); i++ {
		t := rest[i]
		switch {
		case t == "--":
			c.Args = append(c.Args, rest[i+1:]...)
			i = len(rest)
		case strings.HasPrefix(t, "--"):
			name, val, hasVal := strings.Cut(t[2:], "=")
			f, ok := known[name]
			if !ok {
				return Usagef("unknown flag --%s for %s; run `%s help --json`", shown(name), verb, a.Name)
			}
			if f.TakesValue && !hasVal {
				if i+1 >= len(rest) {
					return Usagef("flag --%s needs a value", name)
				}
				i++
				val = rest[i]
			} else if !f.TakesValue && hasVal {
				return Usagef("flag --%s takes no value", name)
			}
			if name == "json" {
				c.JSON = true
				continue
			}
			c.flags[name] = val
		case strings.HasPrefix(t, "-") && t != "-":
			return singleDash(t)
		default:
			c.Args = append(c.Args, t)
		}
	}
	if len(c.Args) > cmd.MaxArgs {
		return Usagef("%s takes %d argument(s), got %d", verb, cmd.MaxArgs, len(c.Args))
	}
	return cmd.Run(c)
}

// singleDash refuses a single-dash flag and names the double-dash form.
func singleDash(t string) error {
	name, _, _ := strings.Cut(strings.TrimLeft(t, "-"), "=")
	switch {
	case name == "h":
		return Usagef("single-dash flag -h is not accepted; use --help")
	case flagNameRe.MatchString(name):
		return Usagef("single-dash flag -%s is not accepted; use --%s", name, name)
	}
	return Usagef("a single-dash flag is not accepted; every flag is --name (run `help --json`)")
}

func (a *App) schemas() []string {
	all := map[string]bool{SchemaHelp: true, SchemaVersion: true, SchemaDoctor: true, SchemaStatus: true}
	if len(a.Subjects) > 0 {
		all[SchemaPlan], all[SchemaApply] = true, true
	}
	for _, s := range a.Schemas {
		all[s] = true
	}
	out := make([]string, 0, len(all))
	for s := range all {
		out = append(out, s)
	}
	sort.Strings(out)
	return out
}
