bump: patch

Classify the exact absent-default-keychain refusal in proof tests without
hiding other failures. Ordinary runs report the unavailable proof as a skip;
designated proof hosts still fail. Add empty-output and diagnostic-channel
regressions and document verbose skip evidence for qualification.
