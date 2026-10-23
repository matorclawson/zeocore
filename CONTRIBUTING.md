# Contributing to ZeoCore: the contributors charter

ZeoCore is a typed Python library for capability authoring (contracts, `@capability` tools, integrations) and the `zeocore` command that gives other languages the same contracts and a client for ZEOconnect's Broker. **Start here: [AGENTS.md](AGENTS.md)** (what the command does, the exit codes, the rules that never bend). This file says how to change ZeoCore.

**Outside contributors, start here.** Welcome, and thank you. In short: open an issue on `zeroemployeeorg/zeocore` that says what you need; fork the repository; install with `make setup` (Python 3.12+) and run `make verify` before you push; keep the change small, with tests that use fakes (no network, no keys); add one `changelog.d/<topic>.md` fragment; open a pull request from your fork against `main`. Never commit a key, token or `.env` file. Report a vulnerability through [SECURITY.md](SECURITY.md), never in a public issue. The rest of this page is the charter that the maintainers' own automated seats follow: where it mentions seats, the Architect, the council, the operator or `zmsg`, those are our internal roles and you can ignore them; a maintainer will route your pull request.

## Who owns this, and the ruling

The owner is the **`zeocore` seat**. The seat that needs a change builds it: propose it, agree the shape with the owner, the council and the Architect, implement it on your own fork under the rules below, and open a PR. The owner maintains: it answers proposals, reviews PRs, and keeps the shape and the gate strict.

The ruling is *RULING: the requester implements; every core project publishes a contributors charter (v1)*, 2026-10-14, an org record (sha256 `0cc5d7bf8714d6c02a8533ca3867f4a16bda02066797f67dc83001d6da90ec45`). The owner-only areas below are where it still writes or decides who writes the code.

## How to propose

Propose before you build. Send a zmsg to the owner seat `zeocore`, with the subject `PROPOSAL:` and your `sow:` in the body. The owner answers **AGREE**, **AMEND** or **DECLINE**, with reasons. A proposal is short and says the need, the shape, who else gains, and the risks:

| Field | Say |
|---|---|
| Need | What you cannot do today, and what you tried |
| Shape | The public API or CLI you want: function signature, command and stdin fields, result schema, exit codes |
| Contract class | For anything that reaches a provider: `read`, `billed computation` or `effect` (Broker contract, see [docs/integrations/zeoconnect.md](docs/integrations/zeoconnect.md)) |
| Who else gains | At least one other seat or consumer |
| Risks | Custody (any credential), spend, exit-code or protocol change, new dependency |

Where else it goes:

- **A seat:** the zmsg above, with the design note in your own SOW; copy `zeocouncil` and the Architect.
- **Anyone who is not a seat:** an issue on `zeroemployeeorg/zeocore`.
- **A vulnerability:** never a public issue. Follow [SECURITY.md](SECURITY.md).
- **A bug:** an issue with steps to reproduce, the `zeocore version` output and your Python version.

Do not build a credentialed-provider integration before ZEOconnect has frozen its Broker contract and the council has reviewed it. The contract comes first, then the keyless client here (ZEOCORE-SOW-13 sets the order).

## What is welcome

ZeoCore keeps this shape:

- **A typed library.** Strict mypy and Pydantic models. The library's own contract models live under `src/zeo_core/contracts/`; the committed JSON Schemas generated from them are under `contracts/zeocore-v1/`, and a test fails if the two drift.
- **A command, `zeocore`,** with the protocol of `cli_protocol` 1: one JSON document on stdin, JSON lines on stdout, a documented exit family.
- **Integrations** under `src/zeo_core/integrations/<name>/`. Follow `gemini`, `kit`, `hubspot` or `revolut`: service, models, operations, a page in `docs/integrations/`.
- **Keyless clients.** All NEW provider access is keyless: the Broker holds the credential and does the handshake. The local-credential code that predates this is listed under "Credentials and auth" in the owner-only table below. It is owner-only and is migrating to the Broker under ZEOCORE-SOW-13.
- **Agent experience.** Better `help --json`, clearer errors with a next action, fewer prompts, better `AGENTS.md`.
- **Tests with fakes,** and documentation.

