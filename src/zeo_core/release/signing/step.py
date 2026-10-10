"""``sign`` and ``verify``: two acts with separate receipts (ZEOCORE-SOW-19 §1).

A release counts as signed only if ``verify`` passes. ``sign`` runs only on
the configured signing host (SOW-22 §2) and signs a separate candidate, never
the unsigned original. ``verify`` is read-only and runs anywhere.

Built so far: internal Darwin releases, signed with ZEO Runtime Code Signing.
Distributed releases are held as ``notarisation_unbuilt`` until notarisation,
stapling and the Gatekeeper check are built, so the internal path never stands
in for them. Linux executables are refused as signed until a Linux signature
contract is adopted (``zeocore#20#linux-executable-signature``).
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from .identity import (
    Identity,
    audience_of_certificate,
    designated_requirement,
    for_audience,
    names_identity,
)
from .journal import FAILED, INTENT, SIGNED, SigningJournal
from .manifest import CURRENT, Executable, LoadedManifest
from .toolchain import KeychainState, SignAnswerLostError, Toolchain, ToolError

RECEIPT_VERSION: Final = 1

Status = Literal["done", "waiting", "held", "refused", "ambiguous"]
Clock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Outcome:
    status: Status
    reason: str  # "signed", "verified" or the refusal's name
    message: str
    receipts: tuple[dict[str, Any], ...] = field(default=())
    executable: str | None = None


def _stop(
    status: Status, reason: str, message: str, name: str | None = None
) -> Outcome:
    return Outcome(status=status, reason=reason, message=message, executable=name)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _matches(path: Path, expected: str) -> bool:
    try:
        return path.is_file() and sha256_file(path) == expected
    except OSError:
        return False


class WorkDir:
    """Candidates, receipts and the journal for one signing host."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.signed = root / "signed"
        self.receipts = root / "receipts"

    def prepare(self) -> None:
        for directory in (self.root, self.signed, self.receipts):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def candidate(self, name: str) -> Path:
        return self.signed / name

    def receipt_path(self, name: str) -> Path:
        return self.receipts / f"sign-{name}.json"

    def read_receipt(self, name: str) -> dict[str, Any] | None:
        try:
            value = json.loads(self.receipt_path(name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(value, dict):
            raise ValueError(f"{name}: the sign receipt is not an object")
        return value

    def write_receipt(self, name: str, receipt: dict[str, Any]) -> None:
        """Once only: a completed receipt is never edited (SOW-14)."""
        payload = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
        descriptor = os.open(
            self.receipt_path(name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())


def _expected_entitlements(
    loaded: LoadedManifest, item: Executable
) -> dict[str, Any] | None:
    if item.entitlements == "none":
        return None
    value = plistlib.loads(loaded.resolve(item.entitlements.path).read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"{item.name}: the entitlements file is not a dictionary")
    return value or None


def _check_signature(
    path: Path,
    item: Executable,
    identity: Identity,
    expected_entitlements: dict[str, Any] | None,
    toolchain: Toolchain,
) -> tuple[str | None, dict[str, Any]]:
    """The refusal for this signed file, or None, with what was read back.

    A valid Apple signature alone does not prove that this is the intended
    program: the identifier, the requirement, the leaf certificate and the
    entitlements must all be the manifest's (SOW-20 §4).
    """
    seen = toolchain.inspect(path)
    facts: dict[str, Any] = {
        "identifier": seen.identifier,
        "designated_requirement": seen.designated,
        "certificate_sha1": seen.leaf_sha1,
    }
    if (
        not seen.signed
        or not seen.designated
        or not seen.designated_explicit
        or not names_identity(seen.designated)
        or seen.leaf_sha1 is None
    ):
        # Unsigned, ad-hoc, or a bare cdhash: not signed in the org's sense,
        # even when an ad-hoc signature carries a requirement naming our pin.
        return "not_signed", facts
    owner = audience_of_certificate(seen.leaf_sha1)
    if owner is not None and owner != item.audience:
        return "wrong_audience", facts
    expected = designated_requirement(item.identifier, identity)
    if (
        seen.leaf_sha1 != identity.certificate_sha1
        or seen.identifier != item.identifier
        or seen.designated != expected
    ):
        return "identity_mismatch", facts
    if not toolchain.satisfies(path, expected):
        return "not_signed", facts
    if (seen.entitlements or None) != expected_entitlements:
        return "entitlements_mismatch", facts
    return None, facts


def _unsupported(item: Executable, *, verifying: bool) -> Outcome | None:
    if item.platform == "linux":
        return _stop(
            "refused",
            "platform_signature_unadopted",
            "no Linux executable signature is adopted yet, so it is not called signed",
            item.name,
        )
    if item.policy != CURRENT:
        if verifying:
            return _stop(
                "held",
                "legacy_policy_unpinned",
                f"{item.policy} can't be verified until its exact pins are recorded",
                item.name,
            )
        return _stop(
            "refused",
            "legacy_policy_verify_only",
            f"{item.policy} is verify-only; nothing new is signed under it",
            item.name,
        )
    if item.audience == "distributed":
        return _stop(
            "held",
            "notarisation_unbuilt",
            "distributed signing needs notarisation, which isn't built yet",
            item.name,
        )
    return None


def _key(loaded: LoadedManifest, item: Executable) -> str:
    return f"{loaded.sha256}:{item.name}"


def sign(
    loaded: LoadedManifest,
    work: WorkDir,
    toolchain: Toolchain,
    signing_host: str | None,
    *,
    now: Clock = _now,
) -> Outcome:
    """Sign every executable in the manifest, or none past the first refusal."""
    if not signing_host:
        return _stop(
            "held",
            "signing_host_unconfigured",
            "set ZEOCORE_SIGNING_HOST to the host that holds the signing identity",
        )
    if toolchain.host() != signing_host.lower():
        return _stop(
            "held",
            "signing_host_mismatch",
            f"this host is not the configured signing host {signing_host}",
        )
    entitlements: dict[str, dict[str, Any] | None] = {}
    for item in loaded.signing.executables:
        stop = _unsupported(item, verifying=False)
        if stop is not None:
            return stop
        files_match = _matches(loaded.resolve(item.path), item.unsigned_sha256) and (
            item.entitlements == "none"
            or _matches(
                loaded.resolve(item.entitlements.path), item.entitlements.sha256
            )
        )
        if not files_match:
            return _stop(
                "refused",
                "input_mismatch",
                "an input is missing or its digest differs from the manifest",
                item.name,
            )
        entitlements[item.name] = _expected_entitlements(loaded, item)

    work.prepare()
    journal = SigningJournal(work.root / "journal.jsonl")
    try:
        run = _Run(loaded, work, toolchain, journal, entitlements, signing_host, now)
        return run.sign_all()
    finally:
        journal.close()


@dataclass(frozen=True)
class _Run:
    loaded: LoadedManifest
    work: WorkDir
    toolchain: Toolchain
    journal: SigningJournal
    entitlements: dict[str, dict[str, Any] | None]
    host: str
    now: Clock

    def sign_all(self) -> Outcome:
        receipts: list[dict[str, Any]] = []
        pending: list[Executable] = []
        for item in self.loaded.signing.executables:
            settled = self._settle(item)
            if isinstance(settled, Outcome):
                return settled
            if settled is None:
                pending.append(item)
            else:
                receipts.append(settled)
        if pending:
            ready = self._ready()
            if ready is not None:
                return ready
        for item in pending:
            signed = self._sign_one(item)
            if isinstance(signed, Outcome):
                return signed
            receipts.append(signed)
        return Outcome("done", "signed", "every executable is signed", tuple(receipts))

    def _check(self, item: Executable, path: Path) -> tuple[str | None, dict[str, Any]]:
        return _check_signature(
            path,
            item,
            for_audience(item.audience),
            self.entitlements[item.name],
            self.toolchain,
        )

    def _settle(self, item: Executable) -> dict[str, Any] | Outcome | None:
        """An earlier run's receipt, a reconciled lost answer, or None to sign."""
        existing = self.work.read_receipt(item.name)
        if existing is not None:
            if existing.get("manifest_sha256") == self.loaded.sha256:
                return existing
            return _stop(
                "refused",
                "receipt_mismatch",
                "this work directory holds a receipt for another manifest",
                item.name,
            )
        if not self.journal.is_open(_key(self.loaded, item)):
            return None
        # An earlier attempt's answer was lost. Read the candidate back: signed
        # as intended closes it; anything else stays ambiguous.
        candidate = self.work.candidate(item.name)
        try:
            refusal, facts = self._check(item, candidate)
        except ToolError:
            refusal, facts = "unreadable", {}
        if refusal is not None or not candidate.is_file():
            return _stop(
                "ambiguous",
                "signing_attempt_unreconciled",
                "an earlier attempt's answer was lost and its candidate doesn't "
                "verify; reconcile it before any new attempt",
                item.name,
            )
        return self._record(item, candidate, facts, reconciled=True)

    def _ready(self) -> Outcome | None:
        if self.toolchain.keychain_state() is KeychainState.LOCKED:
            return _stop(
                "waiting", "signing_keychain_locked", "unlock the login keychain"
            )
        try:
            available = self.toolchain.identities()
        except ToolError:
            return _stop(
                "held",
                "signing_failed_unknown",
                "the signing identities could not be read",
            )
        for item in self.loaded.signing.executables:
            identity = for_audience(item.audience)
            if identity.certificate_sha1 not in available:
                return _stop(
                    "held",
                    "signing_identity_missing",
                    f"{identity.common_name} is not available to this user",
                    item.name,
                )
        return None

    def _sign_one(self, item: Executable) -> dict[str, Any] | Outcome:
        identity = for_audience(item.audience)
        candidate = self.work.candidate(item.name)
        shutil.copy2(self.loaded.resolve(item.path), candidate)
        if not _matches(candidate, item.unsigned_sha256):
            return _stop(
                "refused", "input_mismatch", "the candidate copy differs", item.name
            )
        key = _key(self.loaded, item)
        self.journal.append(key, INTENT, self.now())
        try:
            cause = self.toolchain.sign(
                candidate,
                identifier=item.identifier,
                identity=identity,
                requirement=designated_requirement(item.identifier, identity),
                entitlements=(
                    None
                    if item.entitlements == "none"
                    else self.loaded.resolve(item.entitlements.path)
                ),
                hardened=item.audience == "distributed",
            )
        except SignAnswerLostError:
            return _stop(
                "ambiguous",
                "signing_attempt_unreconciled",
                "codesign's answer was lost; reconcile before any new attempt",
                item.name,
            )
        if cause != "signed":
            self.journal.append(key, FAILED, self.now())
            if cause == "identity_missing":
                return _stop(
                    "held",
                    "signing_identity_missing",
                    f"codesign found no identity {identity.common_name}",
                    item.name,
                )
            return _stop(
                "held",
                "signing_failed_unknown",
                "codesign failed for a cause that wasn't established",
                item.name,
            )
        try:
            refusal, facts = self._check(item, candidate)
        except ToolError:
            self.journal.append(key, FAILED, self.now())
            return _stop(
                "held",
                "signing_failed_unknown",
                "the signed candidate can't be read",
                item.name,
            )
        if refusal is not None:
            self.journal.append(key, FAILED, self.now())
            return _stop(
                "refused",
                refusal,
                "the signed candidate doesn't verify under its binding",
                item.name,
            )
        return self._record(item, candidate, facts, reconciled=False)

    def _record(
        self,
        item: Executable,
        candidate: Path,
        facts: dict[str, Any],
        *,
        reconciled: bool,
    ) -> dict[str, Any]:
        receipt = _receipt(
            self.loaded,
            item,
            candidate,
            facts,
            self.host,
            self.now,
            reconciled=reconciled,
        )
        self.work.write_receipt(item.name, receipt)
        self.journal.append(_key(self.loaded, item), SIGNED, self.now())
        return receipt


def _receipt(
    loaded: LoadedManifest,
    item: Executable,
    candidate: Path,
    facts: dict[str, Any],
    host: str,
    now: Clock,
    *,
    reconciled: bool,
) -> dict[str, Any]:
    return {
        "receipt_version": RECEIPT_VERSION,
        "step": "sign",
        "manifest_sha256": loaded.sha256,
        "product": loaded.signing.product,
        "name": item.name,
        "identifier": item.identifier,
        "audience": item.audience,
        "platform": item.platform,
        "policy": item.policy,
        "unsigned_sha256": item.unsigned_sha256,
        "signed_sha256": sha256_file(candidate),
        "certificate_sha1": facts["certificate_sha1"],
        "designated_requirement": facts["designated_requirement"],
        "entitlements_sha256": (
            None if item.entitlements == "none" else item.entitlements.sha256
        ),
        "signing_host": host,
        "signed_at": now().isoformat(),
        "reconciled": reconciled,
    }


def verify(
    loaded: LoadedManifest, work: WorkDir, toolchain: Toolchain, *, now: Clock = _now
) -> Outcome:
    """Read-only: every executable's signed candidate against its binding."""
    receipts: list[dict[str, Any]] = []
    executables = loaded.signing.executables
    for item in executables:
        stop = _unsupported(item, verifying=True)
        if stop is not None:
            return stop
        signed = work.read_receipt(item.name)
        if signed is None:
            others_signed = any(
                work.read_receipt(other.name) is not None
                for other in executables
                if other is not item
            )
            return _stop(
                "refused",
                "companion_unsigned" if others_signed else "not_signed",
                "an executable in this release has no sign receipt",
                item.name,
            )
        if signed.get("manifest_sha256") != loaded.sha256:
            return _stop(
                "refused",
                "receipt_mismatch",
                "the sign receipt is for another manifest",
                item.name,
            )
        candidate = work.candidate(item.name)
        if not _matches(candidate, str(signed.get("signed_sha256"))):
            return _stop(
                "refused",
                "signed_digest_mismatch",
                "the signed file's digest differs from its sign receipt",
                item.name,
            )
        original = loaded.resolve(item.path)
        if original.exists() and not _matches(original, item.unsigned_sha256):
            return _stop(
                "refused",
                "input_mismatch",
                "the unsigned original no longer matches the manifest",
                item.name,
            )
        try:
            refusal, facts = _check_signature(
                candidate,
                item,
                for_audience(item.audience),
                _expected_entitlements(loaded, item),
                toolchain,
            )
        except ToolError, OSError, ValueError:
            return _stop(
                "held",
                "verify_unreadable",
                "the signature could not be read",
                item.name,
            )
        if refusal is not None:
            return _stop(
                "refused", refusal, "the signed file doesn't verify", item.name
            )
        receipts.append(
            {
                "receipt_version": RECEIPT_VERSION,
                "step": "verify",
                "manifest_sha256": loaded.sha256,
                "name": item.name,
                "identifier": item.identifier,
                "signed_sha256": signed["signed_sha256"],
                "sign_receipt_sha256": hashlib.sha256(
                    work.receipt_path(item.name).read_bytes()
                ).hexdigest(),
                "certificate_sha1": facts["certificate_sha1"],
                "designated_requirement": facts["designated_requirement"],
                "verified_at": now().isoformat(),
            }
        )
    return Outcome("done", "verified", "every executable verifies", tuple(receipts))
