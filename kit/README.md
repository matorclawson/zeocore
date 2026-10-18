# kit: the zeo-cli/v1 command kit

A Go module (`github.com/zeroemployeeorg/zeocore/kit`) that gives every zeo and dt app the same
commands, grammar, exit codes and result line. The contract is zeoarchitect SOW-05 rev d with the
Architect's section 14 ruling; the build plan is ZEOCORE-SOW-34. **Slices 1 to 3 of 7:** the grammar,
the exit table, the `RESULT` line, the `help`, `version`, `doctor` and `status` verbs, `plan` and
`apply`, and `update`, `rollback` and `recover`, plus the conformance suite.

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
  that is not a plain name; a hook's error text is folded to one printable line. A flag or command name is named only when
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
  match, its `plan_id` must match its own body, and **it must be canonically equal to what the kit
  would have written** (the same keys, in any order, with any whitespace): Go's decoder matches keys without regard to case, so a file with `Changes` beside
  `changes` is refused rather than shown one way and applied another. No message echoes the path.
- A review binds the `plan_id`: anyone can recompute it, so it proves the file is self-consistent, not
  who wrote it. An app that needs more compares the id with the one a reviewer approved.
- Before it writes, `apply` stores the undo (the plan with `before` and `after` swapped) under
  `StateDir/backups/<subject>/`, mode 0600 in a 0700 directory, and prints the command that applies it
  (the path is shell-quoted when it needs it). The undo is itself checked by the stale test.
- If the `Apply` hook fails, the kit reads the targets again. All still at `before`: `failed` (safe to
  retry). Some moved: it puts them back and verifies (`undone`), or reports `recovery_required` with
  the backup path in the document. The undo it prints then belongs to a state the targets are no longer
  in, so it refuses `plan_stale` until a person has looked at the targets; it is the record of what was
  planned, not a button.
- **Operation lock.** `apply`, `update`, `rollback` and `recover` take one lock per app in `StateDir`;
  a second operation is refused `busy` (exit 3).
- Exits: `plan` may exit 1; `apply` may exit 1, 3 and 4 (a missing plan file); both exit 2 on usage.

## update, rollback and recover (slice 3)

**Unix only** (macOS and Linux): the lock and the directory syncs use `flock` and `fsync`.

An app sets `Updater` (an absolute `Root`, the `Protected` paths no step may touch, and hooks: `Fetch`,
`Preflight`, `Install`, `Verify`, and for a service `Fence`, `Stop`, `Start`, `Ready` and `Down`). The
kit then runs the six steps of the contract as one state machine: fetch and verify, preflight, install
beside the current version, stop, switch the `current` symlink atomically, start and prove readiness,
and back out on any failure.

- **Durable.** An intent is written (file synced, renamed, directory synced) before the service is
  stopped; the switch and its removal sync the directory too. A crash leaves `recovery_required`, and
  every later operation refuses until `recover` has cleared it.