Out of scope, and refused in review:

| Not accepted | Why |
|---|---|
| New code that reads or holds a provider key, token or `.env` value | Custody belongs to the Broker |
| A generic arbitrary-URL proxy, or a direct dotenv-to-agent path | Bypasses the Broker's contract and approval |
| A live provider call in a test | Tests use fakes |
| A new exit code, or any change to `cli_protocol` 1, without a protocol bump agreed with the owner | Every client depends on it |
| Any new dependency (runtime, extra, `dev`, `lint`, docs, or a CI action or tool pin) that did not go through the Architect | Supply chain |
| Code that detects it runs under test | `make hygiene-check` fails it |
| Anything tied to one seat's private repository | ZeoCore is public |

## Owner-only areas

For these the requester still writes the proposal and the tests. The owner decides who writes the code.

| Area | Why owner-only |
|---|---|
| **Credentials and auth.** `src/zeo_core/release/app_tokens.py` (the merger's App-token path) and `github/auth.py`; `src/zeo_core/connections/` (`SecretStore`, its Keychain and SQLite adapters, `authorization.py`, `admission.py`); every `src/zeo_core/integrations/*/auth.py` (`database/supabase`, `github`, `google`, `google/mail/operations`, `notion`, `revolut/local`, `social/bluesky`), `notion/oauth.py`, the `credential_paths.py` files (`google`, `social/bluesky`), `src/zeo_core/integrations/revolut/local/`, `src/zeo_core/adapters/http/auth.py`, `src/zeo_core/config/dotenv_loader.py` | They read, store or hand out credentials. They are migrating to the Broker under ZEOCORE-SOW-13 |
| **The device grant and session.** `src/zeo_core/integrations/hosted/` (`client.py`, `transport.py`, `pairing.py`, `file_store.py`, `profile.py`) and the session handling in `src/zeo_core/cli/client.py` | They hold the paired device's access token |
| **Release and signing.** `src/zeo_core/release/` (`signing/`, `pypi/`), `src/zeo_core/cli/release.py`, `docs/how-to/release-signing.md`, `docs/how-to/release-pypi.md` | Signing and custody: the admission rule, the allowlist, matormini-only signing; the PyPI promotion steps |
| **Release and deploy machinery.** `.github/workflows/release-on-merge.yml`, `.github/workflows/publish.yml`, `tools/install-pandoc.sh` | PyPI trusted publishing, the `pypi` environment, a pinned and checksummed pandoc |
| **Pinned contracts.** `contracts/broker-1.4/`, `contracts/merger-v1/`, `contracts/runtime-host-v1/` (vendored, checksummed copies; see each `SOURCE.txt`) and `contracts/zeocore-v1/` (zeocore's published schemas: a name is a promise) | Shared with ZEOconnect, the merger and the runtime host; changes are agreed with them and reviewed by the council |
| **The exit-code family.** `src/zeo_core/cli/protocol.py` and the exit table in `docs/reference/cli.md` | Every client depends on `cli_protocol` 1 |
| **The gate's own configuration.** `Makefile`; the `[tool.ruff]`, `[tool.mypy]`, `[tool.coverage]` and `[tool.pytest]` sections of `pyproject.toml`; `.importlinter`; `mkdocs.yml` and `tools/mkdocs_hooks_v1.py`; `.github/workflows/` (`ci.yml`, `docs.yml`, `zeo.yml` as well as the two release workflows); any committed git hook | A change must pass the gate, never weaken it |
| **Packaging.** `pyproject.toml` (dependency pins, extras, metadata) and `uv.lock` | Version and supply chain |

### Releasing (maintainers only)

Every merge to `main` is a release. Nobody bumps a version, tags or approves. `.github/workflows/publish.yml` (workflow `Publish`) has a job `release` that calls the reusable `.github/workflows/release-on-merge.yml`. Its jobs: `plan` computes the version (the higher of the last tag and PyPI's newest, plus the highest `bump:` among the new `changelog.d` fragments); `build` runs the caller's gate (`tools/install-pandoc.sh`, `make setup`, `make verify`) and builds; `tag` tags `vX.Y.Z` at the merge commit. Then `publish.yml`'s own jobs run: `publish-pypi` publishes by trusted publishing, `smoke-test` installs the published package into a clean venv, and `github-release` creates the GitHub Release from the fragments. `pyproject.toml` has no version literal. If `smoke-test` fails, the release did NOT succeed, even if the publish step passed.

One-time trusted-publisher setup (on PyPI's web UI): owner `zeroemployeeorg`, repository `zeocore`, workflow filename `publish.yml`, environment `pypi`. All four must match exactly, or the publish fails with `invalid-publisher` and no hint which field is wrong.

## How to build

**Fork and branch.** Fork `matorclawson/zeocore`, branch `<your-seat>/<topic>-<yyyymmdd>`, where `<your-seat>` is the authoring seat's zmsg name (for example `zeo-rt/…`, `rasa-bench/…`, `zeroemployee/…`). The lane reads the seat from the text before the first `/` of the branch, so this is the lane form; anything else is refused. Until the A1 seat-list install, only the owner seat's PRs are admitted on this repository; after it, every stream seat is admitted. Name the requesting seat's SOW in the PR body as `sow: <id>`. Never push to `main`. Never tag.

**Dependencies.** Every dependency, new tool or pin goes through the Architect. Never ask the operator.

**Set up.** `make setup` installs exactly what `uv.lock` pins (Python 3.12; the package requires 3.12+; every gate extra, `dev` and `lint`, the documentation group) into `.venv`. `make help` lists every target.

**Heavy work on ZeoWork.** Keep the venv, caches and temporary files off the system disk:

```bash
export TMPDIR=/Volumes/ZeoWork/<you>/tmp UV_CACHE_DIR=/Volumes/ZeoWork/<you>/uv-cache
UV_PROJECT_ENVIRONMENT=/Volumes/ZeoWork/<you>/venv make setup PYTHON=/Volumes/ZeoWork/<you>/venv/bin/python
make verify PYTHON=/Volumes/ZeoWork/<you>/venv/bin/python
```

**The gate is `make verify`.** It must pass at the head you submit. It runs, in order: `format-check`, `ruff` lint, `mypy` strict, `arch-check` (import-linter contracts in `.importlinter`), `hygiene-check`, `hygiene-secrets`, `plugin-boundary`, the tests with coverage (floor 90%, including `tests/test_docs`), and then `docs-build`: the `tests/docs_site` unit tests and `mkdocs build --strict`. For a clean reinstall first, run `make verify-full`. For inner-loop work: `make lint`, `make typecheck`, `make test-fast`, `make test-docs`, `make test-module M=test_fs`.

**Code.**

- Ruff (rules in `[tool.ruff]`, line length 88, double quotes). Format only the files you touch: `ruff format <paths>`. Never run `make format` on the tree; it rewrites unrelated files.
- Fully typed under `mypy --strict`. Untyped code is not gated.
- Import `zeo_core.tools` mixins from `zeo_core.tools`, not from `zeo_core.tools.mixins.*`.
- Production code never detects that it runs under test. Inject a parameter, fixture or fake.
- Examples under `examples/` import `zeo_core` and run with `uv run examples/<name>.py`. The beginner examples stay deterministic, offline and credential-free.

`make setup` also points git at the hooks committed in `.githooks` (it runs
`git config core.hooksPath .githooks`; `make hooks` does just that step). The
`pre-push` hook runs `make verify` before anything leaves your machine, so a
fresh clone has the gate without anyone installing a hook by hand. It tests the
checked-out tree, so it refuses a push with uncommitted tracked changes or of a
ref that isn't `HEAD`. `git push --no-verify` skips it; don't use it, because the
merge gate runs the same check. A failing gate is fixed, then you push again. If you already set `core.hooksPath` to something else, `make setup` keeps
it and prints the one command that installs these.

Run `make help` at any point to see every available target.

**Tests.** They live under `tests/`, mirroring `src/zeo_core/` (see [tests/README.md](tests/README.md)). Rules:

- Test the failure path as well as the success path. Every refusal gets a negative control.
- A test that proves a refusal does not echo its input value (use a sentinel).
- Fakes only: no provider call, no key, no network.
- Tests that run git must drop every inherited `GIT_*` variable: a hook sets `GIT_DIR`, and an inherited one re-initialises the real repository.
- There is no mutation tooling in this repo. Say so in your PR; the negative controls stand in for it.

**Docs and changelog.** User-visible change needs a page or section under `docs/` (strict mkdocs build; `make test-docs` checks repository-relative links and anchors) and one `changelog.d/<topic>.md` fragment (name it after your branch topic, the part after the last `/`) whose first line is `bump: patch`, `bump: minor` or `bump: major` ([changelog.d/README.md](changelog.d/README.md)). Never edit a merged fragment.

**CI on a pull request.** Run the full gate yourself before you open the PR (`make verify`, and `make kit-verify` when you touched `kit/`) and paste its summary in the PR body. The PR itself runs one job, `smoke` (ruff format and lint, and a wheel build; under three minutes, no secrets). The full Python gate and the kit's gate with its mutation rows run after a merge to `main` and every night (`ci.yml`; `zeo.yml` certifies `main` too)

**Exit codes and `--json`.** The command's protocol is zeocore's `cli_protocol` 1 (also `zeocore help --json` and [docs/reference/cli.md](docs/reference/cli.md)); a code in it keeps one meaning everywhere, and a new one needs a protocol bump agreed with the owner:

- stdout is JSON lines. The last line is the result and carries `ok` and `schema` (`zeocore.<command>/v1`).
- Exit codes: 0 done, 2 invalid input or command, 10 approval required, 11 waiting, 12 not paired, 13 ambiguous, 20 held or refused (for `doctor`, not ready), 1 internal.
- stdin is one strict JSON document: no duplicate keys, no NaN, at most 1 MiB.
- Errors never echo input, and end with the next action.
- Nothing prompts, except a declared human step.

## How to submit

- **One PR at a time is ready to merge.** Stack the next one on your fork.
- **Fork to gold.** Open the PR from your fork against `zeroemployeeorg/zeocore` `main`. Merges are merge commits: no squash, no rebase. Update a PR with a merge of `main`.
- **PR body.** Use these labelled parts, in this order: **What** (and why); **Look here** (files and functions to read first); **Contract changes** (exit codes, outcomes, schemas, new dependencies, or "none"); **Gate** (the `make verify` numbers); **Not verified**; **Head** (the exact sha).
- **Evidence.** A `make verify` log run AT the head you submit, on a clean tree, whose first line is `head: <full sha>`; store it on ZeoWork and name the path in the PR. For a large change (many files, or a contract or release path) add a bundle with `SHA256SUMS`.
- **Route.** Route: owner, then council, then zeomerger. Until zeomerger is live for that repo, the Architect sequences the merge. The operator never merges, and is never asked. The council reviews the exact head, and its verdict is separate from the owner's.
- **After a verdict, push nothing to that head.** A push dismisses the review and moves the head. New work goes in the next PR.
- **Every merge is a release,** with a computed version (see Releasing above). You never tag.
- **Never put a secret** in a file, commit, log, PR or message.

## Response times

| What | The owner commits to |
|---|---|
| Answer a proposal (AGREE, AMEND or DECLINE, with reasons) | Within 4 hours |
| Review a PR at its exact head | Within 1 working day |
| Re-review after you merge `main` or fix findings | Within 4 hours |

Longer only with a stated reason. The council's verdict follows its own queue and is separate. While a freeze is in force (for example the merger's proof), the council still reviews, but approval and merge wait. The owner says so in its answer and keeps reviewing.

## Escalation

A missed time goes to the Architect by zmsg.
