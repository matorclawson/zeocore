# Contributing to ZeoCore: the contributors charter

ZeoCore is a typed Python library for capability authoring (contracts, `@capability` tools, keyless integrations) and the `zeocore` command that gives other languages the same contracts and a client for ZEOconnect's Broker. **Start here: [AGENTS.md](AGENTS.md)** (what the command does, the exit codes, the rules that never bend). This file says how to change ZeoCore.

The seat that needs a change builds it. Propose it, agree the shape with the owner (the `zeocore` seat), the council and the Architect, implement it on your own fork under the rules below, and open a PR. The owner maintains: it answers proposals, reviews PRs, and keeps the shape and the gate strict.

## How to propose

Propose before you build. A proposal is short and says:

| Field | Say |
|---|---|
| Need | What you cannot do today, and what you tried |
| Shape | The public API or CLI you want: function signature, command and stdin fields, result schema, exit codes |
| Contract class | For anything that reaches a provider: `read`, `billed computation` or `effect` (Broker contract, see [docs/integrations/zeoconnect.md](docs/integrations/zeoconnect.md)) |
| Who else gains | At least one other seat or consumer |
| Risks | Custody (any credential), spend, exit-code or protocol change, new dependency |

Where it goes:

- **A seat:** a short note in your own SOW, sent by zmail to `zeocore` and `zeocouncil`, copying the Architect.
- **Anyone else:** an issue on `zeroemployeeorg/zeocore`.
- **A vulnerability:** never a public issue. Follow [SECURITY.md](SECURITY.md).
- **A bug:** an issue with steps to reproduce, the `zeocore version` output and your Python version.

Do not build a credentialed-provider integration before ZEOconnect has frozen its Broker contract and the council has reviewed it. The contract comes first, then the keyless client here (ZEOCORE-SOW-13 sets the order).

## What is welcome

ZeoCore keeps this shape:

- **A typed library.** Strict mypy, Pydantic models, canonical contracts under `contracts/`.
- **A command, `zeocore`,** with the protocol of `cli_protocol` 1: one JSON document on stdin, JSON lines on stdout, a documented exit family.
- **Integrations** under `src/zeo_core/integrations/<name>/`. Follow `gemini`, `kit`, `hubspot` or `revolut`: service, models, operations, a page in `docs/integrations/`.
- **Keyless clients.** The Broker holds every provider credential and does the handshake. ZeoCore never holds one.
- **Agent experience.** Better `help --json`, clearer errors with a next action, fewer prompts, better `AGENTS.md`.
- **Tests with fakes,** and documentation.

Out of scope, and refused in review:

| Not accepted | Why |
|---|---|
| Reading or holding a provider key, token or `.env` value | Custody belongs to the Broker |
| A generic arbitrary-URL proxy, or a direct dotenv-to-agent path | Bypasses the Broker's contract and approval |
| A live provider call in a test | Tests use fakes |
| A new exit code, or any change to `cli_protocol` 1, without a protocol bump agreed with the owner | Every client depends on it |
| A new runtime dependency without the operator's OK | Supply chain |
| Code that detects it runs under test | `make hygiene-check` fails it |
| Anything tied to one seat's private repository | ZeoCore is public |

## Owner-only areas

For these the requester still writes the proposal and the tests. The owner decides who writes the code.

| Area | Why owner-only |
|---|---|
| `src/zeo_core/release/signing/` and `docs/how-to/release-signing.md` | Signing and custody: the admission rule, the allowlist, matormini-only signing |
| `.github/workflows/release-on-merge.yml`, `.github/workflows/publish.yml`, `tools/install-pandoc.sh` | Release and deploy machinery: PyPI trusted publishing, the `pypi` environment, a pinned and checksummed pandoc |
| Session and pairing code: `src/zeo_core/integrations/hosted/pairing.py`, `file_store.py`, `profile.py`, and the session handling in `src/zeo_core/cli/client.py` | Credentials |
| `contracts/broker-1.4/`, `contracts/zeocore-v1/` and the other pinned contracts | Shared with ZEOconnect and the merger; changes are agreed with them and reviewed by the council |
| The exit-code family and `src/zeo_core/cli/protocol.py` | Every client depends on `cli_protocol` 1 |
| `pyproject.toml` packaging, dependency pins and `uv.lock` | Version and supply chain |

### Releasing (maintainers only)

