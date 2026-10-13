"""The step's only contact with the host: ``codesign`` and ``security``.

``Toolchain`` is the seam the step decides through, so every refusal has a
fixture without a key. ``DarwinToolchain`` is the real one. It reads key-access
metadata and nothing else: it never unlocks a keychain, changes an ACL or a
partition list, exports, or prints a key or password (SOW-20 §5).
"""

from __future__ import annotations

import hashlib
import plistlib
import socket
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from .identity import Identity, parse_designated

TIMEOUT_SECONDS: Final = 300

SignCause = Literal["signed", "identity_missing", "failed_unknown"]


class KeychainState(StrEnum):
    UNLOCKED = "unlocked"
    LOCKED = "locked"
    UNKNOWN = "unknown"


class ToolError(RuntimeError):
    """A read could not be completed: the step holds rather than guesses."""


class SignAnswerLostError(RuntimeError):
    """``codesign`` may have run, but its answer was lost (SOW-20 §4)."""


@dataclass(frozen=True)
class Inspection:
    """What the signature on one file says, read back from the file."""

    signed: bool
    identifier: str | None = None
    designated: str | None = None
    designated_explicit: bool = False
    leaf_sha1: str | None = None  # upper-case hex
    entitlements: dict[str, Any] | None = None


@dataclass(frozen=True)
class Assessment:
    """What Gatekeeper said about a bundle, kept as information only."""

    status: int | None  # None when spctl could not be run
    output: str


#: How much of spctl's answer is kept.
ASSESSMENT_LIMIT: Final = 4096


class Toolchain(Protocol):
    def host(self) -> str: ...

    def keychain_state(self) -> KeychainState: ...

    def identities(self) -> frozenset[str]:
        """The SHA-1s of the valid code-signing identities this user can see."""
        ...

    def sign(
        self,
        path: Path,
        *,
        identifier: str,
        identity: Identity,
        requirement: str,
        entitlements: Path | None,
        hardened: bool,
    ) -> SignCause: ...

    def satisfies(self, path: Path, requirement: str) -> bool:
        """``codesign --verify --strict`` against an explicit requirement.

        False only for a real non-match. A tool or syntax error raises
        ``ToolError``, so it is held rather than read as "not signed".
        """
        ...

    def sealed(self, path: Path) -> bool:
        """``codesign --verify --strict --deep`` on a whole bundle.

        Verification only: ``--deep`` is never used to sign (SOW-32 §3). False
        only for a bundle that doesn't verify; a tool error raises
        ``ToolError``.
        """
        ...

    def assess(self, path: Path) -> Assessment:
        """``spctl --assess --type execute -vv``: information, never a verdict.

        An internal bundle isn't notarised, so a rejection is expected.
        """
        ...

    def inspect(self, path: Path) -> Inspection: ...


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: bytes
    stderr: bytes


Runner = Callable[[Sequence[str]], Completed]


