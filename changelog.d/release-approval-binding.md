bump: patch

`zeocore release check-approval` now accepts an approval only when its status
description names this repository, pull request and head
(`apr1-<repository id>-<pull number>-<head>`), is dated no later than the merge,
and the pull request's base is this repository's `main`. A status that merely
exists on the head, or on another repository's pull request, is no longer enough.

sow: zeocore-SOW-44
