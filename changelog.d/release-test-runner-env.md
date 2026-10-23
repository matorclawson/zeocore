bump: patch

The release-metadata tests no longer inherit the CI runner's `GITHUB_*` variables. One of them, the check that a credential in the git remote is refused, failed on GitHub's runners (which set `GITHUB_REPOSITORY`) and stopped the release of the previous merge. Tests only; no change to what a release does.
