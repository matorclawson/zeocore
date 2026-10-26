bump: patch

`zeocore release check-approval` now parses the approval's date and the merge time
as RFC 3339 UTC times (`YYYY-MM-DDTHH:MM:SSZ`) instead of comparing text, and
refuses an empty, offset, fractional or impossible date. It also requires the
pull request's base to be `main` of this repository by branch name and by the
repository's numeric id, not only by name.

sow: zeocore-SOW-44
