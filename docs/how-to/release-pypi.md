# Release to PyPI

`zeocore release pypi` is the PyPI adapter (ZEOCORE-SOW-26). It changes
nothing about how a package publishes: the repository's `publish.yml` builds
and publishes through trusted publishing when a `vX.Y.Z` tag is pushed,
behind its `pypi` environment's approval. The adapter makes every step around
that approval explicit, receipted and reconcilable.

## Two identities, delegated by the operator

The operator delegated releases on 2026-10-11:

- the **`zeo-release` GitHub App** creates the `v*` tag. Its private key is in
  the signing host's Keychain as `zeo-release-app-key`, and it is read only to
  sign the App's own short-lived JWT. Each tag is made with a fresh
  installation token for that one repository, with `contents: write`, which is
  revoked as soon as the call is answered;
- **matorclawson** approves the `pypi` deployment, as its only required
  reviewer.

Neither identity can release alone. Configure the App's numeric id (not a
secret) with `ZEOCORE_RELEASE_APP_ID`; set
`ZEOCORE_RELEASE_APP_KEY_ACCOUNT` if the Keychain item's account isn't
`private-key`.

## The targets

Each target is a row in zeocore's reviewed table
(`zeo_core.release.pypi.targets.TARGETS`): `zeocore`, `zeocreator`,
`zero-employee` and `sovereign-agent`. Each row gives the repository, the
workflow and the environment. A manifest can name only a row in that table.

## The manifest

Each target's `release.yaml`, changed in the release's own reviewed PR. A
file can't name the commit it's part of, so the committed section has no
`commit`:

```yaml
pypi:
  project: zeocore
  version: X.Y.Z
  repository: zeroemployeeorg/zeocore
  workflow: publish.yml
  environment: pypi
```

The stages read a local copy of that section, with `commit` added: the
40-hex commit on the default branch that the release is cut from. `plan`
refuses unless every other field equals `release.yaml` at that commit
(`manifest_unreviewed`).

## The stages

```console
$ echo '{"manifest": "release.yaml"}' | zeocore release pypi plan
```

| Stage | Effect | Answers |
|---|---|---|
| `plan` | none | `planned`, with the tag it would create, once the commit is on the default branch, the target's workflow file is at that commit (`workflow_missing` otherwise: a `v*` tag can't be deleted under the ruleset, so it is never made with nothing to publish it), `pyproject.toml`, `release.yaml` and `CHANGELOG.md` name the version, PyPI hasn't got it, and the tag is absent or already at the commit |
| `admit` | none | `admitted` only when the two identities, together, are the only way to release (below) |
| `apply` | **one: creates the tag `vX.Y.Z`, as the App** | `tag_created`, or `already_applied` |
| `approve` | **one: approves the waiting `pypi` deployment, as matorclawson, once** | `approved`, or `already_approved` |
| `reconcile` | none | the tag, then its run: `awaiting_approval` (exit 11), `publish_failed`, or `published` |
| `verify` | none | `publisher_recorded` when PyPI lists a wheel and an sdist, neither yanked, and each file's attestation names the target's publisher and exactly that file and digest |

`verify` checks the publisher PyPI recorded for each attestation, not its
Sigstore signature, and says so in its answer.

## What admit requires

Read through GitHub's own API; anything else is refused:

- matorclawson is the `pypi` environment's only required reviewer
  (`promotion_unbound`), with self-review prevented (`self_review_allowed`)
  and no admin bypass (`admin_bypass_possible`);
- the environment deploys only from `v*` tags
  (`deployment_policy_unrestricted`). Every page of the policy list is read,
  and a list read short of GitHub's count is unreadable;
- an active ruleset on `refs/tags/v*`, with no exclusions, restricts creation,
  update and deletion, and only the release App bypasses it
  (`tag_ruleset_missing`);
- `gh` acts as matorclawson, the approver (`approver_mismatch`), with the
  **Admin** role on the repository: GitHub shows a ruleset's bypass list only
  to an Admin, and a hidden list is held as `target_unreadable`, never read as
  empty. A private repository under a personal account can't give a
  collaborator Admin, so it can't be delegated this way;
- the release App's id is configured (`release_app_unconfigured`).

## apply and approve

Both journal in this user's `$XDG_STATE_HOME/zeocore/release/pypi-journal.jsonl`
(by default under `~/.local/state`), keyed by repository and version. So one
release is tagged once and approved once, whatever the commit or directory.

- `apply` settles an earlier attempt first, by reading the tag back, before
  `plan` or `admit` could hide it. It then runs both again, checks that the
  Keychain key signs for `zeo-release` itself (`creator_not_release_app`),
  writes its intent, and makes one `POST /git/refs`. A lost answer is
  `apply_unreconciled` (exit 13), and is never retried.
- `approve` approves only the one run of that exact tag and commit, waiting
  on `pypi` alone, after `plan` and `admit` pass again. Anything else is
  `approval_unexpected`. A run that has stopped waiting is `run_not_waiting`,
  held, never a success. A lost answer is `approve_unreconciled`, and is never
  sent again. It is settled only by reading GitHub's own record of the run's
  reviews: matorclawson's approval of `pypi` closes it, and a rejection closes
  it as `approval_rejected`.

The adapter never holds a PyPI credential and never dispatches a workflow.
GitHub reads go through `gh`, and PyPI is read anonymously from
`https://pypi.org`.
