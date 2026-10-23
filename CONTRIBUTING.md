# Contributing to ZeoCore

ZeoCore is a typed Python library for capability authoring (contracts, `@capability` tools, integrations) and the `zeocore` command that gives other languages the same contracts. Thank you for helping. **Start with [AGENTS.md](AGENTS.md)** (what the command does, the exit codes, the rules that never bend); this file says how to change ZeoCore.

Org seats follow an internal charter; everything below is for everyone else.

## Before you start

- **Open an issue first** on `zeroemployeeorg/zeocore` for anything bigger than a typo or a small fix. Say what you need, what you tried, and the API or command you would like. A maintainer will agree, ask for changes, or explain why not, before you spend time building.
- **A vulnerability** is never a public issue. Follow [SECURITY.md](SECURITY.md).
- **A bug** is an issue with steps to reproduce, the `zeocore version` output and your Python version.
- Be kind: see the [code of conduct](CODE_OF_CONDUCT.md).

## What is welcome

- Fixes and tests, and clearer documentation.
- Better `help --json`, clearer errors that end with a next action, fewer prompts, better `AGENTS.md`.
- Integrations under `src/zeo_core/integrations/<name>/`. Follow an existing one (`gemini`, `hubspot`, `revolut`): service, models, operations, a page in `docs/integrations/`.
- Tests with fakes.

Not accepted:

| Not accepted | Why |
|---|---|
| New code that reads or holds a provider key, token or `.env` value | New provider access is keyless: a broker holds the credential |
| A generic arbitrary-URL proxy, or a direct dotenv-to-agent path | It bypasses the broker's contract and approval |
| A live provider call in a test | Tests use fakes: no network, no key |
| A new exit code, or any change to `cli_protocol` 1, without a protocol bump agreed in the issue | Every client depends on it |
| A new dependency (runtime, extra, dev, docs, or a CI action pin) without agreement in the issue | Supply chain |
| Code that detects it runs under test | `make hygiene-check` fails it |

Some areas are changed by the maintainers themselves: credentials and signing, release and deploy workflows, the published schemas in `contracts/zeocore-v1/`, the exit-code family, the gate's own configuration (`Makefile`, `pyproject.toml` tool sections, `.importlinter`, `mkdocs.yml`) and packaging (`pyproject.toml`, `uv.lock`). Open an issue first and expect the maintainers to write or closely guide the change.

## How to build

1. **Fork** `zeroemployeeorg/zeocore` and make a branch.
2. **Set up** (Python >=3.12): `make setup` installs exactly what `uv.lock` pins into `.venv`. `make help` lists every target.
3. **Write the change**, with tests.
4. **Run the gate:** `make verify` (format check, ruff, strict mypy, import contracts, hygiene, tests, docs build). It must pass at the head you submit. `make setup` also installs a pre-push hook that runs it; please do not bypass it with `--no-verify`.

Code rules:

- Ruff (line length 88, double quotes). Format only the files you touch with `ruff format <paths>`; never reformat the whole tree.
- Fully typed under `mypy --strict`.
- Production code never detects that it runs under test; inject a parameter, fixture or fake.
- Examples under `examples/` stay deterministic, offline and credential-free.

Tests live under `tests/`, mirroring `src/zeo_core/` (see [tests/README.md](tests/README.md)). Test the failure path as well as the success path. A test that proves a refusal must not echo its input value (use a sentinel). Tests that run git must drop every inherited `GIT_*` variable.

Documentation: a user-visible change needs a page or section under `docs/` and one `changelog.d/<topic>.md` fragment that starts with a line `bump: patch`, `bump: minor` or `bump: major`, then a short description.

The command's protocol is `cli_protocol` 1 (`zeocore help --json` and [docs/reference/cli.md](docs/reference/cli.md)): stdout is JSON lines whose last line carries `ok` and `schema`; stdin is one strict JSON document of at most 1 MiB; errors never echo input; nothing prompts, except a declared human step.

## How to submit

- Open the pull request from your fork against `main`. Merges are merge commits; update a branch by merging `main`, not by rebasing.
- Say **what** changed and why, **where to look** first, and any change to an exit code, schema or dependency. Paste the end of your `make verify` run.
- A maintainer reviews. After a review, push only the fixes it asks for; new work goes in a new pull request.
- Every merge to `main` is released automatically by the maintainers' workflow; you never bump a version or tag.
- Never put a key, token or secret in a file, commit, log or message.
