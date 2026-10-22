bump: patch

`run_native` takes `spend_limit_cents`: a hard limit for the managed session in USD cents,
sent as the SDK's `spend_control` and enforced by the provider. Left out, the argument
is not sent at all (an explicit null would remove a limit a saved template carries). A
value that is not a whole number of cents from 1 to 1,000,000 (a bool included) is
refused with `spend_limit_invalid` before any call is made.
