bump: minor

Add an explicitly injected model-operation executor that retains and admits the
exact fenced intent before one dispatch, validates receipts and artifact bytes,
and preserves the original request on ambiguous outcomes. No model receiver,
credential reader, transport, provider registration or automatic retry is added.
