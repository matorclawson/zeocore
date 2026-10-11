# Check a zeocore release on PyPI

`zeocore release pypi` is the PyPI adapter for `pypi:zeocore`
(ZEOCORE-SOW-26). It changes nothing about how zeocore publishes:
`publish.yml` builds and publishes through trusted publishing when a `vX.Y.Z`
tag is pushed, and the operator approves each run in GitHub's `pypi`
environment. The adapter makes the steps around that approval explicit.

## The manifest

zeocore's `release.yaml`, changed in the release's own reviewed PR:

```yaml
pypi:
  project: zeocore
  version: X.Y.Z
  repository: zeroemployeeorg/zeocore
  commit: "<40 hex, on the default branch>"
  workflow: publish.yml
  environment: pypi
```

## The stages

```console
$ echo '{"manifest": "release.yaml"}' | zeocore release pypi plan
```

| Stage | Effect | Answers |
|---|---|---|
| `plan` | none | `planned`, with the tag it would create, once the commit is on the default branch, `pyproject.toml`, `release.yaml` and `CHANGELOG.md` name the version, PyPI hasn't got it, and the tag is absent or already at the commit |
| `admit` | none | `admitted` only when the `pypi` environment requires the operator's approval; otherwise `promotion_unbound` |
| `reconcile` | none | the tag, then its `publish.yml` run: `awaiting_operator_approval` (exit 11) until the operator approves, `publish_failed`, or `published` |
| `verify` | none | `publisher_recorded` when PyPI lists a wheel and an sdist, neither yanked, and each file's provenance names GitHub, the repository, `publish.yml` and `pypi` |

`verify` checks the publisher PyPI recorded for each attestation. It does
not verify the attestation's Sigstore signature yet, and says so in its
answer.

`apply`, which creates the tag, is the adapter's only effect. It isn't built
yet: it follows the design's review. The adapter never approves the
workflow, never dispatches it, and never holds a PyPI credential.

GitHub is read through `gh`, so no token passes through zeocore. PyPI is
read anonymously from `https://pypi.org`.
