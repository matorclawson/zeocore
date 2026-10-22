bump: patch

`run_native(spend_limit_cents=...)` needs `openai>=3.30.0` and says so. `sessions.create` in
openai 3.29.0 has no `spend_control`, so on that SDK the call is now refused with
`spend_limit_unsupported_by_sdk` before any request, instead of failing as a generic
provider error. It was checked against the real SDK at 3.29.0 (refused), 3.30.0 and 3.36.0
(accepted). This also corrects the 0.35.4 notes: the limit is REQUESTED from the provider
(sent as `spend_control`); that the provider stops a session over the limit is not yet shown
by a funded run. The agents guide documents both.
