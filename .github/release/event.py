"""The release event: one JSON file naming what a merge released.

    python3 event.py TAG NOTES [ARTIFACT_DIR] > release-event.json

Run at the released commit, from the repository root. It reads the commit and
tree from git, hashes the notes and every file in ARTIFACT_DIR, and ends with
the sha256 of everything above it, so a consumer can tell an altered event from
the one the executor wrote. It does not sign anything: signatures come from
PyPI attestations for packages and from Pomar's signed build for runnables,
and are referenced here, never created here. The standard library only.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

SCHEMA = "zeo.release-event/v1"


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(tag: str, notes: Path, artifacts: Path | None) -> dict[str, object]:
    files = sorted(p for p in artifacts.rglob("*") if p.is_file()) if artifacts else []
    body: dict[str, object] = {
        "schema": SCHEMA,
        "repository": git("config", "--get", "remote.origin.url"),
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
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return {**body, "event_sha256": hashlib.sha256(canonical.encode()).hexdigest()}


if __name__ == "__main__":
    tag, notes = sys.argv[1], Path(sys.argv[2])
    artifacts = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    print(json.dumps(build(tag, notes, artifacts), indent=2, sort_keys=True))
