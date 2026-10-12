bump: minor
**`zeocore invoke --wait SECONDS`: wait for the outcome, don't poll.** Rule 5 of the agentic experience standard (ZEOCORE-SOW-31).
- It asks again with the same request key every 5 s, for up to SECONDS (1 to 3600), through a person's approval, an answer still in flight, and ZEOconnect being unavailable.
- It emits each kind of wait event once, and exits with the final answer's own code. At the deadline it exits with the last answer's: 10 or 11.
- An ambiguous answer (13), or a Broker that didn't answer, is never retried.
- `--wait-approval` keeps its meaning (approval only), and the two can't be combined.
- `zeocore help` and `AGENTS.md` describe it.
