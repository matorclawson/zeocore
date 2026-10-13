# AGENTS.md: zeocore for an agent

**What zeocore is.** A typed Python library for capability authoring: contracts, `@capability` tools, and integrations (Google, HubSpot, YouTube, Bluesky, Revolut and more). It also installs the `zeocore` command, which gives callers that don't import Python the same contracts, plus a client for ZEOconnect's Broker. Python 3.14 or later.

**Install:** `uv tool install zeocore` gives the command. `uv pip install zeocore` gives the library, and an extra adds an integration, for example `zeocore[google]`. `zeocore release` also needs the `github` extra.

## The command: learn it from itself

```bash
zeocore help --json          # every command, argument, stdin, exit code and result schema
```

Every `zeocore` answer is JSON lines on stdout. Events carry `event`, and the last line is the result, carrying `ok` and `schema` (`zeocore.<command>/v1`). The one exception is a successful `zeocore schema <name>`: its answer is the contract document itself, named by its own `$schema` and `$id`. Input is strict JSON on stdin, at most 1 MiB, and errors never echo it. Nothing prompts.

## The commands that matter

| Do | Command |
|---|---|
| Check a value against a contract | `zeocore validate <name> < value.json`; `zeocore schema list` names them |
| Get a contract as JSON Schema 2020-12 | `zeocore schema <name>` |
| Fingerprint a request (RFC 8785 sha256) | `zeocore digest < value.json` |
| Run a ZEOconnect operation | `zeocore invoke <operation_id> --connection C [--wait-approval SECONDS] < arguments.json` |
| Check this machine is ready (Python, session store, pairing, Broker URL, extras), each failure with its fix | `zeocore doctor` |
| Check pairing and connections | `zeocore whoami`, then `zeocore connections` (pair first with `zeocore login`) |

**Exit codes**, the same for every command, also listed in `help --json`:
- 0 done;
- 2 invalid input, nothing sent;
- 10 approval required;
- 11 still in flight, so ask the same again;
- 12 not paired: run `zeocore login`;
- 13 ambiguous, never retried automatically;
- 20 refused or held (for `doctor`: not ready, some check failed);
- 1 internal, which for an effect counts as ambiguous.

`invoke` is idempotent. The same connection, operation, arguments and `--occurrence` replay the stored outcome, and a new `--occurrence` is a deliberate new act.

## Working on zeocore itself

- **The gate is `make verify`:** format check, lint, strict mypy, import contracts, hygiene, secrets, plugin boundary, and the tests with 90 % coverage. Nothing lands without it.
- **Set up with `make setup`**, which installs exactly what `uv.lock` pins.
- **A release is a merge.** Every merge to `main` is released by `.github/workflows/publish.yml`, with a computed version. Add one `changelog.d/<branch>.md` fragment whose first line is `bump: patch|minor|major` ([`changelog.d/README.md`](changelog.d/README.md)). Never edit a version.
- **Format only the files you touch**, for example `ruff format path/to/file.py`. `make format` rewrites the whole tree.
- **Tests that run git** must drop every inherited `GIT_*` variable. The pre-push hook sets `GIT_DIR`, and an inherited `git init <tmp>` then re-initialises the real repository.

## Never

- **Never push to `main`, and never tag.** Work on a branch, and open a PR. Tags are made only by the release workflow.
- **Never put a token, key or `.env` value** in a file, commit, log, PR or message. Read secrets from the environment, and never print them.
- **Revolut's `complete` step is a human checkpoint.** The redirect URL holds a one-time code, so it is read from a hidden terminal prompt or the first line of stdin, never from an argument. If you have no terminal, ask the person to run `complete` themselves, in a terminal or by piping the URL from their own shell. Never ask them to hand you the URL: it would put a one-time code in your transcript.
- **Never call a live provider** (HubSpot, Revolut, Google and so on) without a connection or token you were explicitly given. Tests use fakes.
- **Never weaken a refusal to make a call succeed:** a review binding, a digest check, an idempotency key or a schema.