- **Live rule fails closed.** If `Status` cannot say whether the app is live, it is treated as live: the
  operation refuses with the live reason (`live`, or an app's own, e.g. `studio_live`) unless the app
  has an idle `Fence` and takes it.
- **Guard compares what the kernel would open.** It takes absolute paths without `..` and resolves each
  path as the kernel does, component by component: a link is replaced by its target (an absolute target
  restarts from the root), and every existing directory on the way is compared by `os.SameFile`, so a
  case or Unicode-normalisation spelling the file system folds, or a link into a subdirectory, names the
  same thing. Past the first component that does not exist yet only names are compared, so a new file
  beside the protected tree is allowed. A link that does not resolve (or loops) is refused, and a
  protected path that cannot be resolved refuses everything. **Guard is advisory**: it checks a path, not
  the descriptor a hook later opens, so a link swapped after Guard returns is not stopped by it. A hard
  link to a protected file is not caught by Guard (not even when the protected path is the file itself);
  the snapshot catches the write. A name that does not exist yet is compared folded (ASCII case, and any
  non-ASCII byte alike), so a protected path that is absent cannot be created through a folded spelling. The snapshot is the backstop.
- **Protected paths.** `Guard` takes absolute paths without `..` (a `..` after a symbolic link would be
  collapsed as text before the link is resolved, so it is refused outright) and resolves links on both
  sides. The kit also snapshots the protected paths before `Fetch` (or a rollback's preflight) and
  compares them just before the switch and again after the new version is proved: any hook that touched
  one ends the operation. **A touched file is detected, not undone**: before the switch the operation
  ends `failed` (nothing switched; the file stays as the hook left it); after, the version is switched
  back (`undone`), also leaving the file as it is. Every protected file keeps its exact size and bytes,
  except files under `Updater.AppendOnly` (append-only state), which may grow with their first bytes
  unchanged. No entry may appear, vanish or change mode, and a symbolic link (inside the tree, or the
  protected path itself) may not be retargeted. `Protected` and `AppendOnly` are copied at the start of a
  run, so a hook cannot widen them; **do not let `AppendOnly` cover key custody**. Limits of the snapshot,
  said plainly: a link inside a tree that points outside it is compared by its target string only; a
  write undone inside the window (truncate, then restore) is invisible; owner, ACL and extended
  attributes are not compared; every regular file is hashed in full, several times per update, so a
  large append-only journal costs several full reads.
- **Custody (key custody, never opened).** `Updater.Custody` lists key-custody paths. The kit never
  reads them: each is checked by `lstat` identity only (device, inode, mode, owner, group, size,
  mtime; or that it is absent) before Fetch, just before the switch and after the proof. Any change,
  including a replaced file with the same bytes, a touched mtime, a file that appears or vanishes,
  ends the operation as `protected`, the same way a touched Protected file does. A custody path is
  one entry: for a directory only the directory itself is checked, not what is inside it. It must be
  absolute, must not go through a link that does not resolve, and must not overlap the install root
  or a Protected path (Protected files are hashed, so keep key custody out of it). `Guard` refuses a
  path at or under a custody path. This detects a touch, it does not undo it.
- **recover looks before it acts.** It first proves the version that is current as it stands (ready and
  verify, no stop). If one of the intent's two versions is running and proved, only the record is left,
  and it is cleared. The app's `Ready` is asked with a zero time (a readiness event at any time counts)
  and must still check the live process (its pid and a readiness event). Only if that fails does it act: it applies the live rule, then goes through the two
  versions, From first, and proves the first that passes. A `Down` migration must therefore be
  all-or-nothing: the intent does not record it. An unreadable intent is cleared once current is proved.
- **recover exists for an app with `Subjects` and no `Updater`** (its `help` lists `zeo.update/v1`; an unreadable intent there can only be removed by hand, since only an apply writes one) (a half-done apply leaves an intent that only recover clears); an unfinished update cannot be recovered without an updater.
- **An apply leaves an intent too.** A killed `Apply` hook, or a half-done apply the kit could not put
  back, blocks every later update, rollback or apply until `recover` reads the targets: all at their
  before values, or all at their after values, clears it; a mix stays `recovery_required` with the
  backup path in the document.
- **rollback --to VERSION** goes back to an installed version named by `--to` (the default is the
  previous one). An unknown, pruned or not-a-directory version is refused with `not_found`; the current
  version with `current`. Further back than the previous version, the kit must prove no state migration
  lies between: it keeps the last 64 switches (each with whether a migration, or a down-migration,
  crossed it) and walks them back from the current version. A crossed migration, a gap, or no record
  (a history written before this, or after a recover) is refused as `migration`: a `Down` hook reverses
  one step only, so go back one version at a time. The same live, fence, preflight and recovery rules
  apply as for a plain rollback. `not_found` is exit 4 (the RESULT line stays
  `refused`); the other refusals are exit 3.
- Refusals: `busy`, `recovery_required`, `current`, `downgrade`, `space`, the live reasons, `protected`.
  A version name that holds `.tmp-` is not a version (that is how an install in progress is marked).
  Exits: 1 (failed), 2 (usage), 3 (refused). `update --check` writes nothing and takes no lock. A failure
  to create the lock or the state directory is `failed`, not `busy`. `--to` must be a version word; the
  value is not echoed. Hook error texts and intent fields are folded to one line. Install directories
  (`versions/*.tmp-<pid>`) left by a crashed process are removed at the next install.
