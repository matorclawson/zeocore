# OpenAI managed Agents API

`zeo_core.integrations.agents` runs one message on a **managed OpenAI agent
session** and answers the agent's tool calls with your own Python functions.
It targets `client.beta.agents` (OpenAI runs the agent loop), not the separate
`openai-agents` package.

You build the OpenAI client. It reads `OPENAI_API_KEY` from your environment;
ZeoCore never accepts, stores or logs a key. You need the `openai` package,
version 3.29 or newer (`pip install "openai>=3.29"`), which `zeocore` does not
install for you.

!!! note "Status"
    The wrapper is tested offline against a fake shaped like the SDK and
    against the SDK's own source. Check the field names of `agent` and
    `environment` against OpenAI's Agents API reference for the version you
    use: ZeoCore passes them through unchanged.

## Run a turn

```python
from openai import OpenAI

from zeo_core.integrations.agents import function_tool, run_native

client = OpenAI(timeout=60)   # reads OPENAI_API_KEY; bounds each network read

def get_rate(arguments):
    # Call your real reader here and return its result as JSON-safe data.
    return {"pair": arguments["pair"], "rate": 1.09}

agent = {
    "model": "<an agent-capable model>",
    "instructions": "Answer only from tool results.",
    "tools": [
        function_tool(
            "get_rate",
            "Read the daily euro reference rate.",
            {
                "type": "object",
                "properties": {"pair": {"type": "string"}},
                "required": ["pair"],
                "additionalProperties": False,
            },
        )
    ],
}

turn = run_native(
    client,
    agent=agent,
    environment={"type": "<environment type>"},
    text="What is the EUR/USD reference rate?",
    handlers={"get_rate": get_rate},
    max_calls=8,
)
print(turn.status, turn.ok)
print(turn.output_text)
```

`run_native` returns a `NativeTurn` with `status` (`completed`, `failed`,
`cancelled` or `incomplete`), `output_text`, `tools_called`, `session_id` and a
fixed `error_code` that never contains provider text. `turn.ok` is true only
for a completed turn with non-blank output.

## Rules that keep it safe

- **The `handlers` mapping is the allowlist.** A function the model calls that
  is not a key of `handlers` stops the run.
- **Tool schemas must be closed** (`"additionalProperties": False`).
- **Bounds:** calls per turn, events, argument size (64 KiB), result size
  (4 MiB), a whole-run `deadline_seconds`, and the client's own per-read
  timeout. A breach returns `incomplete`, never `ok`.
- **A handler's own failure** is reported to the model by the SDK as a failed
  result. Catch expected source errors inside the handler and return a plain
  "unavailable" result instead.
- **Never show the model's prose as data.** Build the values you display from
  what your handlers returned. The [briefing cards guide](../how-to/briefing-cards.md)
  does this.
- **There is no per-session money cap.** The SDK has a `spend_control` field, but the
  service refused it (HTTP 400, parameter `spend_control`) in a bounded run on 2026-10-22,
  so `run_native` never sends it. The real cap is the budget on your OpenAI project.
  `run_native(..., spend_limit_cents=500, estimated_cents_per_call=25)` is a LOCAL check
  on your own estimate: if `max_calls` times the estimate is over the limit, the run is
  refused with `spend_estimate_over_limit` before any request. It is an estimate, not a
  cap.
- **A follow-up message is a new session** carrying the earlier brief.

## Test without a key

`ScriptedTransport` with `run(...)` replays a scripted event stream through
the same tool loop, so you can test your handlers offline. See
`tests/test_integrations/agents/` in the repository for working examples.
