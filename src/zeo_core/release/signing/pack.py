"""Content-addressed packs between a build host and the signing host (SOW-23 §2).

A request pack carries ``request.json``, the manifest and every file its
``signing`` section lists, and nothing else. Its sha256 is its request id.
A response pack carries the signed candidates and their sign receipts; the
build host runs ``verify`` on it itself. Both are plain tar files written
deterministically: sorted, with fixed owners, modes and times, so the same
inputs always give the same id.

Opening a pack refuses links, devices, absolute paths, ``..``, duplicates,
unlisted files and any digest that differs, before a byte is used.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from .manifest import LoadedManifest, ManifestError, load
from .step import WorkDir, sha256_file

PACK_VERSION: Final = 1
MAX_PACK_BYTES: Final = 512 * 1024 * 1024
REQUEST_FILE: Final = "request.json"
MANIFEST_FILE: Final = "release.yaml"

Repo = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
Commit = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
ManifestPath = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9_.][A-Za-z0-9_./-]{0,511}$")
]


class PackError(ValueError):
    def __init__(
        self, reason: Literal["request_invalid", "input_mismatch"], message: str
    ) -> None:
        super().__init__(message)
        self.reason = reason


class SigningRequest(BaseModel):
    """Where the manifest was reviewed: matormini reads it back from there."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pack_version: Literal[1]
    product: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
    repo: Repo
    commit: Commit
    manifest_path: ManifestPath


def _member(name: str, data: bytes, mode: int) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), mode, 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info, data


def _write(out: Path, members: Iterable[tuple[tarfile.TarInfo, bytes]]) -> str:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for info, data in sorted(members, key=lambda pair: pair[0].name):
            tar.addfile(info, io.BytesIO(data))
    content = buffer.getvalue()
    out.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def create_request(loaded: LoadedManifest, request: SigningRequest, out: Path) -> str:
    """Write the request pack and return its id. Inputs are checked first."""
    if request.product != loaded.signing.product:
        raise PackError("request_invalid", "the request names another product")
    members = [
        _member(REQUEST_FILE, request.model_dump_json().encode(), 0o644),
        _member(MANIFEST_FILE, loaded.manifest_path.read_bytes(), 0o644),
    ]
    for relative, expected, mode in _listed(loaded):
        path = loaded.resolve(relative)
        if not path.is_file() or sha256_file(path) != expected:
            raise PackError("input_mismatch", f"{relative} differs from the manifest")
        members.append(_member(relative, path.read_bytes(), mode))
    return _write(out, members)


def _listed(loaded: LoadedManifest) -> list[tuple[str, str, int]]:
    files = []
    for item in loaded.signing.executables:
        files.append((item.path, item.unsigned_sha256, 0o755))
        if item.entitlements != "none":
            files.append((item.entitlements.path, item.entitlements.sha256, 0o644))
    return files


def _extract(pack: Path, dest: Path) -> dict[str, Path]:
    """Every regular file in the pack, written under ``dest``, by its name."""
    if not pack.is_file() or pack.stat().st_size > MAX_PACK_BYTES:
        raise PackError("request_invalid", "the pack is missing or too large")
    written: dict[str, Path] = {}
    try:
        with tarfile.open(pack, mode="r:") as tar:
            for info in tar:
                name = info.name
                parts = PurePosixPath(name).parts
                if (
                    not info.isreg()
                    or not parts
                    or name.startswith("/")
                    or ".." in parts
                    or "\\" in name
                    or name in written
                ):
                    raise PackError("request_invalid", "the pack has an unsafe member")
                target = dest.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(info)
                if source is None:
                    raise PackError("request_invalid", "a pack member has no content")
                target.write_bytes(source.read())
                target.chmod(0o755 if info.mode & 0o100 else 0o644)
                written[name] = target
    except tarfile.TarError:
        raise PackError("request_invalid", "the pack is not a tar file") from None
    return written


@dataclass(frozen=True)
class OpenedRequest:
    request_id: str
    request: SigningRequest
    manifest: LoadedManifest
    manifest_bytes: bytes


def open_request(pack: Path, dest: Path) -> OpenedRequest:
    """Unpack into an empty ``dest`` and check it against its own manifest."""
    dest.mkdir(mode=0o700, parents=True, exist_ok=False)
    written = _extract(pack, dest)
    if REQUEST_FILE not in written or MANIFEST_FILE not in written:
        raise PackError("request_invalid", "the pack has no request or manifest")
    try:
        request = SigningRequest.model_validate_json(written[REQUEST_FILE].read_bytes())
        loaded = load(written[MANIFEST_FILE])
    except (ValidationError, ManifestError) as error:
        raise PackError("request_invalid", f"the request is invalid: {error}") from None
    if request.product != loaded.signing.product:
        raise PackError("request_invalid", "the request names another product")
    listed = {relative: expected for relative, expected, _ in _listed(loaded)}
    unlisted = set(written) - set(listed) - {REQUEST_FILE, MANIFEST_FILE}
    if unlisted:
        raise PackError(
            "input_mismatch", "the pack has a file its manifest doesn't list"
        )
    for relative, expected in listed.items():
        if relative not in written or sha256_file(written[relative]) != expected:
            raise PackError("input_mismatch", f"{relative} differs from the manifest")
    return OpenedRequest(
        request_id=sha256_file(pack),
        request=request,
        manifest=loaded,
        manifest_bytes=written[MANIFEST_FILE].read_bytes(),
    )


def create_response(loaded: LoadedManifest, work: WorkDir, out: Path) -> str:
    """The signed candidates and their sign receipts, for the build host."""
    members = []
    for item in loaded.signing.executables:
        candidate, receipt = work.candidate(item.name), work.receipt_path(item.name)
        if not candidate.is_file() or not receipt.is_file():
            raise PackError("input_mismatch", f"{item.name} has not been signed")
        members.append(_member(f"signed/{item.name}", candidate.read_bytes(), 0o755))
        members.append(
            _member(f"receipts/sign-{item.name}.json", receipt.read_bytes(), 0o644)
        )
    return _write(out, members)


def open_response(loaded: LoadedManifest, pack: Path, dest: Path) -> WorkDir:
    """Unpack a response into an empty work directory, ready for ``verify``."""
    dest.mkdir(mode=0o700, parents=True, exist_ok=False)
    written = _extract(pack, dest)
    expected = {
        name
        for item in loaded.signing.executables
        for name in (f"signed/{item.name}", f"receipts/sign-{item.name}.json")
    }
    if set(written) != expected:
        raise PackError("input_mismatch", "the response doesn't match the manifest")
    for item in loaded.signing.executables:
        try:
            json.loads(written[f"receipts/sign-{item.name}.json"].read_text())
        except ValueError:
            raise PackError("request_invalid", "a receipt is not JSON") from None
    return WorkDir(dest)
