"""A bundle's canonical tree and its digest (ZEOCORE-SOW-32 §1.2, §1.3).

One line per entry below the bundle's root, sorted by path: the path, its
type (``file``, ``dir`` or ``symlink``), its mode, and the file's sha256 or the
link's target, separated by tabs. ``unsigned_tree_sha256`` and a receipt's
``signed_tree_sha256`` are the sha256 of those lines. The build's receipt, the
request pack and the signing step all compute it here, so they can't differ.

The mode is the one a pack carries: ``755`` for a directory or a file with
any execute bit, ``644`` for any other file, ``-`` for a link, whose own
mode means nothing. A tree with anything else, a device, a socket, a link that
is absolute or leaves the bundle, is refused rather than described.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Literal

EntryType = Literal["file", "dir", "symlink"]
EXECUTABLE_MODE: Final = 0o755
DATA_MODE: Final = 0o644


class TreeError(ValueError):
    """The tree can't be described: an unsafe link or an unsupported entry."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class Entry:
    path: str  # POSIX, relative to the bundle's root
    type: EntryType
    mode: int | None  # None for a link
    value: str  # a file's sha256, a link's target, "-" for a directory

    def line(self) -> str:
        mode = "-" if self.mode is None else f"{self.mode:o}"
        return f"{self.path}\t{self.type}\t{mode}\t{self.value}\n"


def file_mode(st_mode: int) -> int:
    return EXECUTABLE_MODE if st_mode & 0o111 else DATA_MODE


def contained_target(link: str, target: str) -> bool:
    """Whether a link at ``link`` naming ``target`` stays inside its root.

    Lexical only: a relative target whose path, joined to the link's
    directory, never climbs above the root. ``listing`` checks the resolved
    path as well, because a link through another link can climb further.
    """
    if not target or target.startswith("/") or "\\" in target or "\0" in target:
        return False
    depth = len(PurePosixPath(link).parts) - 1
    for part in PurePosixPath(target).parts:
        if part == "..":
            depth -= 1
            if depth < 0:
                return False
        elif part != ".":
            depth += 1
    return True


def _entry(root: Path, real_root: Path, path: Path) -> Entry:
    relative = path.relative_to(root).as_posix()
    if "\t" in relative or "\n" in relative or "\\" in relative:
        raise TreeError("a bundle path can't hold a tab, newline or backslash")
    info = path.lstat()
    if stat.S_ISDIR(info.st_mode):
        return Entry(relative, "dir", EXECUTABLE_MODE, "-")
    if stat.S_ISREG(info.st_mode):
        return Entry(relative, "file", file_mode(info.st_mode), sha256_file(path))
    if not stat.S_ISLNK(info.st_mode):
        raise TreeError(f"{relative}: only files, directories and links")
    target = os.readlink(path)
    if "\t" in target or "\n" in target or not contained_target(relative, target):
        raise TreeError(f"{relative}: a link must stay inside the bundle")
    try:
        resolved = path.resolve(strict=True)
    except OSError, RuntimeError:
        raise TreeError(f"{relative}: the link resolves to nothing") from None
    if not resolved.is_relative_to(real_root):
        raise TreeError(f"{relative}: a link must stay inside the bundle")
    return Entry(relative, "symlink", None, target)


def listing(root: Path) -> tuple[Entry, ...]:
    """Every entry below ``root``, sorted by path, never following a link."""
    try:
        top = root.lstat()
    except OSError:
        raise TreeError("the bundle is missing") from None
    if not stat.S_ISDIR(top.st_mode):
        raise TreeError("the bundle is not a directory")
    real_root = root.resolve()
    entries = [
        _entry(root, real_root, Path(directory) / name)
        for directory, dirs, files in os.walk(root, followlinks=False)
        for name in [*dirs, *files]
    ]
    return tuple(sorted(entries, key=lambda entry: entry.path))


def digest(entries: tuple[Entry, ...]) -> str:
    content = "".join(entry.line() for entry in entries).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def tree_sha256(root: Path) -> str:
    """The canonical tree digest of the bundle at ``root``."""
    return digest(listing(root))
