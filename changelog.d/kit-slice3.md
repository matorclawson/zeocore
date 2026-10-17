bump: patch

Kit slice 3: `update`, `rollback` and `recover` as one state machine over app hooks: fetch, preflight, install beside the current version, one atomic symlink switch, verify by behaviour, and a durable intent record so a crash or a failed switch-back leaves `recovery_required` until `recover` proves a version running.
