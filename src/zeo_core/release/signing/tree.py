"""A bundle's canonical tree and its digest (ZEOCORE-SOW-32 §1.2, §1.3).

The listing is one line per entry below the bundle's root, sorted by path as
UTF-8 bytes. Each line is ``path TAB type TAB mode TAB value LF``:

- ``path``: POSIX, relative to the root, UTF-8, never escaped. A path or link
  target with a control character (U+0000 to U+001F, U+007F), a backslash, or
  bytes that aren't UTF-8 is refused, so nothing needs escaping.
- ``type``: ``file``, ``dir`` or ``symlink``. Anything else (a fifo, socket
  or device) is refused, and so is a regular file with more than one hard
  link, an entry with the setuid, setgid or sticky bit, and one with any BSD
  file flag but transparent compression.
- ``mode``: the one a pack carries, in octal: ``755`` for a directory or a
  file with any execute bit, ``644`` for any other file, ``-`` for a link.
- ``value``: a file's lowercase hex sha256, a link's target as written, or
  ``-`` for a directory.

The digest is the sha256 of the lines concatenated. Extended attributes,
owners and times are not part of it: xattrs are neither described nor sealed
here (codesign seals the resources it covers), and the signing copy doesn't
carry them. ``unsigned_tree_sha256`` and a
receipt's ``signed_tree_sha256`` are this digest; the build's receipt, the
request pack and the signing step all compute it here, so they can't differ.

**Links** (§1.3) are allowed only where a framework needs them: its
``Versions/Current``, naming one version directory beside it, and a link
directly inside the framework whose target starts ``Versions/``. Every hop
of a link's resolution, through any link it passes, stays inside the
nearest enclosing ``.framework``; an absolute target, a ``..`` that leaves
it, a dangling link or a loop is refused. Nothing is ever followed while
walking.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Literal

EntryType = Literal["file", "dir", "symlink"]
EXECUTABLE_MODE: Final = 0o755
DATA_MODE: Final = 0o644


#: How many links one resolution may pass through before it is a loop.
MAX_HOPS: Final = 40
#: The one BSD file flag allowed: transparent compression, which changes how
#: a file is stored and never what it reads. Any other (immutable, append-only,
#: hidden, …) refuses the entry.
ALLOWED_FLAGS: Final = getattr(stat, "UF_COMPRESSED", 0)


class TreeError(ValueError):
    """The tree can't be described: an unsafe link or an unsupported entry."""


def _plain(text: str) -> bool:
    """No control character, no backslash, and valid UTF-8."""
    if "\\" in text or any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def copy_plain(source: str, destination: str) -> None:
    """Copy one file's bytes, with only the mode a pack carries.

    For ``shutil.copytree``: no setuid bit, flag or extended attribute of the
    original reaches the copy that is signed.
    """
    shutil.copyfile(source, destination, follow_symlinks=False)
    os.chmod(destination, file_mode(os.lstat(source).st_mode))


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
    directory, never climbs above the root. ``listing`` also resolves every
    hop inside the link's framework, because a link through another link can
    climb further.
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


def _framework(parts: tuple[str, ...]) -> int | None:
    """The index in ``parts`` of the nearest enclosing ``.framework``."""
    for index in range(len(parts) - 2, -1, -1):
        if parts[index].endswith(".framework"):
            return index
    return None


def _resolves_inside(top: Path, parts: tuple[str, ...], scope: int) -> bool:
    """Whether the link at ``parts`` resolves, hop by hop, inside ``scope``.

    ``top`` is the directory ``parts`` are relative to, and ``parts[:scope + 1]``
    the framework every hop must stay in. Each component is checked as it is
    reached, so a link through a link can't step out and back in.
    """
    floor = scope + 1
    resolved = list(parts[:-1])
    pending = list(PurePosixPath(os.readlink(top.joinpath(*parts))).parts)
    hops = 0
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if len(resolved) <= floor:
                return False
            resolved.pop()
            continue
        if part.startswith("/"):
            return False
        resolved.append(part)
        try:
            info = top.joinpath(*resolved).lstat()
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode):
            hops += 1
            target = os.readlink(top.joinpath(*resolved))
            if hops > MAX_HOPS or target.startswith("/"):
                return False
            resolved.pop()
            pending = [*PurePosixPath(target).parts, *pending]
        elif not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            return False
    return len(resolved) > floor


def _is_real_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _link(root: Path, relative: str) -> Entry:
    target = os.readlink(root / relative)
    if not _plain(target) or not contained_target(relative, target):
        raise TreeError(f"{relative}: a link must stay inside the bundle")
    # The root itself may be a framework: a nested item's subtree digest.
    parts = (root.name, *PurePosixPath(relative).parts)
    scope = _framework(parts)
    inner = () if scope is None else parts[scope + 1 :]
    target_parts = PurePosixPath(target).parts
    placed = (
        inner == ("Versions", "Current")
        and len(target_parts) == 1
        and target_parts[0] not in (".", "..")
    ) or (len(inner) == 1 and len(target_parts) > 1 and target_parts[0] == "Versions")
    if scope is None or not placed:
        raise TreeError(
            f"{relative}: a link is allowed only as a framework's Versions/Current "
            "or a link at its top naming Versions/"
        )
    if not _resolves_inside(root.parent, parts, scope):
        raise TreeError(f"{relative}: a link must resolve inside its framework")
    if inner == ("Versions", "Current") and not _is_real_directory(
        (root / relative).parent / target
    ):
        raise TreeError(f"{relative}: Versions/Current names a version directory")
    return Entry(relative, "symlink", None, target)


def _entry(root: Path, path: Path) -> Entry:
    relative = path.relative_to(root).as_posix()
    if not _plain(relative):
        raise TreeError(
            "a bundle path can't hold a control character, a backslash or non-UTF-8"
        )
    info = path.lstat()
    if info.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise TreeError(f"{relative}: a setuid, setgid or sticky entry")
    if getattr(info, "st_flags", 0) & ~ALLOWED_FLAGS:
        raise TreeError(f"{relative}: an entry with file flags set")
    if stat.S_ISDIR(info.st_mode):
        return Entry(relative, "dir", EXECUTABLE_MODE, "-")
    if stat.S_ISREG(info.st_mode):
        if info.st_nlink > 1:
            raise TreeError(f"{relative}: a file with more than one hard link")
        return Entry(relative, "file", file_mode(info.st_mode), sha256_file(path))
    if not stat.S_ISLNK(info.st_mode):
        raise TreeError(f"{relative}: only files, directories and links")
    return _link(root, relative)


def listing(root: Path) -> tuple[Entry, ...]:
    """Every entry below ``root``, sorted by path, never following a link."""
    try:
        top = root.lstat()
    except OSError:
        raise TreeError("the bundle is missing") from None
    if not stat.S_ISDIR(top.st_mode):
        raise TreeError("the bundle is not a directory")
    if top.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX) or (
        getattr(top, "st_flags", 0) & ~ALLOWED_FLAGS
    ):
        raise TreeError("the bundle has a setuid, setgid or sticky bit, or flags")
    entries = [
        _entry(root, Path(directory) / name)
        for directory, dirs, files in os.walk(root, followlinks=False)
        for name in [*dirs, *files]
    ]
    return tuple(sorted(entries, key=lambda entry: entry.path.encode("utf-8")))


def digest(entries: tuple[Entry, ...]) -> str:
    content = "".join(entry.line() for entry in entries).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def tree_sha256(root: Path) -> str:
    """The canonical tree digest of the bundle at ``root``."""
    return digest(listing(root))