def run_tool(argv: Sequence[str]) -> Completed:
    """Run one tool with no shell, no stdin and a bound on its time."""
    done = subprocess.run(  # noqa: S603 - fixed argv, never a shell
        list(argv),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    return Completed(done.returncode, done.stdout, done.stderr)


_CODESIGN: Final = "/usr/bin/codesign"
_SECURITY: Final = "/usr/bin/security"
_SPCTL: Final = "/usr/sbin/spctl"
_LOGIN_KEYCHAIN: Final = Path.home() / "Library" / "Keychains" / "login.keychain-db"
# Reading an unlocked keychain's settings needs no interaction. Only a locked
# one answers with errSecInteractionNotAllowed, so this text establishes a
# lock. Any other failure leaves the state unknown, never "locked".
_LOCKED_TEXT: Final = b"User interaction is not allowed"
# codesign's exit status when the code is valid but fails the -R requirement.
# Any other non-zero status is a tool or syntax error, never a non-match.
_UNSATISFIED: Final = 3
# codesign's exit status when --verify finds the code invalid or altered.
_INVALID: Final = 1
_MISSING_TEXT: Final = (b"no identity found", b"could not be found in the keychain")


class DarwinToolchain:
    def __init__(
        self, run: Runner = run_tool, keychain: Path = _LOGIN_KEYCHAIN
    ) -> None:
        self._run = run
        self._keychain = keychain

    def host(self) -> str:
        return socket.gethostname().split(".", 1)[0].lower()

    def _read(self, argv: Sequence[str]) -> Completed:
        try:
            return self._run(argv)
        except (OSError, subprocess.SubprocessError) as error:
            raise ToolError(f"{Path(argv[0]).name} could not be run") from error

    def keychain_state(self) -> KeychainState:
        try:
            done = self._read([_SECURITY, "show-keychain-info", str(self._keychain)])
        except ToolError:
            return KeychainState.UNKNOWN
        if done.returncode == 0:
            return KeychainState.UNLOCKED
        if _LOCKED_TEXT in done.stderr:
            return KeychainState.LOCKED
        return KeychainState.UNKNOWN

    def identities(self) -> frozenset[str]:
        done = self._read([_SECURITY, "find-identity", "-v", "-p", "codesigning"])
        if done.returncode != 0:
            raise ToolError("the code-signing identities could not be listed")
        found = set()
        for line in done.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].endswith(")") and len(parts[1]) == 40:
                found.add(parts[1].upper())
        return frozenset(found)

    def sign(
        self,
        path: Path,
        *,
        identifier: str,
        identity: Identity,
        requirement: str,
        entitlements: Path | None,
        hardened: bool,
    ) -> SignCause:
        argv = [
            _CODESIGN,
            "--force",
            "--sign",
            identity.certificate_sha1,
            "--identifier",
            identifier,
            "--requirements",
            f"=designated => {requirement}",
        ]
        if hardened:
            argv += ["--options", "runtime", "--timestamp"]
        if entitlements is not None:
            argv += ["--entitlements", str(entitlements)]
        argv.append(str(path))
        try:
            done = self._run(argv)
        except OSError:
            # Never started: nothing can have been signed.
            return "failed_unknown"
        except subprocess.SubprocessError as error:
            raise SignAnswerLostError("codesign's answer was lost") from error
        if done.returncode == 0:
            return "signed"
        if any(text in done.stderr for text in _MISSING_TEXT):
            return "identity_missing"
        # errSecInternalComponent and everything else not established: never
        # diagnosed as a locked keychain by itself (SOW-20 §4).
        return "failed_unknown"

    def satisfies(self, path: Path, requirement: str) -> bool:
        done = self._read(
            [
                _CODESIGN,
                "--verify",
                "--strict",
                # The bare requirement: codesign's -R grammar has no
                # "designated =>" prefix, and refuses it as a syntax error.
                f"-R={requirement}",
                str(path),
            ]
        )
        if done.returncode == 0:
            return True
        if done.returncode == _UNSATISFIED:
            return False
        raise ToolError("codesign could not test the requirement")

    def sealed(self, path: Path) -> bool:
        done = self._read([_CODESIGN, "--verify", "--strict", "--deep", str(path)])
        if done.returncode == 0:
            return True
        if done.returncode in (_INVALID, _UNSATISFIED):
            return False
        raise ToolError("codesign could not verify the bundle")

    def assess(self, path: Path) -> Assessment:
        try:
            done = self._read(
                [_SPCTL, "--assess", "--type", "execute", "-vv", str(path)]
            )
        except ToolError:
            return Assessment(None, "spctl could not be run")
        text = (done.stdout + done.stderr).decode("utf-8", "replace").strip()
        return Assessment(done.returncode, text[:ASSESSMENT_LIMIT])

    def inspect(self, path: Path) -> Inspection:
        details = self._read([_CODESIGN, "-d", "-vv", str(path)])
        if details.returncode != 0:
            return Inspection(signed=False)
        identifier = None
        for line in details.stderr.decode("utf-8", "replace").splitlines():
            if line.startswith("Identifier="):
                identifier = line.removeprefix("Identifier=")
        requirement = self._read([_CODESIGN, "-d", "-r-", str(path)])
        parsed = parse_designated(requirement.stdout.decode("utf-8", "replace"))
        return Inspection(
            signed=True,
            identifier=identifier,
            designated=parsed[0] if parsed else None,
            designated_explicit=parsed[1] if parsed else False,
            leaf_sha1=self._leaf_sha1(path),
            entitlements=self._entitlements(path),
        )

    def _leaf_sha1(self, path: Path) -> str | None:
        with tempfile.TemporaryDirectory() as scratch:
            prefix = Path(scratch) / "certificate"
            self._read([_CODESIGN, "-d", f"--extract-certificates={prefix}", str(path)])
            leaf = Path(f"{prefix}0")
            if not leaf.is_file():
                return None
            return (
                hashlib.sha1(leaf.read_bytes(), usedforsecurity=False)
                .hexdigest()
                .upper()
            )

    def _entitlements(self, path: Path) -> dict[str, Any] | None:
        done = self._read([_CODESIGN, "-d", "--entitlements", "-", "--xml", str(path)])
        if done.returncode != 0:
            raise ToolError("the signed entitlements could not be read")
        if not done.stdout.strip():
            return None
        try:
            value = plistlib.loads(done.stdout)
        except Exception as error:  # plistlib raises several unrelated types
            raise ToolError(
                "the signed entitlements are not a property list"
            ) from error
        if not isinstance(value, dict):
            raise ToolError("the signed entitlements are not a dictionary")
        return value
