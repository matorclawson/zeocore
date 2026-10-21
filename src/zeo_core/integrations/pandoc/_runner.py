"""
Run pandoc for the newsletter components, bounded and in a clean environment.

One place builds the child process, so the rules hold for both ``convert_newsletter``
and ``render_email_html``: no shell, the document on stdin and the result on stdout,
a fixed environment (no user defaults, no inherited variables), an empty working
directory and an empty data directory, an enforced timeout, a bound on both output
streams, its own process group (killed whole on a timeout or an overrun), and
``--sandbox`` so pandoc reads and writes no file and calls no program.

``--sandbox`` is not the whole defence, and it is not the same on every pandoc: CI's
pinned 3.1.3 still reads a local file and embeds it as a ``data:`` URI when it is
also given ``--embed-resources`` (CI log of PR #126, run 38449618130), while 3.11
refuses. So the argv is checked as well: ``check_flags`` allows only the few options
the two components use, and refuses everything that can read a file, embed a
resource, run a filter or a template, or write a file. zeocore never passes
``--standalone`` or ``--embed-resources``, so nothing is embedded on any version.

It also resolves *which* binary ran, and runs only a binary it has verified. The
``pandoc`` on ``PATH`` is never executed: it is read, hashed, and COPIED into a
private 0700 directory; the copy is hashed again and must match; only then is the
copy asked for its version and used. The version, the digest and the path a result
names are the copy's, so a renderer or converter tuple names the bytes that ran. The
copy is made once per process and is checked against its size, modification time
and inode before every run.
"""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import shutil
import signal
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

#: The most output one conversion may return, and the most diagnostic text read.
MAX_OUTPUT_BYTES: Final = 8_000_000
MAX_STDERR_BYTES: Final = 1_048_576

#: The only pandoc options a component may pass: choosing the reader and writer,
#: wrapping, line endings, highlighting, a metadata value, and our own Lua filter.
#: Everything else (``--standalone``, ``--embed-resources``, ``--self-contained``,
#: ``--extract-media``, ``--filter``, ``--template``, ``--data-dir``, ``--defaults``,
#: ``--metadata-file``, ``--include-*``, ``--resource-path``, ``--output``, any
#: single-dash option and any positional input file) is refused.
ALLOWED_FLAGS: Final = frozenset(
    {
        "--from",
        "--to",
        "--wrap",
        "--eol",
        "--no-highlight",
        "--metadata",
        "--lua-filter",
    }
)

_VERSION_LINE: Final = re.compile(r"(\d+(?:\.\d+)+)")
_CHUNK: Final = 65_536


@dataclass(frozen=True, slots=True)
class PandocBinary:
    """The pandoc that will run: its verified private copy, version and digest."""

    path: str
    version: str
    sha256: str


@dataclass(slots=True)
class _Verified:
    binary: PandocBinary
    signature: tuple[int, int, int]
    directory: tempfile.TemporaryDirectory[str]


#: Verified private copies, keyed by the source file's (real path, mtime, size).
_PRIVATE: dict[tuple[str, int, int], _Verified] = {}


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


def _signature(path: str) -> tuple[int, int, int]:
    info = os.stat(path)  # noqa: PTH116
    return (info.st_size, info.st_mtime_ns, info.st_ino)


def _read_version(path: str) -> str:
    """Ask a VERIFIED copy for its version; never called on the file on ``PATH``."""
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


def _copy_and_verify(
    real: str, expected_sha256: str | None = None
) -> tuple[PandocBinary, tempfile.TemporaryDirectory[str]]:
    """
    Hash the source, copy it, hash the copy, and only then run the copy.

    With ``expected_sha256``, a copy whose digest differs is refused BEFORE it is run
    even once (the ``--version`` probe is the first run).
    """
    try:
        source_sha = _sha256_of(real)  # a read: the file on PATH is never executed
    except OSError as error:
        raise refusal("pandoc_missing") from error
    work = tempfile.TemporaryDirectory(prefix="zeocore-pandoc-bin-")
    try:
        os.chmod(work.name, stat.S_IRWXU)  # noqa: PTH101
        target = Path(work.name) / "pandoc"
        shutil.copyfile(real, target)
        os.chmod(target, stat.S_IRWXU)  # noqa: PTH101
        copy_sha = _sha256_of(str(target))
        if copy_sha != source_sha:
            raise refusal("pandoc_failed")  # the file changed while it was copied
        if expected_sha256 is not None and copy_sha != expected_sha256:
            raise refusal("renderer_mismatch")  # nothing has been run
        version = _read_version(str(target))
        if _version_tuple(version)[:2] < MIN_PANDOC_VERSION:
            raise refusal("pandoc_too_old")
    except OSError as error:
        work.cleanup()
        raise refusal("pandoc_failed") from error
    except BaseException:
        work.cleanup()
        raise
    return PandocBinary(path=str(target), version=version, sha256=copy_sha), work


