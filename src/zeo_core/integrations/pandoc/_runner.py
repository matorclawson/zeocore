"""
Run pandoc for the newsletter components, bounded and in a clean environment.

One place builds the child process, so the rules hold for both ``convert_newsletter``
and ``render_email_html``: no shell, the document on stdin and the result on stdout,
a fixed environment (no user defaults, no inherited variables), an empty working
directory and an empty data directory, an enforced timeout, a bound on the output,
and ``--sandbox`` so pandoc reads and writes no file and calls no program.

It also resolves *which* binary ran: the version, and the sha256 of the executable
itself (not only its name), so a renderer's output can be tied to the exact tool.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from zeo_core.integrations.pandoc.refusals import refusal

#: The oldest pandoc with ``--sandbox``.
MIN_PANDOC_VERSION: Final = (2, 15)

#: The most output one conversion may return.
MAX_OUTPUT_BYTES: Final = 8_000_000

_VERSION_LINE: Final = re.compile(r"(\d+(?:\.\d+)+)")


@dataclass(frozen=True, slots=True)
class PandocBinary:
    """The pandoc that will run: where, which version, and its digest."""

    path: str
    version: str
    sha256: str


_CACHE: dict[tuple[str, int, int], PandocBinary] = {}


class Deadline:
    """One time budget for a whole conversion: cleaning, parsing and pandoc."""

    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + max(seconds, 0.001)

    def check(self) -> None:
        """Refuse once the budget is spent."""
        if time.monotonic() > self._end:
            raise refusal("convert_timeout")

    def remaining(self) -> float:
        """Seconds left, never zero."""
        return max(self._end - time.monotonic(), 0.001)


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:  # noqa: PTH123
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_version(path: str) -> str:
    try:
        done = subprocess.run(  # noqa: S603
            [path, "--version"],
            capture_output=True,
            check=False,
            timeout=10,
            stdin=subprocess.DEVNULL,
            env={"LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise refusal("pandoc_missing") from error
    first = done.stdout.decode("utf-8", "replace").splitlines()[:1]
    found = _VERSION_LINE.search(first[0]) if first else None
    if done.returncode != 0 or found is None:
        raise refusal("pandoc_missing")
    return found.group(1)


def find_pandoc() -> PandocBinary:
    """
    The pandoc on ``PATH``: resolved, version-checked and hashed.

    Refuses when pandoc is missing, or older than 2.15 (no ``--sandbox``). The
    result is cached by the executable's path, size and modification time.
    """
    found = shutil.which("pandoc")
    if found is None:
        raise refusal("pandoc_missing")
    real = os.path.realpath(found)
    try:
        stat = os.stat(real)  # noqa: PTH116
    except OSError as error:
        raise refusal("pandoc_missing") from error
    key = (real, stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    version = _read_version(real)
    if _version_tuple(version)[:2] < MIN_PANDOC_VERSION:
        raise refusal("pandoc_too_old")
    binary = PandocBinary(path=real, version=version, sha256=_sha256_of(real))
    _CACHE[key] = binary
    return binary


_PRIVATE: dict[
    str, tuple[PandocBinary, tuple[int, int, int], tempfile.TemporaryDirectory[str]]
] = {}


def private_copy(binary: PandocBinary) -> PandocBinary:
    """
    A private copy of the pandoc executable, hashed and run as the copy.

    Hashing the file on ``PATH`` and then running it leaves a window in which another
    process could swap it. The copy lives in a fresh 0700 directory only this process
    can write to, and its version and digest are read from the copy, so a renderer
    tuple names the bytes that ran. The copy is made once per process and reused while
    its size, modification time and inode are unchanged; it is removed at exit.
    """
    kept = _PRIVATE.get(binary.path)
    if kept is not None:
        copy, signature, _ = kept
        if _signature(copy.path) == signature and copy.sha256 == _recorded(binary):
            return copy
    work = tempfile.TemporaryDirectory(prefix="zeocore-renderer-")
    os.chmod(work.name, stat.S_IRWXU)  # noqa: PTH101
    target = Path(work.name) / "pandoc"
    try:
        shutil.copyfile(binary.path, target)
        os.chmod(target, stat.S_IRWXU)  # noqa: PTH101
    except OSError as error:
        work.cleanup()
        raise refusal("pandoc_failed") from error
    copy = PandocBinary(
        path=str(target),
        version=_read_version(str(target)),
        sha256=_sha256_of(str(target)),
    )
    if copy.sha256 != binary.sha256:
        work.cleanup()
        raise refusal("pandoc_failed")  # the file changed while it was copied
    _PRIVATE[binary.path] = (copy, _signature(str(target)), work)
    return copy


def _recorded(binary: PandocBinary) -> str:
    return binary.sha256


def _signature(path: str) -> tuple[int, int, int]:
    info = os.stat(path)  # noqa: PTH116
    return (info.st_size, info.st_mtime_ns, info.st_ino)


def run_pandoc(
    binary: PandocBinary,
    flags: list[str],
    stdin: bytes,
    *,
    timeout_s: float,
) -> bytes:
    """
    Run ``pandoc <flags>`` with ``stdin`` and return stdout.

    ``--sandbox`` and an empty ``--data-dir`` are always added. The child gets a
    fixed environment and an empty working directory. A timeout, a failure or an
    oversized result is a typed refusal; pandoc's own error text is never returned.
    """
    with tempfile.TemporaryDirectory(prefix="zeocore-pandoc-") as work:
        data_dir = Path(work) / "data"
        data_dir.mkdir()
        argv = [binary.path, "--sandbox", f"--data-dir={data_dir}", *flags]
        try:
            done = subprocess.run(  # noqa: S603
                argv,
                input=stdin,
                capture_output=True,
                check=False,
                timeout=timeout_s,
                cwd=work,
                env={"HOME": work, "LC_ALL": "C"},
            )
        except subprocess.TimeoutExpired as error:
            raise refusal("pandoc_timeout") from error
        except OSError as error:
            raise refusal("pandoc_failed") from error
    if done.returncode != 0:
        raise _failure(done.stderr)
    if len(done.stdout) > MAX_OUTPUT_BYTES:
        raise refusal("output_too_large")
    return done.stdout


_FILTER_REFUSAL: Final = re.compile(rb"ZEOCORE_REFUSE:([a-z0-9_]+)")


def _failure(stderr: bytes) -> Exception:
    """A refusal for a failed run: the filter's own code, else a generic failure."""
    found = _FILTER_REFUSAL.search(stderr)
    if found is not None:
        return refusal(found.group(1).decode("ascii"))
    return refusal("pandoc_failed")
