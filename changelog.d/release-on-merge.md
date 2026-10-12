bump: patch
**Every merge to `main` is now a release.** The version is computed from the last tag, the newest version on PyPI and the `changelog.d` fragments added since that tag, so no one edits a version, tags or approves. The workflow verifies, builds, tags `v<version>` at the merge commit, publishes to PyPI through trusted publishing, smoke-tests the published package and creates this page. Other repositories call the same workflow, `.github/workflows/release-on-merge.yml`, pinned by commit.

**0.15.1 has no tag.** It was published from `e22a16ca` by Publish run 38201406179, a manual dispatch on 2026-10-12, before release on merge existed. Its files are on PyPI with GitHub attestations for `zeroemployeeorg/zeocore`, `publish.yml` and `pypi`. Its notes are in `CHANGELOG.md`. This release, 0.15.2, counts from it.

The plain-install fix for the `zeocore` command shipped in 0.15.1.

```bash
uv tool install zeocore==0.15.2      # the command
uv pip install "zeocore==0.15.2"     # the library
```
