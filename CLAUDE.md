# ZeoCore: notes for coding agents

Start with [AGENTS.md](AGENTS.md) (what the `zeocore` command does, the exit codes, the rules
that never bend) and [CONTRIBUTING.md](CONTRIBUTING.md) (how to change ZeoCore).

- Python 3.12+. Set up with `make setup`; the gate is `make verify` and it must pass before a push.
- Tests use fakes: no network, no provider key, no `.env` read. Never commit a secret.
- Format only the files you touch (`ruff format <paths>`); never run `make format` on the tree.
- Add one `changelog.d/<topic>.md` fragment for a user-visible change.
