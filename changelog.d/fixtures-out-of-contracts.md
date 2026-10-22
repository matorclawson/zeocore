bump: patch

`contracts/` now holds only zeocore's own published schemas (`contracts/zeocore-v1/`). The three
interoperability fixture sets that were vendored there (`broker-1.4`, `merger-v1`, `runtime-host-v1`) are
test data, so they moved to `tests/fixtures/`. Their repository names are example names, and each
`SOURCE.txt` is one neutral line. No installed code changes. The Broker 1.4.0 digest vectors changed
because the repository name is part of the digested request: `github.pr.open` is now
`770f24c5918c…` (455 canonical bytes); anything that vendored the old vectors should refresh them.
