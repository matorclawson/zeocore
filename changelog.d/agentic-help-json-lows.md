bump: patch
**`zeocore help` describes each argument and release verb exactly.** Following zeocouncil's review of #119:
- Each argument now says whether it is required, whether it takes a value, and its `choices`. `zeocore release` lists every verb with the exact stdin fields it requires.
- Tests hold all of it to the code: each command's `add_argument` keywords, the release command's verb tables, and the hand-parsed commands' accepted arguments.
- The result label is keyed on the command: only a successful `zeocore schema <name>` answers without it, and no answer can override it.
- The catalogue and `AGENTS.md` now state that exception.
