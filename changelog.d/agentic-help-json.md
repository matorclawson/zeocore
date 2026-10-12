bump: minor
**zeocore teaches an agent how to use it.** Following the agentic experience standard v1 (ZEOCORE-SOW-31):
- `zeocore help --json` is the machine catalogue: every command, its arguments, what it reads on stdin, the exit family and the schema its result names. A test holds it to each command's parser, so it can't drift from the code.
- Every result line now names its schema, `zeocore.<command>/v1`. An unknown command answers `zeocore.usage/v1` and points at `zeocore help`, and an internal error answers `zeocore.internal/v1`. `zeocore schema <name>` still answers with the contract itself, unchanged.
- `AGENTS.md` at the repository root is the one page an agent reads first: what zeocore does, the five commands that matter, the gate, and what an agent must never do.

The new `schema` key is additive: a caller that ignores unknown keys sees no change. A caller comparing whole answers for equality must allow it, so this is a minor release.