Every merge to `main` is a release. Nobody bumps a version, tags or approves. `.github/workflows/publish.yml` calls `release-on-merge.yml`, which computes the version (the higher of the last tag and PyPI's newest, plus the highest `bump:` among the new `changelog.d` fragments), runs `make verify`, builds, tags `vX.Y.Z` at the merge commit, publishes to PyPI by trusted publishing, and creates the GitHub Release from the fragments. `pyproject.toml` has no version literal. The workflow's `smoke-test` job installs the published package into a clean venv: its failure means the release did NOT succeed, even if the publish step passed.

One-time trusted-publisher setup (on PyPI's web UI): owner `zeroemployeeorg`, repository `zeocore`, workflow filename `publish.yml`, environment `pypi`. All four must match exactly, or the publish fails with `invalid-publisher` and no hint which field is wrong.

## How to build

**Fork and branch.** Fork `zeroemployeeorg/zeocore` to your own account. Branch as `<seat>/<topic>-<yyyymmdd>`. Never push to `main`. Never tag.

**Set up.** `make setup` installs exactly what `uv.lock` pins (Python 3.14; the package requires 3.14+; every gate extra, `dev` and `lint`, the documentation group) into `.venv`. `make help` lists every target.

**Heavy work on ZeoWork.** Keep the venv, caches and temporary files off the system disk:

```bash
export TMPDIR=/Volumes/ZeoWork/<you>/tmp UV_CACHE_DIR=/Volumes/ZeoWork/<you>/uv-cache
UV_PROJECT_ENVIRONMENT=/Volumes/ZeoWork/<you>/venv make setup PYTHON=/Volumes/ZeoWork/<you>/venv/bin/python
make verify PYTHON=/Volumes/ZeoWork/<you>/venv/bin/python
```

**The gate is `make verify`.** It must pass at the head you submit. It runs, in order: `format-check`, `ruff` lint, `mypy` strict, `arch-check` (import-linter contracts in `.importlinter`), `hygiene-check`, `hygiene-secrets`, `plugin-boundary`, and the tests with coverage (floor 90%). For a clean reinstall first, run `make verify-full`. For inner-loop work: `make lint`, `make typecheck`, `make test-fast`, `make test-docs`, `make test-module M=test_fs`.

**Code.**

- Ruff (rules in `[tool.ruff]`, line length 88, double quotes). Format only the files you touch: `ruff format <paths>`. Never run `make format` on the tree; it rewrites unrelated files.
- Fully typed under `mypy --strict`. Untyped code is not gated.
- Import `zeo_core.tools` mixins from `zeo_core.tools`, not from `zeo_core.tools.mixins.*`.
- Production code never detects that it runs under test. Inject a parameter, fixture or fake.
- Examples under `examples/` import `zeo_core` and run with `uv run examples/<name>.py`. The beginner examples stay deterministic, offline and credential-free.

**Tests.** They live under `tests/`, mirroring `src/zeo_core/` (see [tests/README.md](tests/README.md)). Rules:

- Test the failure path as well as the success path. Every refusal gets a negative control.
- A test that proves a refusal does not echo its input value (use a sentinel).
- Fakes only: no provider call, no key, no network.
- Tests that run git must drop every inherited `GIT_*` variable: a hook sets `GIT_DIR`, and an inherited one re-initialises the real repository.
- There is no mutation tooling in this repo. Say so in your PR; the negative controls stand in for it.

**Docs and changelog.** User-visible change needs a page or section under `docs/` (strict mkdocs build; `make test-docs` checks repository-relative links and anchors) and one `changelog.d/<branch>.md` fragment whose first line is `bump: patch`, `bump: minor` or `bump: major` ([changelog.d/README.md](changelog.d/README.md)). Never edit a merged fragment.

**The command's conventions** (`cli_protocol` 1; also `zeocore help --json` and [docs/reference/cli.md](docs/reference/cli.md)):

- stdout is JSON lines. The last line is the result and carries `ok` and `schema` (`zeocore.<command>/v1`).
- Exit codes: 0 done, 2 invalid input or command, 10 approval required, 11 waiting, 12 not paired, 13 ambiguous, 20 held or refused (for `doctor`, not ready), 1 internal.
- stdin is one strict JSON document: no duplicate keys, no NaN, at most 1 MiB.
- Errors never echo input, and end with the next action.
- Nothing prompts, except a declared human step.

## How to submit

- **One PR at a time is ready to merge.** Stack the next one on your fork.
- **Fork to gold.** Open the PR from your fork against `zeroemployeeorg/zeocore` `main`. Merges are merge commits: no squash, no rebase. Update a PR with a merge of `main`.
- **PR body.** Use this order: what and why; a look-here list (files and functions to read first); contract and behaviour changes (exit codes, outcomes, schemas, new dependencies, or "none"); the gate numbers; what is NOT verified; the exact head sha.
- **Evidence.** The `make verify` log, with the head sha first, stored on ZeoWork (name the path in the PR). For a large change add a bundle with `SHA256SUMS`.
- **Review route.** The owner reviews first, then `zeocouncil` reviews the exact head, then the merger merges. Today the operator merges by hand.
- **After a verdict, push nothing to that head.** A push dismisses the review and moves the head. New work goes in the next PR.
- **Every merge is a release,** with a computed version (see Releasing above). You never tag.
- **Never put a secret** in a file, commit, log, PR or message.

## Response times

| What | The owner commits to |
|---|---|
| Answer a proposal (accept, change the shape, or decline with a reason) | Within 4 hours |
| First review of a PR at its exact head | Within 1 working day |
| A re-review after you merge `main` or fix findings | Within 1 working day |

The council's verdict follows its own queue and is separate. While a freeze is in force (for example the merger's proof), the council still reviews, but approval and merge wait. The owner says so in its answer and keeps reviewing.
