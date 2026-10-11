# Check a zeocore release on PyPI

`zeocore release pypi` is the PyPI adapter for `pypi:zeocore`
(ZEOCORE-SOW-26). It changes nothing about how zeocore publishes:
`publish.yml` builds and publishes through trusted publishing when a `vX.Y.Z`
tag is pushed, and the operator approves each run in GitHub's `pypi`
environment. The adapter makes the steps around that approval explicit.

## The manifest

zeocore's `release.yaml` is changed in the release's own reviewed PR. A file
can't name the commit it's part of, so the committed section has no
`commit`:

```yaml
pypi:
  project: zeocore
  version: X.Y.Z
  repository: zeroemployeeorg/zeocore
  workflow: publish.yml
  environment: pypi
```

The stages read a local copy of the same section, with `commit` added: the
40-hex commit on the default branch that the release is cut from. `plan`
refuses unless every other field equals `release.yaml` at that commit
(`manifest_unreviewed`). The project, repository, workflow and environment
are also pinned in zeocore's source, so a manifest can't point the adapter at
another target.

## The stages

```console
$ echo '{"manifest": "release.yaml"}' | zeocore release pypi plan
```

| Stage | Effect | Answers |
|---|---|---|
| `plan` | none | `planned`, with the tag it would create, once the commit is on the default branch, `pyproject.toml`, `release.yaml` and `CHANGELOG.md` name the version, PyPI hasn't got it, and the tag is absent or already at the commit |
| `admit` | none | `admitted` only when the operator's approval is a control, not a convention (below) |
| `apply` | **one: creates the tag `vX.Y.Z` at the commit** | `tag_created`, or `already_applied`. It runs `plan` and `admit` again first, writes its intent, and never retries |
| `reconcile` | none | the tag, then its `publish.yml` run: `awaiting_operator_approval` (exit 11) until the operator approves, `publish_failed`, or `published` |
| `verify` | none | `publisher_recorded` when PyPI lists a wheel and an sdist, neither yanked, and each file's provenance names GitHub, the repository, `publish.yml` and `pypi` |

`verify` checks the publisher PyPI recorded for each attestation. It does
not verify the attestation's Sigstore signature yet, and says so in its
answer.

`verify` also checks that each attestation names exactly that file and its
sha256, so a correct publisher on other bytes is refused.

## What admit requires

The identity that creates the tag must not be able to approve or bypass the
publish. Otherwise "never approves" would be a convention, not a control.
`admit` refuses unless all of these hold, read through GitHub's own API:

- the operator is the `pypi` environment's only required reviewer, because
  any one listed reviewer can approve (`promotion_unbound`);
- the identity creating the tag is not the operator's, so it can't approve
  its own deployment (`creator_is_operator`);
- it can't bypass the protection as an admin: either the environment doesn't
  let admins bypass, or the identity isn't an admin (`admin_bypass_possible`);
- the environment deploys only from `v*` tags (`deployment_policy_unrestricted`).

## apply

```console
$ echo '{"manifest": "release.yaml", "work_dir": "/path/to/journal"}' | zeocore release pypi apply
```

`apply` is the adapter's only effect. It creates a lightweight tag through
GitHub's API, after `plan` and `admit` pass again at that moment. Its intent
is journaled before the call. A lost answer is `apply_unreconciled` (exit 13),
and is never retried: `reconcile` reads the tag back. The adapter never
approves the workflow, never dispatches it, and never holds a PyPI
credential.

GitHub is read through `gh`, so no token passes through zeocore. PyPI is
read anonymously from `https://pypi.org`.
