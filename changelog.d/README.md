# changelog.d: one fragment per PR

Every merge to `main` is a release (`.github/workflows/release-on-merge.yml`).
Its version is computed, never edited: the highest `v` tag plus one increment.
A PR declares the increment by adding one fragment here, so the level is part of
what review approves:

```markdown
bump: patch
What changed, for the release notes, in a sentence or a short list.
```

- **The first line** is `bump: patch`, `bump: minor` or `bump: major`, and nothing else.
- **The rest** is the release notes text. The GitHub Release for that version
  shows it, followed by the list of merged PRs.
- **Name it after your branch**, for example `changelog.d/cli-base-install.md`.
- **Optional:** a PR with no fragment is a patch release, with a one-line note.
- **Never edit or delete a merged fragment.** The release reads only the fragments
  its merge *added*, so old ones stay as history and are never counted again, and
  a merge that changes or deletes one fails. This README is the only exception.

From 0.15.1 on, the GitHub Releases page is the changelog. `CHANGELOG.md` keeps
the history up to 0.15.1.
