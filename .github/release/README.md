# The shared release-on-merge component

Every merge to a golden `main` is a release of exactly that commit (RULING-471
amendment 1, section 3a). This note says which files are the executor, what a
repository passes to it, and how a repository uses it unchanged.

## Reserved paths (the executor's definition)

A change to any of these is a change to the executor, which the operator may
veto (section 3c). A review must name it as such.

- `.github/workflows/release-on-merge.yml` (the reusable workflow)
- `.github/workflows/publish.yml` (zeocore's own caller)
- `release.yaml` (a repository's release file: facts, no version, no commit)

`tests/test_release_metadata.py` fails if one of these is missing or not listed
here.

## What the component takes

`project`, `verify` and `build`. It takes no version, commit or ref, and its
only trigger is `workflow_call`. A caller's only trigger is a push to `main`;
no release, tag or upload can be started by hand. The version is computed from
the last tag, the package index and the `changelog.d` fragments added since.

## Using it unchanged

A repository's own `publish.yml` calls the workflow at a pinned commit of this
repository, on every push to `main`:

```yaml
jobs:
  release:
    uses: zeroemployeeorg/zeocore/.github/workflows/release-on-merge.yml@<40-hex sha>
    permissions: {contents: write}
    with: {project: <distribution name>, verify: <shell>}
```

Its upload job runs in the caller, in an environment named `pypi` that deploys
from `main` only, with PyPI trusted publishing. PyPI matches the top-level
workflow, so the trusted publisher names the caller (`publish.yml`), not the
reusable file: the component's "shared workflow" is met by the caller calling
it unchanged. No token and no signing key is stored as an Actions secret.