def find_pandoc(
    expected_sha256: str | None = None, *, path: str | None = None
) -> PandocBinary:
    """
    A verified private copy of the pandoc on ``PATH``: its version and digest.

    ``expected_sha256`` pins the binary: a ``PATH`` pandoc with another digest is
    refused as ``renderer_mismatch`` before anything is executed, not after the
    version probe has already run it.

    The ``PATH`` file is hashed and copied BEFORE anything is executed; the copy is
    hashed, must match, and is the only file ever run. Refuses when pandoc is missing,
    older than 2.15 (no ``--sandbox``), or changes while it is copied. The copy is
    made once per process and reused while the source's path, size and modification
    time are the same; a later change to the copy itself is refused at the next run.

    ``path`` names the binary directly: an ABSOLUTE path, used instead of any ``PATH``
    lookup (a relative path is refused as ``pandoc_missing``). The verification, the
    private copy and ``expected_sha256`` work exactly as for the ``PATH`` binary.
    """
    if path is not None:
        if not os.path.isabs(path):
            raise refusal("pandoc_missing")
        found: str | None = path
    else:
        found = shutil.which("pandoc")
    if found is None:
        raise refusal("pandoc_missing")
    real = os.path.realpath(found)
    try:
        info = os.stat(real)  # noqa: PTH116
    except OSError as error:
        raise refusal("pandoc_missing") from error
    key = (real, info.st_mtime_ns, info.st_size)
    kept = _PRIVATE.get(key)
    if kept is not None:
        _check_unchanged(kept)
        if expected_sha256 is not None and kept.binary.sha256 != expected_sha256:
            raise refusal("renderer_mismatch")
        return kept.binary
    for stale in [k for k in _PRIVATE if k[0] == real]:  # the source was replaced
        _PRIVATE.pop(stale).directory.cleanup()
    binary, work = _copy_and_verify(real, expected_sha256)
    _PRIVATE[key] = _Verified(binary, _signature(binary.path), work)
    return binary


def _check_unchanged(kept: _Verified) -> None:
    try:
        same = _signature(kept.binary.path) == kept.signature
    except OSError:
        same = False
    if not same:
        raise refusal("pandoc_failed")  # the verified copy was modified


def _verified(binary: PandocBinary) -> None:
    """Refuse to run any binary that ``find_pandoc`` did not verify, or that changed."""
    for kept in _PRIVATE.values():
        if kept.binary.path == binary.path:
            _check_unchanged(kept)
            return
    raise ValueError("pandoc was asked to run a binary that was not verified")


def check_flags(flags: list[str], *, lua_filter: str | None = None) -> None:
    """
    Refuse any option outside ``ALLOWED_FLAGS``, before pandoc is started.

    A ``--lua-filter`` is allowed only when its path is exactly ``lua_filter``, the
    package's own filter. This guards the code, not the input: the flags are ours, so
    a refusal here is a defect, raised as ``ValueError`` (its text names no input).
    """
    for flag in flags:
        name, _, value = flag.partition("=")
        if name not in ALLOWED_FLAGS:
            raise ValueError("a pandoc option outside the allowed set was passed")
        if name == "--lua-filter" and (lua_filter is None or value != lua_filter):
            raise ValueError("a Lua filter other than the package's own was passed")


class _OutputOverrun(Exception):  # noqa: N818 -- internal control flow, never raised out
    """The child wrote more than its allowance to one stream."""

    def __init__(self, *, stdout: bool) -> None:
        super().__init__()
        self.stdout = stdout


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the child and everything it started, then reap it."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()
    process.wait()


