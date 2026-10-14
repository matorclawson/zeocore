bump: patch

An effectful hosted call whose success answer cannot be read (an integer over CPython's 4300-digit limit, truncated JSON, bytes that are not UTF-8, or a body over the client limit) now raises `HostedResponseUnreadableError`: the outcome is unknown, so replay the same idempotency key and never use a new one. `zeocore invoke` answers it with exit 13, outcome `ambiguous` and `retry: same_request`, where it was a plain shape error. A read keeps its plain error, and the digit limit stays as a guard.
