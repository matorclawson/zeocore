"""Content-addressed packs between a build host and the signing host (SOW-23 §2).

A request pack carries ``request.json``, the manifest and every file its
``signing`` section lists, and nothing else. Its sha256 is its request id.
A response pack carries the signed candidates and their sign receipts; the
build host runs ``verify`` on it itself. Both are plain tar files written
deterministically: sorted, with fixed owners, modes and times, so the same
inputs always give the same id.

A bundle travels whole, as its tree: directories, files and relative links
(ZEOCORE-SOW-32 §1.3). Opening a pack refuses devices, absolute paths, ``..``,
duplicates, unlisted files, any digest that differs, and any link or directory
outside a listed bundle, before a byte is used. A link inside a bundle must be
relative and resolve inside that bundle; nothing is written through one.
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

from .manifest import Bundle, LoadedManifest, ManifestError, load
from .step import WorkDir, sha256_file
from .tree import TreeError, contained_target, listing, tree_sha256
from .tree import digest as tree_digest

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


def _entry_member(
    name: str, kind: str, root: Path, relative: str, target: str
) -> tuple[tarfile.TarInfo, bytes]:
    if kind == "file":
        path = root / relative
        return _member(name, path.read_bytes(), 0o755 if _executable(path) else 0o644)
    info, data = _member(name, b"", 0o755)
    info.type = tarfile.DIRTYPE if kind == "dir" else tarfile.SYMTYPE
    if kind == "symlink":
        info.linkname = target
    return info, data


def _executable(path: Path) -> bool:
    return bool(path.stat().st_mode & 0o111)


def _tree_members(prefix: str, root: Path) -> list[tuple[tarfile.TarInfo, bytes]]:
    """A bundle's whole tree under ``prefix``, as its listing describes it."""
    entries = listing(root)
    return [
        _dir(prefix),
        *(
            _entry_member(
                f"{prefix}/{entry.path}", entry.type, root, entry.path, entry.value
            )
            for entry in entries
        ),
    ]


def _dir(name: str) -> tuple[tarfile.TarInfo, bytes]:
    info, data = _member(name, b"", 0o755)
    info.type = tarfile.DIRTYPE
    return info, data


def _write(out: Path, members: Iterable[tuple[tarfile.TarInfo, bytes]]) -> str:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for info, data in sorted(members, key=lambda pair: pair[0].name):
            tar.addfile(info, io.BytesIO(data))
    content = buffer.getvalue()
    if len(content) > MAX_PACK_BYTES:
        raise PackError("request_invalid", "the pack is larger than 512 MiB")
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
    for bundle in loaded.signing.bundles:
        root = loaded.resolve(bundle.path)
        try:
            same = tree_digest(listing(root)) == bundle.unsigned_tree_sha256
            if same:
                members.extend(_tree_members(bundle.path, root))
        except (TreeError, OSError) as error:
            raise PackError("input_mismatch", f"{bundle.path}: {error}") from None
        if not same:
            raise PackError(
                "input_mismatch", f"{bundle.path} differs from the manifest"
            )
    return _write(out, members)


def _listed(loaded: LoadedManifest) -> list[tuple[str, str, int]]:
    """Every listed regular file outside a bundle's tree, with its digest."""
    files = []
    for item in loaded.signing.executables:
        files.append((item.path, item.unsigned_sha256, 0o755))
        if item.entitlements != "none":
            files.append((item.entitlements.path, item.entitlements.sha256, 0o644))
    for bundle in loaded.signing.bundles:
        for pinned in (bundle.entitlements, *(n.entitlements for n in bundle.nested)):
            if pinned != "none":
                files.append((pinned.path, pinned.sha256, 0o644))
    return sorted(set(files))


def _inside(name: str, root: str) -> bool:
    parts, top = PurePosixPath(name).parts, PurePosixPath(root).parts
    return parts[: len(top)] == top


@dataclass(frozen=True)
class _Extracted:
    files: dict[str, Path]  # every regular file, by its name
    trees: frozenset[str]  # every directory and link, by its name