class _Pump:
    """The non-blocking transfer between this process and one child's three pipes."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        stdin: bytes,
        out_cap: int,
        err_cap: int,
    ) -> None:
        assert process.stdin is not None  # noqa: S101 -- pipes were requested
        assert process.stdout is not None  # noqa: S101
        assert process.stderr is not None  # noqa: S101
        self.stdin, self.stdout, self.stderr = (
            process.stdin,
            process.stdout,
            process.stderr,
        )
        self.view, self.sent = memoryview(stdin), 0
        self.caps = {self.stdout: out_cap, self.stderr: err_cap}
        self.buffers = {self.stdout: bytearray(), self.stderr: bytearray()}
        self.selector = selectors.DefaultSelector()
        for pipe in (self.stdin, self.stdout, self.stderr):
            os.set_blocking(pipe.fileno(), False)
        self.selector.register(self.stdout, selectors.EVENT_READ)
        self.selector.register(self.stderr, selectors.EVENT_READ)
        if self.view:
            self.selector.register(self.stdin, selectors.EVENT_WRITE)
        else:
            self.stdin.close()

    def open(self) -> bool:
        """True while any pipe is still registered."""
        return bool(self.selector.get_map())

    def step(self, seconds: float) -> None:
        """Wait up to ``seconds`` for a pipe, and move what is ready."""
        for selected, _ in self.selector.select(seconds):
            if selected.fileobj is self.stdin:
                self._write()
            else:
                self._read(selected.fileobj)

    def _write(self) -> None:
        try:
            self.sent += os.write(self.stdin.fileno(), self.view[self.sent :][:_CHUNK])
        except BlockingIOError:
            return
        except BrokenPipeError:
            self.sent = len(self.view)  # the child stopped reading
        if self.sent >= len(self.view):
            self.selector.unregister(self.stdin)
            self.stdin.close()

    def _read(self, pipe: object) -> None:
        try:
            data = os.read(pipe.fileno(), _CHUNK)  # type: ignore[attr-defined]
        except BlockingIOError:
            return
        if not data:
            self.selector.unregister(pipe)  # type: ignore[arg-type]
            return
        buffer = self.buffers[pipe]  # type: ignore[index]
        buffer.extend(data)
        if len(buffer) > self.caps[pipe]:  # type: ignore[index]
            raise _OutputOverrun(stdout=pipe is self.stdout)

    def close(self) -> None:
        """Close every pipe and the selector."""
        self.selector.close()
        for pipe in (self.stdin, self.stdout, self.stderr):
            if not pipe.closed:
                pipe.close()


def _run_child(
    argv: list[str],
    stdin: bytes,
    *,
    timeout_s: float,
    cwd: str,
    env: dict[str, str],
    out_cap: int,
    err_cap: int,
) -> subprocess.CompletedProcess[bytes]:
    """
    Run one child in its own process group, with bounded reads and one deadline.

    ``stdin`` is written, and stdout and stderr are read, without ever holding more
    than their allowance: an overrun, or the deadline, kills the whole group. Raises
    ``subprocess.TimeoutExpired`` at the deadline and ``_OutputOverrun`` on an overrun.
    """
    process = subprocess.Popen(  # noqa: S603
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    end = time.monotonic() + timeout_s
    pump = _Pump(process, stdin, out_cap, err_cap)
    try:
        while pump.open():
            left = end - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired(argv, timeout_s)
            pump.step(min(left, 0.5))
        code = process.wait(timeout=max(end - time.monotonic(), 0.001))
    except BaseException:
        _kill_group(process)
        raise
    finally:
        pump.close()
    return subprocess.CompletedProcess(
        argv, code, bytes(pump.buffers[pump.stdout]), bytes(pump.buffers[pump.stderr])
    )


def run_pandoc(
    binary: PandocBinary,
    flags: list[str],
    stdin: bytes,
    *,
    timeout_s: float,
    lua_filter: str | None = None,
) -> bytes:
    """
    Run ``pandoc <flags>`` with ``stdin`` and return stdout.

    ``binary`` must be what ``find_pandoc`` returned and unchanged since. ``--sandbox``
    and an empty ``--data-dir`` are always added, and ``flags`` must pass
    ``check_flags`` (``lua_filter`` names the one filter allowed). The child gets a
    fixed environment, an empty working directory and its own process group, which is
    killed whole on a timeout or when either output stream passes its bound. A
    timeout, a failure or an oversized result is a typed refusal; pandoc's own error
    text is never returned.
    """
    check_flags(flags, lua_filter=lua_filter)
    _verified(binary)
    with tempfile.TemporaryDirectory(prefix="zeocore-pandoc-") as work:
        data_dir = Path(work) / "data"
        data_dir.mkdir()
        argv = [binary.path, "--sandbox", f"--data-dir={data_dir}", *flags]
        try:
            done = _run_child(
                argv,
                stdin,
                timeout_s=timeout_s,
                cwd=work,
                env={"HOME": work, "LC_ALL": "C"},
                out_cap=MAX_OUTPUT_BYTES,
                err_cap=MAX_STDERR_BYTES,
            )
        except subprocess.TimeoutExpired as error:
            raise refusal("pandoc_timeout") from error
        except _OutputOverrun as error:
            raise refusal(
                "output_too_large" if error.stdout else "pandoc_failed"
            ) from error
        except OSError as error:
            raise refusal("pandoc_failed") from error
    if done.returncode != 0:
        raise _failure(done.stderr)
    return done.stdout


_FILTER_REFUSAL: Final = re.compile(rb"ZEOCORE_REFUSE:([a-z0-9_]+)")


def _failure(stderr: bytes) -> Exception:
    """A refusal for a failed run: the filter's own code, else a generic failure."""
    found = _FILTER_REFUSAL.search(stderr)
    if found is not None:
        return refusal(found.group(1).decode("ascii"))
    return refusal("pandoc_failed")
