bump: patch

`run_native` no longer sends the SDK's `spend_control` and makes no promise of a
provider-side money cap. The service refused the field (HTTP 400, parameter
`spend_control`) in a bounded run on 2026-10-22, even though openai 3.30.0 and later accept
it in `sessions.create`; the real cap is the budget on your OpenAI project. `spend_limit_cents`
is now a local check on your own estimate: pass `estimated_cents_per_call` too, and a run
whose `max_calls` times the estimate is over the limit is refused with
`spend_estimate_over_limit` before any request (`spend_limit_required`,
`spend_estimate_required`, `spend_limit_invalid` and `spend_estimate_invalid` refuse the
other misuses). This corrects the 0.35.4 notes, which said the limit was sent and enforced by
the provider.
