# kit: the zeo-cli/v1 command kit

A Go module (`github.com/zeroemployeeorg/zeocore/kit`) that gives every zeo and dt app the same
commands, grammar, exit codes and result line. The contract is zeoarchitect SOW-05 rev d with the
Architect's section 14 ruling; the build plan is ZEOCORE-SOW-34. **Slice 1 of 7:** the grammar, the
exit table, the `RESULT` line, and the `help`, `version`, `doctor` and `status` verbs, plus the
conformance suite. `update`, `rollback`, `plan` and `apply` come in later slices.

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
  single-dash flag is refused with exit 2 and a message naming the double-dash form. `--json`
  prints one document on stdout and nothing else, `schema` first. Nothing prompts: the kit never
  reads stdin. `<binary> --help` is `help`.
- **Exit codes:** 0 ok, 1 failed, 2 usage, 3 refused, 4 not_found, 5 timeout, 6 unavailable,
  7 not_ready. `help --json` prints the table.
- **RESULT line:** `RESULT <app> <verb> <outcome> from=<v> to=<v> [reason=<token>] [undo="<cmd>"]`,
  built by `kit.Result.Line`, which refuses a line that breaks the grammar.
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
