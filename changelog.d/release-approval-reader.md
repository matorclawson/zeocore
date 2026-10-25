bump: minor

Add `zeocore release check-approval`, the real reader for the release-approval check: given a release commit it finds the one distinct pull request (by number, over every page of GitHub's list) merged as that commit (its `merge_commit_sha` must equal the commit and the base must be the default branch), reads the council's `council/approve` status on that pull request's head through `gh api`, and answers `approved` only for a success created by the adopted approver. A missing, ambiguous or failed read is `refused`, never approval. This is separate from `verify-event`: neither implies the other. `MergeReads` no longer extends `EvidenceReads`; it names the four reads the check uses.

sow: zeocore-SOW-44
