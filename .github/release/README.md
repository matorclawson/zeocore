# The shared release-on-merge component

Every merge to a golden `main` is a release of exactly that commit (RULING-471
amendment 1). This directory holds the parts of that component that run outside
GitHub Actions or are read by a consumer.

## Reserved paths (the executor's definition)

A change to any of these is a change to the executor, which the operator may
veto. A review must name it as such.

- `.github/workflows/release-on-merge.yml` (the reusable workflow)
- `.github/workflows/publish.yml` (zeocore's own caller)
- `.github/release/plan.py` (the version computation)
- `.github/release/event.py` (the release event)
- `release.yaml` (a repository's release file: facts, no version, no commit)

`tests/test_release_metadata.py` fails if one of these is missing or if the
inline copy of `plan.py` in the workflow differs from the file.

## Options

The workflow takes `project`, `verify`, `build`, `tag_prefix` (default `v`;
the engine uses `engine/v`) and `index` (default PyPI's JSON API; `none` for a
repository with no package). It takes no version, commit or ref, and its only
trigger is `workflow_call`; a caller's only trigger is a push to `main`.

## Runners

- **Public gold** runs `release-on-merge.yml` from the repository's own Actions.
- **Private gold** runs `plan.py` as it stands, from the same pinned commit of
  this repository, on Pomar: `TAG_PREFIX=engine/v INDEX=none python3 plan.py notes.md`
  from the repository root at the merged commit. It prints `version=`, `floor=`
  and `tag=` lines and writes the release notes to `notes.md`. It creates no tag.

## The release event

`event.py TAG NOTES [ARTIFACT_DIR]` prints one JSON document, schema
`zeo.release-event/v1`: `repository`, `tag`, `tag_object`, `commit`, `tree`,
`notes_sha256`, `artifacts` (each `path`, `size`, `sha256`) and `event_sha256`,
the sha256 of the canonical JSON of everything else. It signs nothing. A
consumer checks the signature of the artifacts it names (PyPI attestations for
a package, Pomar's signed build for a runnable) and refuses an event whose
`event_sha256` does not match, whose commit is not on `main`, or whose artifact
digests do not match the bytes it received.