def _unsafe(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return (
        not parts
        or name.startswith("/")
        or ".." in parts
        or "\\" in name
        or "\0" in name
    )


def _extract(pack: Path, dest: Path) -> _Extracted:
    """Every member in the pack, written under ``dest``, links last."""
    if not pack.is_file() or pack.stat().st_size > MAX_PACK_BYTES:
        raise PackError("request_invalid", "the pack is missing or too large")
    try:
        with tarfile.open(pack, mode="r:") as tar:
            return _write_members(tar, dest)
    except tarfile.TarError:
        raise PackError("request_invalid", "the pack is not a tar file") from None
    except OSError:
        raise PackError("request_invalid", "the pack has an unsafe member") from None


def _write_members(tar: tarfile.TarFile, dest: Path) -> _Extracted:
    members = tar.getmembers()
    names = [info.name for info in members]
    links = {info.name: info.linkname for info in members if info.issym()}
    if len(set(names)) != len(names) or any(
        _unsafe(name)
        or not (info.isreg() or info.isdir() or info.issym())
        or any(_inside(name, link) and name != link for link in links)
        for info, name in zip(members, names, strict=True)
    ):
        raise PackError("request_invalid", "the pack has an unsafe member")
    if not all(contained_target(name, target) for name, target in links.items()):
        raise PackError(
            "request_invalid", "the pack has an unsafe member: a link leaves it"
        )
    written: dict[str, Path] = {}
    for info in members:
        target = dest.joinpath(*PurePosixPath(info.name).parts)
        if info.isdir():
            target.mkdir(mode=0o755, parents=True, exist_ok=True)
            continue
        if not info.isreg():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source = tar.extractfile(info)
        if source is None:
            raise PackError("request_invalid", "a pack member has no content")
        target.write_bytes(source.read())
        target.chmod(0o755 if info.mode & 0o111 else 0o644)
        written[info.name] = target
    # Every file is on disk before the first link, so none is written through one.
    for name, link in sorted(links.items()):
        target = dest.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(link)
    trees = {info.name for info in members if info.isdir() or info.issym()}
    return _Extracted(written, frozenset(trees))


def _bundle_trees_match(
    loaded: LoadedManifest, extracted: _Extracted, dest: Path
) -> None:
    """Links and directories only inside a listed bundle, whose tree matches."""
    roots = [bundle.path for bundle in loaded.signing.bundles]
    if any(not any(_inside(name, root) for root in roots) for name in extracted.trees):
        raise PackError(
            "input_mismatch", "the pack has a tree its manifest doesn't list"
        )
    for bundle in loaded.signing.bundles:
        try:
            same = tree_sha256(dest / bundle.path) == bundle.unsigned_tree_sha256
        except TreeError, OSError:
            same = False
        if not same:
            raise PackError(
                "input_mismatch", f"{bundle.path} differs from the manifest"
            )


@dataclass(frozen=True)
class OpenedRequest:
    request_id: str
    request: SigningRequest
    manifest: LoadedManifest
    manifest_bytes: bytes


def open_request(pack: Path, dest: Path) -> OpenedRequest:
    """Unpack into an empty ``dest`` and check it against its own manifest."""
    dest.mkdir(mode=0o700, parents=True, exist_ok=False)
    extracted = _extract(pack, dest)
    written = extracted.files
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
    roots = [bundle.path for bundle in loaded.signing.bundles]
    unlisted = {
        name
        for name in set(written) - set(listed) - {REQUEST_FILE, MANIFEST_FILE}
        if not any(_inside(name, root) for root in roots)
    }
    if unlisted:
        raise PackError(
            "input_mismatch", "the pack has a file its manifest doesn't list"
        )
    for relative, expected in listed.items():
        if relative not in written or sha256_file(written[relative]) != expected:
            raise PackError("input_mismatch", f"{relative} differs from the manifest")
    _bundle_trees_match(loaded, extracted, dest)
    return OpenedRequest(
        request_id=sha256_file(pack),
        request=request,
        manifest=loaded,
        manifest_bytes=written[MANIFEST_FILE].read_bytes(),
    )


def _receipt_names(loaded: LoadedManifest) -> list[str]:
    return [
        *(item.name for item in loaded.signing.executables),
        *(
            name
            for bundle in loaded.signing.bundles
            for name in (*(item.name for item in bundle.nested), bundle.name)
        ),
    ]


def _signed_root(bundle: Bundle) -> str:
    return f"signed/{bundle.name}/{PurePosixPath(bundle.path).name}"


def create_response(loaded: LoadedManifest, work: WorkDir, out: Path) -> str:
    """The signed candidates and their sign receipts, for the build host."""
    members = []
    for item in loaded.signing.executables:
        candidate = work.candidate(item.name)
        if not candidate.is_file():
            raise PackError("input_mismatch", f"{item.name} has not been signed")
        members.append(_member(f"signed/{item.name}", candidate.read_bytes(), 0o755))
    for bundle in loaded.signing.bundles:
        try:
            members.extend(
                _tree_members(_signed_root(bundle), work.bundle_candidate(bundle))
            )
        except TreeError, OSError:
            raise PackError(
                "input_mismatch", f"{bundle.name} has not been signed"
            ) from None
    for name in _receipt_names(loaded):
        receipt = work.receipt_path(name)
        if not receipt.is_file():
            raise PackError("input_mismatch", f"{name} has not been signed")
        members.append(
            _member(f"receipts/sign-{name}.json", receipt.read_bytes(), 0o644)
        )
    return _write(out, members)


def open_response(loaded: LoadedManifest, pack: Path, dest: Path) -> WorkDir:
    """Unpack a response into an empty work directory, ready for ``verify``.

    A bundle's signed tree is checked by ``verify``, against its receipt.
    """
    dest.mkdir(mode=0o700, parents=True, exist_ok=False)
    extracted = _extract(pack, dest)
    receipts = {f"receipts/sign-{name}.json" for name in _receipt_names(loaded)}
    expected = receipts | {f"signed/{item.name}" for item in loaded.signing.executables}
    roots = [_signed_root(bundle) for bundle in loaded.signing.bundles]
    files = {
        name
        for name in extracted.files
        if not any(_inside(name, root) for root in roots)
    }
    trees_outside = any(
        not any(_inside(name, root) for root in roots)
        and name not in {f"signed/{b.name}" for b in loaded.signing.bundles}
        for name in extracted.trees
    )
    if files != expected or trees_outside:
        raise PackError("input_mismatch", "the response doesn't match the manifest")
    for name in receipts:
        try:
            json.loads(extracted.files[name].read_text())
        except ValueError:
            raise PackError("request_invalid", "a receipt is not JSON") from None
    return WorkDir(dest)
