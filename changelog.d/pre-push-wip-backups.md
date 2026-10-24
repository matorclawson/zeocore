bump: patch

The pre-push hook no longer runs the gate for a push that only goes to `refs/heads/<seat>/wip/<topic>` branches, so work in progress can be backed up before it is green. Every other branch name, including a push that mixes a backup with another branch, is still gated, and the full gate must pass on the exact head before a pull request opens.
