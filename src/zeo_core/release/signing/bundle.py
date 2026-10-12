"""What a bundle holds that must be signed (ZEOCORE-SOW-32 §1.1, §1.5).

Every Mach-O file, found by its magic number and never by its name, every
other file with an execute bit, and every nested bundle directory. Each must
be listed in the manifest, except a bundle's own main executable, which
codesign signs as part of that bundle. Only a Mach-O file or a nested bundle
can be listed (the ruling draft r4 §8A), so an executable script refuses the
bundle.
"""

from __future__ import annotations

import plistlib
from pathlib import Path, PurePosixPath
from typing import Final, Literal

from .manifest import MainExecutable, Nested
from .tree import Entry

#: Thin and universal Mach-O, either byte order, 32 and 64 bit. ``cafebabe``
#: is also a Java class file's magic: one in a bundle is counted as code, so
#: it is refused unless listed, never skipped.
MACHO_MAGICS: Final = frozenset(
    bytes.fromhex(magic)
    for magic in (
        "feedface",
        "feedfacf",
        "cefaedfe",
        "cffaedfe",
        "cafebabe",
        "bebafeca",
        "cafebabf",
        "bfbafeca",
    )
)
#: Directories that are bundles of their own. SOW-32 §1.1 names the first
#: five; the rest are other bundle kinds codesign seals the same way.
BUNDLE_SUFFIXES: Final = (
    ".app",
    ".framework",
    ".bundle",
    ".xpc",
    ".appex",
    ".plugin",
    ".kext",
    ".dext",
    ".systemextension",
)


def is_macho(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(4) in MACHO_MAGICS


def is_bundle_directory(relative: str) -> bool:
    return PurePosixPath(relative).name.endswith(BUNDLE_SUFFIXES)


def _info_plist(root: Path, bundle: str) -> dict[str, object] | None:
    base = root / bundle if bundle else root
    for candidate in (
        base / "Contents" / "Info.plist",
        base / "Resources" / "Info.plist",
    ):
        try:
            value = plistlib.loads(candidate.read_bytes())
        except FileNotFoundError, NotADirectoryError:
            continue
        except Exception:  # plistlib raises several unrelated types
            return None
        return value if isinstance(value, dict) else None
    return None


def bundle_identifier(root: Path) -> str | None:
    """The outer bundle's ``CFBundleIdentifier``, or None if it can't be read."""
    info = _info_plist(root, "")
    value = None if info is None else info.get("CFBundleIdentifier")
    return value if isinstance(value, str) else None


def info_executable(root: Path, bundle: str) -> tuple[bool, str | None]:
    """Whether Info.plist names a main executable, and its real path.

    The path is relative to the bundle: ``Contents/MacOS/<name>``, or for a
    ``.framework`` its binary's real path. It is None unless it names a
    Mach-O file inside the bundle.
    """
    info = _info_plist(root, bundle)
    name = None if info is None else info.get("CFBundleExecutable")
    if name is None:
        return False, None
    if not isinstance(name, str) or not name or "/" in name or name in (".", ".."):
        return True, None
    base = root / bundle if bundle else root
    framework = PurePosixPath(bundle).name.endswith(".framework")
    candidate = base / name if framework else base / "Contents" / "MacOS" / name
    try:
        real = candidate.resolve(strict=True)
    except OSError, RuntimeError:
        return True, None
    real_base = base.resolve()
    if not real.is_relative_to(real_base) or not real.is_file() or not is_macho(real):
        return True, None
    return True, real.relative_to(real_base).as_posix()


Problem = tuple[str, str]  # a refusal's reason and its message


def _main_problem(
    root: Path, bundle: str, declared: MainExecutable | Literal["none"]
) -> tuple[Problem | None, str | None]:
    """A declared main executable against Info.plist, and its path in ``root``."""
    where = bundle or "the bundle"
    named, real = info_executable(root, bundle)
    if not named:
        if declared != "none":
            return (
                "main_executable_invalid",
                f"{where}: Info.plist names no main executable, but one is declared",
            ), None
        return None, None
    if declared == "none":
        return (
            "main_executable_invalid",
            f"{where}: Info.plist names a main executable that isn't declared",
        ), None
    if real is None:
        return (
            "main_executable_invalid",
            f"{where}: the main executable is missing or not Mach-O",
        ), None
    if real != declared.path:
        return (
            "main_executable_invalid",
            f"{where}: the main executable is {real}, not {declared.path}",
        ), None
    return None, f"{bundle}/{real}" if bundle else real


def shape(
    root: Path,
    entries: tuple[Entry, ...],
    main: MainExecutable | Literal["none"],
    nested: tuple[Nested, ...],
) -> Problem | None:
    """Every piece of code in ``root`` against the manifest, or the first gap.

    ``entries`` is the tree's listing, so links are never followed: a file
    reached through one is seen once, at its real path. A bundle's main
    executable, at every level, is its bundle's own: declared, never listed.
    """
    bundles = {
        entry.path
        for entry in entries
        if entry.type == "dir" and is_bundle_directory(entry.path)
    }
    files = [entry.path for entry in entries if entry.type == "file"]
    machos = {path for path in files if is_macho(root / path)}
    executables = {path for path in files if (root / path).stat().st_mode & 0o111}
    mains: set[str] = set()
    owners = [("", main)] + [
        (item.path, item.main_executable)
        for item in nested
        if item.kind == "bundle" and item.path in bundles
    ]
    for bundle, declared in owners:
        problem, found = _main_problem(root, bundle, declared)
        if problem is not None:
            return problem
        if found is not None:
            mains.add(found)
    listed = {item.path for item in nested}
    if listed & mains:
        return (
            "main_executable_listed",
            "a main executable is signed with its bundle, never listed: "
            f"{sorted(listed & mains)[0]}",
        )
    unlisted = sorted((bundles | ((machos | executables) - mains)) - listed)
    if unlisted:
        return (
            "nested_unlisted",
            f"the bundle holds code its manifest doesn't list: {unlisted[0]}",
        )
    for item in nested:
        present = (
            item.path in bundles
            if item.kind == "bundle"
            else item.path in machos and item.path not in bundles
        )
        if not present:
            return (
                "nested_missing",
                f"a listed nested item is not a {item.kind} here: {item.path}",
            )
    return None
