bump: patch

Two more unreadable answers to a dispatched effect are now an unknown outcome, not a plain error. A success (2xx) to an effectful hosted call that decodes as JSON but fits no response shape raises `HostedResponseUnreadableError` (exit 13, `ambiguous`, same idempotency key), as does a 2xx YouTube relay answer that cannot be read: the relay treats it like a broken connection, so the transfer backs off, probes the byte YouTube holds and resumes (or pauses the job to resume later) instead of failing terminally. Reads keep their plain error.
