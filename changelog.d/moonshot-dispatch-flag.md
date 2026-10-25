bump: patch

A Moonshot reply that was sent and answered but is unusable (empty, cut off at the token limit, or ended as a tool call) now counts as a started attempt in the outer execution policy: it is a `MALFORMED_RESPONSE` with `dispatch_started` true, where it was recorded as never sent. It is still not retried. A request the client refuses before sending (tools, for example) is still `dispatch_started` false.

sow: zeocore-SOW-44
