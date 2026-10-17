# kit: the zeo-cli/v1 command kit

A Go module (`github.com/zeroemployeeorg/zeocore/kit`) that gives every zeo and dt app the same
commands, grammar, exit codes and result line. The contract is zeoarchitect SOW-05 rev d with the
Architect's section 14 ruling; the build plan is ZEOCORE-SOW-34. **Slices 1 and 2 of 7:** the grammar,
the exit table, the `RESULT` line, the `help`, `version`, `doctor` and `status` verbs, and `plan` and
`apply`, plus the conformance suite. `update`, `rollback` and `recover` come in later slices.

The kit uses the Go standard library only, so an app that links it takes no third-party module.

## Use it

```go
app := &kit.App{
    Name: "myapp", Version: "1.2.0",
    Doctor: func(ctx context.Context) []kit.Check { /* one Check per prerequisite, in order */ },
    Status: func(ctx context.Context) (kit.Status, error) { /* what runs, healthy or not */ },
    Commands: []kit.Command{ /* your own verbs, beside the kit's */ },
}
os.Exit(app.Run(ctx, os.Args[1:], os.Stdout, os.Stderr))
```

An app adds verbs beside the kit's and never changes them: a command named `help`, `version`,
`doctor` or `status` is refused at start. `internal/sampleapp` is the smallest working app.

## What every app gets

- **Grammar:** `<binary> <verb> [subject] [--flag value] [--json]`. Double-dash flags only: a
  single-dash flag is refused with exit 2 and a message naming the double-dash form (`-h` points at `--help`). `--json`
  prints one document on stdout and nothing else, `schema` first. Nothing prompts: the kit never
  reads stdin. `<binary> --help` is `help`.
- **Exit codes:** 0 ok, 1 failed, 2 usage, 3 refused, 4 not_found, 5 timeout, 6 unavailable,
  7 not_ready. `help --json` prints the table.
- **RESULT line:** `RESULT <app> <verb> <outcome> from=<v> to=<v> [reason=<token>] [undo="<cmd>"]`,
  built by `kit.Result.Line`, which refuses a line that breaks the grammar. Words are printable
  ASCII without `"` or `=`; `from=` and `to=` are left out when empty (a verb with no versions,
  or a refusal before the versions are known); a report verb's totals are further `key=value`
  pairs that may not reuse `from`, `to`, `reason` or `undo`.
- **Errors:** an error message never echoes an argument, a plan file's path or contents, or a subject
  that is not a plain name; a hook's error text is folded to one printable line. A flag or command name is named only when a flag or command name is named only when
  it is a plain name, and any other argument is not shown, so no argument can forge a RESULT line.
  A command can only end with a code of the table, and never with 0 and a message.
- **Documents:** `zeo.help/v1`, `zeo.version/v1`, `zeo.doctor/v1` (a check may be `skipped`),
  `zeo.status/v1` (with `live` and `counts`). `help --json` is built from the command table, so it
  cannot list a command that does not exist.

## Pin it, and vendor it

Apps pin the kit by Go pseudo-version of a gold zeocore commit, never by a tag (gold tags are
operator-only), and **vendor it** (`go mod vendor`, with `vendor/` committed), so an app's gate runs
with `GOPROXY=off` and `-mod=readonly`. `consumer_test.go` proves a vendored consumer builds that way.

## The gate

`make kit-verify` (Go 1.27 on PATH; the Python gate needs no Go): gofmt, `go vet`, `go test -race`,
and `scripts/mutate.sh scripts/mutations.txt`, which breaks each guard in turn and fails if no test
notices. `scripts/mutate.sh` is a copy of zeoarchitect's `scripts/mutate.sh` at `35f6681`.

## Conformance

`conformance.Run(t, conformance.Config{Binary: ..., Name: ...})` runs the suite against an app's own
binary: the shapes of the four documents, every exit code it can reach, single-dash refusal, `--json`
purity, and that every command `help` lists exists. zeocore runs it against the sample app; an app
adopts it in its own tests.

## plan and apply (slice 2)

An app declares `Subjects` (things whose state it can read and change) and a `StateDir` (an absolute
path where the backup goes). The kit adds `plan <subject>` and `apply <subject>`.

- `plan` reads the state and prints a `zeo.plan/v1` document: `app`, `subject`, the `changes` (each
  `target`, `before`, `after`, sorted by target) and a `plan_id`, the sha256 of
  `{app, subject, changes}` as canonical JSON (RFC 8785), the same digest `zeocore digest` computes.
  It changes nothing. A target or value that is not valid UTF-8, or a target named twice, is refused,
  because the plan and its undo could not be exact.
- `apply` re-reads the targets and refuses (`plan_stale`, exit 3) unless each still holds its `before`
  value. For a `Shared` subject (org rules, council config, seat permissions) it runs only a reviewed
  plan: `apply <subject> --plan FILE`. The file is read strictly: its schema, app and subject must
  match, its `plan_id` must match its own body, and **its bytes must be exactly what the kit would
  have printed**: Go's decoder matches keys without regard to case, so a file with `Changes` beside
  `changes` is refused rather than shown one way and applied another. No message echoes the path.
- A review binds the `plan_id`: anyone can recompute it, so it proves the file is self-consistent, not
  who wrote it. An app that needs more compares the id with the one a reviewer approved.
- Before it writes, `apply` stores the undo (the plan with `before` and `after` swapped) under
  `StateDir/backups/<subject>/`, mode 0600 in a 0700 directory, and prints the command that applies it
  (the path is shell-quoted when it needs it). The undo is itself checked by the stale test.
- If the `Apply` hook fails, the kit reads the targets again. All still at `before`: `failed` (safe to
  retry). Some moved: it puts them back and verifies (`undone`), or reports `recovery_required` with
  the backup and the undo in the document.
- **No operation lock yet.** `apply` takes none until slice 3; no app should ship `apply` for shared
  state before then.
- Exits: `plan` may exit 1; `apply` may exit 1, 3 and 4 (a missing plan file); both exit 2 on usage.

