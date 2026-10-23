"""The release event: one JSON file naming what a merge released.

    python3 event.py TAG NOTES [ARTIFACT_DIR] [--repository NAME] [--detached DIR]

Run at the released commit, from the repository root. It reads the commit and
tree from git, hashes the notes and every file in ARTIFACT_DIR, and ends with
the sha256 of the canonical JSON of every other field (sorted keys, no spaces),
so a consumer can tell an altered event from the one the executor wrote. With
--detached DIR it also writes the canonical payload bytes and their digest as
two files, for a signer to sign and a verifier to recompute. It does not sign
anything: signatures come from PyPI attestations for packages and from
Pomar's signed build for runnables, and are referenced here, never created
here. The standard library only.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = "zeo.release-event/v1"


def git(*args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed git, arguments from this script
        ["git", *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def repository(argument: str | None) -> str:
    """The repository's name, never a credential: this goes in a published asset.

    From --repository, else GITHUB_REPOSITORY, else origin's URL. A value with
    userinfo (https://user:token@host/...) is refused, not cleaned.
    """
    value = argument or os.environ.get("GITHUB_REPOSITORY")
    if not value:
        value = git("config", "--get", "remote.origin.url")
    parts = urlsplit(value)
    if parts.username is not None or parts.password is not None:
        sys.exit("event.py: the repository value carries userinfo; refusing it")
    return value


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(
    tag: str, notes: Path, artifacts: Path | None, repo: str | None = None
) -> dict[str, object]:
    files = sorted(p for p in artifacts.rglob("*") if p.is_file()) if artifacts else []
    body: dict[str, object] = {
        "schema": SCHEMA,
        "repository": repository(repo),
        "tag": tag,
        "tag_object": git("rev-parse", f"refs/tags/{tag}"),
        "commit": git("rev-parse", "HEAD"),
        "tree": git("rev-parse", "HEAD^{tree}"),
        "notes_sha256": sha256(notes),
        "artifacts": [
            {
                "path": p.relative_to(artifacts).as_posix(),
                "size": p.stat().st_size,
                "sha256": sha256(p),
            }
            for p in files
        ]
        if artifacts
        else [],
    }
    return {**body, "event_sha256": hashlib.sha256(canonical(body)).hexdigest()}


def canonical(body: dict[str, object]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


if __name__ == "__main__":
    args = sys.argv[1:]
    detached = None
    named = None
    if "--detached" in args:
        at = args.index("--detached")
        detached = Path(args[at + 1])
        del args[at : at + 2]
    if "--repository" in args:
        at = args.index("--repository")
        named = args[at + 1]
        del args[at : at + 2]
    tag, notes = args[0], Path(args[1])
    artifacts = Path(args[2]) if len(args) > 2 else None
    event = build(tag, notes, artifacts, named)
    print(json.dumps(event, indent=2, sort_keys=True))
    if detached is not None:
        detached.mkdir(parents=True, exist_ok=True)
        payload = {k: v for k, v in event.items() if k != "event_sha256"}
        (detached / "release-event.payload.json").write_bytes(canonical(payload))
        (detached / "release-event.sha256").write_text(f"{event['event_sha256']}\n")
