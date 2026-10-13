bump: minor
**`zeocore release pypi wait`: wait for the publish, don't poll `reconcile`.** Rule 5 of the agentic experience standard (ZEOCORE-SOW-31), the second half after `invoke --wait`.
- It takes `{"manifest", "seconds"}` (1 to 3600) and runs `reconcile`, which only reads, every 5 s and once more at the deadline. It never makes the tag, never approves and never retries an effect.
- It waits only while `reconcile` says `waiting`; `published`, `publish_failed`, `tag_absent`, `tag_mismatch` and `target_unreadable` end it at once.
- It emits one `{"event": "waiting", "reason", "elapsed"}` line for each reason it waits on. The last line is the last `reconcile` answer with its own exit status, plus `waited`; if the time runs out it is `waiting` and the exit is 11.
- `zeocore help`, `docs/reference/cli.md` and `AGENTS.md` describe it.
