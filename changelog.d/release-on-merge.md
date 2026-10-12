bump: patch
**Every merge to `main` is now a release.** The version is computed from the last tag and this merge's `changelog.d` fragments, so no one edits a version, tags or approves. The workflow verifies, builds, tags `v<version>` at the merge commit, publishes to PyPI through trusted publishing, smoke-tests the published package and creates this page. Other repositories call the same workflow, `.github/workflows/release-on-merge.yml`, pinned by commit.

**The `zeocore` command runs on a plain install.** In 0.15.0, `uv tool install zeocore` and `pip install zeocore` installed a command that crashed at start-up. `rfc8785` is now a base dependency, and `zeocore release` loads only when it is used, naming the `github` extra if that is missing.

**Every publish goes through the protected `pypi` environment.** The TestPyPI target is gone.

```bash
uv tool install zeocore==0.15.1      # the command
uv pip install "zeocore==0.15.1"     # the library
```
