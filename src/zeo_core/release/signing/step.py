"""``sign`` and ``verify``: two acts with separate receipts (ZEOCORE-SOW-19 §1).

A release counts as signed only if ``verify`` passes. ``sign`` runs only on
the configured signing host (SOW-22 §2) and signs a separate candidate, never
the unsigned original. ``verify`` is read-only and runs anywhere.

Built so far: internal Darwin releases, signed with ZEO Runtime Code Signing,
as single executables or as bundles. A bundle is signed inside-out on a copy:
every nested piece of code first, deepest first, then the bundle itself, and
never with ``--deep`` (ZEOCORE-SOW-32 §2).
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
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal

from .bundle import bundle_identifier
from .bundle import shape as code_shape
from .identity import (
    audience_of_certificate,
    designated_requirement,
    for_audience,
    names_identity,
)
from .journal import FAILED, INTENT, SIGNED, SigningJournal
from .manifest import (
    CURRENT,
    Audience,
    Bundle,
    Entitlements,
    Executable,
    LoadedManifest,
    MainExecutable,
)
from .toolchain import KeychainState, SignAnswerLostError, Toolchain, ToolError
from .tree import TreeError, copy_plain, listing, sha256_file, tree_sha256
from .tree import digest as tree_digest

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


class RecordInvalidError(ValueError):
    """A receipt or a reviewed entitlements file that can't be read as one."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _stop(
    status: Status, reason: str, message: str, name: str | None = None
) -> Outcome:
    return Outcome(status=status, reason=reason, message=message, executable=name)


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

    def bundle_candidate(self, bundle: Bundle) -> Path:
        """The signed copy keeps the bundle's own directory name."""
        return self.signed / bundle.name / PurePosixPath(bundle.path).name

    def receipt_path(self, name: str) -> Path:
        return self.receipts / f"sign-{name}.json"

    def read_receipt(self, name: str) -> dict[str, Any] | None:
        try:
            value = json.loads(self.receipt_path(name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise RecordInvalidError(
                "receipt_invalid", f"{name}: the sign receipt can't be read"
            ) from None
        if not isinstance(value, dict):
            raise RecordInvalidError(
                "receipt_invalid", f"{name}: the sign receipt is not an object"
            )
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
    loaded: LoadedManifest, name: str, entitlements: Entitlements | Literal["none"]
) -> dict[str, Any] | None:
    if entitlements == "none":
        return None
    try:
        value = plistlib.loads(loaded.resolve(entitlements.path).read_bytes())
    except Exception:  # plistlib raises several unrelated types
        value = None
    if not isinstance(value, dict):
        raise RecordInvalidError(
            "entitlements_invalid",
            f"{name}: the entitlements file is not a property-list dictionary",
        )
    if not value:
        # "none" is the one way to say no entitlements, so that a signature
        # carrying an entitlements blob, even an empty one, never matches it.
        raise RecordInvalidError(
            "entitlements_invalid",
            f"{name}: the entitlements file is empty; declare none instead",
        )
    return value


def _entitlements_match(
    loaded: LoadedManifest, entitlements: Entitlements | Literal["none"]
) -> bool:
    return entitlements == "none" or _matches(
        loaded.resolve(entitlements.path), entitlements.sha256
    )


def _check_signature(
    path: Path,
    identifier: str,
    audience: Audience,
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
    if owner is not None and owner != audience:
        return "wrong_audience", facts
    identity = for_audience(audience)
    expected = designated_requirement(identifier, identity)
    if (
        seen.leaf_sha1 != identity.certificate_sha1
        or seen.identifier != identifier
        or seen.designated != expected
    ):
        return "identity_mismatch", facts
    if not toolchain.satisfies(path, expected):
        return "not_signed", facts
    # Exact: "none" means no entitlements blob at all, and nothing undeclared
    # is accepted on any signed item, nested, main or outer.
    if seen.entitlements != expected_entitlements:
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


def _key(loaded: LoadedManifest, name: str) -> str:
    return f"{loaded.sha256}:{name}"


def _bundle_keys(loaded: LoadedManifest, bundle: Bundle) -> list[str]:
    return [_key(loaded, item.name) for item in bundle.nested] + [
        _key(loaded, bundle.name)
    ]


def _bundle_names_and_keys(
    loaded: LoadedManifest, bundle: Bundle
) -> list[tuple[str, str]]:
    names = [item.name for item in bundle.nested] + [bundle.name]
    return [(name, _key(loaded, name)) for name in names]


def _inside_out(bundle: Bundle) -> list[Any]:
    """Nested code deepest first, so nothing is signed before what it holds."""
    return sorted(
        bundle.nested,
        key=lambda item: (-len(PurePosixPath(item.path).parts), item.path),
    )


def _unsigned_bundle(loaded: LoadedManifest, bundle: Bundle) -> Outcome | None:
    """The refusal for an unsigned bundle's tree, before any signer is used."""
    source = loaded.resolve(bundle.path)
    try:
        entries = listing(source)
    except TreeError as error:
        return _stop("refused", "bundle_unsafe", str(error), bundle.name)
    except OSError:
        return _stop(
            "refused", "input_mismatch", "the bundle can't be read", bundle.name
        )
    if tree_digest(entries) != bundle.unsigned_tree_sha256:
        return _stop(
            "refused",
            "input_mismatch",
            "the bundle's tree differs from the manifest",
            bundle.name,
        )
    shape = _bundle_shape(bundle, source, entries)
    if shape is not None:
        return shape
    nested = _nested_unsigned(bundle, source)
    if nested is not None:
        return nested
    for owner, _, path, main in _mains(bundle):
        if sha256_file(source / path) != main.unsigned_sha256:
            return _stop(
                "refused",
                "input_mismatch",
                "a main executable's digest differs from the manifest",
                owner,
            )
    return None


def _nested_unsigned(bundle: Bundle, source: Path) -> Outcome | None:
    """Each nested item's unsigned digest against the manifest."""
    for item in bundle.nested:
        try:
            signed_sha256, tree = _code_digests(source / item.path)
        except TreeError as error:
            # Only if the tree changed after the whole bundle was listed: the
            # listing already refuses every link a nested subtree could hold.
            return _stop("refused", "bundle_unsafe", str(error), item.name)
        except OSError:
            return _stop(
                "refused", "input_mismatch", "a nested item can't be read", item.name
            )
        if (signed_sha256 or tree) != item.unsigned_sha256:
            return _stop(
                "refused",
                "input_mismatch",
                "a nested item's digest differs from the manifest",
                item.name,
            )
    return None


def _mains(bundle: Bundle) -> list[tuple[str, str, str, MainExecutable]]:
    """Each declared main executable: its bundle's name and identifier, and
    its path inside the outer bundle."""
    found = []
    if bundle.main_executable != "none":
        main = bundle.main_executable
        found.append((bundle.name, bundle.identifier, main.path, main))
    for item in bundle.nested:
        if item.main_executable != "none":
            main = item.main_executable
            found.append((item.name, item.identifier, f"{item.path}/{main.path}", main))
    return found


def _main_path(main: MainExecutable | Literal["none"]) -> str:
    return "" if main == "none" else main.path


def _main_record(
    candidate: Path, path: str, main: MainExecutable | Literal["none"]
) -> dict[str, Any] | None:
    if main == "none":
        return None
    return {
        "path": main.path,
        "unsigned_sha256": main.unsigned_sha256,
        "signed_sha256": sha256_file(candidate / path),
    }


def _bundle_shape(
    bundle: Bundle, root: Path, entries: tuple[Any, ...]
) -> Outcome | None:
    """Every piece of code listed, and the bundle identifier kept (SOW-32 §1)."""
    if bundle_identifier(root) != bundle.bundle_identifier:
        return _stop(
            "refused",
            "bundle_identifier_mismatch",
            f"Info.plist's CFBundleIdentifier is not {bundle.bundle_identifier}",
            bundle.name,
        )
    problem = code_shape(root, entries, bundle.main_executable, bundle.nested)
    if problem is not None:
        return _stop("refused", problem[0], problem[1], bundle.name)
    return None


def _check_bundle(
    bundle: Bundle,
    candidate: Path,
    entitlements: dict[str, dict[str, Any] | None],
    toolchain: Toolchain,
) -> tuple[Outcome | None, dict[str, dict[str, Any]]]:
    """A signed bundle against its binding: each nested item, then the whole.

    ``codesign --verify --strict --deep`` on the outer bundle confirms every
    nested signature and the sealed resources. ``--deep`` is used here, to
    verify, and never to sign (SOW-32 §3). A tool error raises ``ToolError``.
    """
    facts: dict[str, dict[str, Any]] = {}
    try:
        entries = listing(candidate)
    except TreeError as error:
        return _stop("refused", "bundle_unsafe", str(error), bundle.name), facts
    shape = _bundle_shape(bundle, candidate, entries)
    if shape is not None:
        return shape, facts
    for item in bundle.nested:
        refusal, facts[item.name] = _check_signature(
            candidate / item.path,
            item.identifier,
            bundle.audience,
            entitlements[item.name],
            toolchain,
        )
        if refusal is not None:
            return _stop(
                "refused", refusal, "a nested item doesn't verify", item.name
            ), facts
    refusal, facts[bundle.name] = _check_signature(
        candidate,
        bundle.identifier,
        bundle.audience,
        entitlements[bundle.name],
        toolchain,
    )
    # A main executable is verified once its bundle is signed, under the
    # bundle's identifier and with the bundle's entitlements.
    for owner, identifier, path, _ in _mains(bundle) if refusal is None else []:
        refusal, _ = _check_signature(
            candidate / path,
            identifier,
            bundle.audience,
            entitlements[owner],
            toolchain,
        )
        if refusal is not None:
            return _stop(
                "refused", refusal, "a main executable doesn't verify", owner
            ), facts
    if refusal is None and not toolchain.sealed(candidate):
        refusal = "not_signed"
    if refusal is not None:
        return _stop(
            "refused", refusal, "the signed bundle doesn't verify", bundle.name
        ), facts
    return None, facts


def _bundle_entitlements(
    loaded: LoadedManifest, bundle: Bundle
) -> dict[str, dict[str, Any] | None]:
    found = {
        item.name: _expected_entitlements(loaded, item.name, item.entitlements)
        for item in bundle.nested
    }
    found[bundle.name] = _expected_entitlements(
        loaded, bundle.name, bundle.entitlements
    )
    return found


def _code_digests(path: Path) -> tuple[str | None, str | None]:
    """A nested item's file digest, or its tree digest if it is a bundle."""
    if path.is_dir():
        return None, tree_sha256(path)
    return sha256_file(path), None


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
    stop = _executable_inputs(loaded, entitlements) or _bundle_inputs(
        loaded, entitlements
    )
    if stop is not None:
        return stop

    work.prepare()
    try:
        journal = SigningJournal(work.root / "journal.jsonl")
    except ValueError:
        # Which attempts are open can't be known, so nothing is signed.
        return _stop(
            "held", "journal_unreadable", "the signing journal has an unreadable line"
        )
    try:
        run = _Run(loaded, work, toolchain, journal, entitlements, signing_host, now)
        return run.sign_all()
    except RecordInvalidError as error:
        return _stop("refused", error.reason, str(error))
    finally:
        journal.close()


def _executable_inputs(
    loaded: LoadedManifest, entitlements: dict[str, dict[str, Any] | None]
) -> Outcome | None:
    for item in loaded.signing.executables:
        stop = _unsupported(item, verifying=False)
        if stop is not None:
            return stop
        files_match = _matches(
            loaded.resolve(item.path), item.unsigned_sha256
        ) and _entitlements_match(loaded, item.entitlements)
        if not files_match:
            return _stop(
                "refused",
                "input_mismatch",
                "an input is missing or its digest differs from the manifest",
                item.name,
            )
        try:
            entitlements[item.name] = _expected_entitlements(
                loaded, item.name, item.entitlements
            )
        except RecordInvalidError as error:
            return _stop("refused", error.reason, str(error), item.name)
    return None


def _bundle_inputs(
    loaded: LoadedManifest, entitlements: dict[str, dict[str, Any] | None]
) -> Outcome | None:
    for bundle in loaded.signing.bundles:
        stop = _unsigned_bundle(loaded, bundle)
        if stop is not None:
            return stop
        pinned = [bundle.entitlements, *(item.entitlements for item in bundle.nested)]
        if not all(_entitlements_match(loaded, each) for each in pinned):
            return _stop(
                "refused",
                "input_mismatch",
                "an entitlements file is missing or its digest differs",
                bundle.name,
            )
        try:
            entitlements.update(_bundle_entitlements(loaded, bundle))
        except RecordInvalidError as error:
            return _stop("refused", error.reason, str(error), bundle.name)
    return None


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
        settled = self._settle_all()
        if isinstance(settled, Outcome):
            return settled
        receipts, pending, bundles = settled
        if pending or bundles:
            ready = self._ready()
            if ready is not None:
                return ready
        for item in pending:
            signed = self._sign_one(item)
            if isinstance(signed, Outcome):
                return signed
            receipts.append(signed)
        for bundle in bundles:
            sealed = self._sign_bundle(bundle)
            if isinstance(sealed, Outcome):
                return sealed
            receipts.extend(sealed)
        what = "executable and bundle" if self.loaded.signing.bundles else "executable"
        return Outcome("done", "signed", f"every {what} is signed", tuple(receipts))

    def _settle_all(
        self,
    ) -> tuple[list[dict[str, Any]], list[Executable], list[Bundle]] | Outcome:
        """What earlier runs settled, and what is left to sign."""
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
        bundles: list[Bundle] = []
        for bundle in self.loaded.signing.bundles:
            done = self._settle_bundle(bundle)
            if isinstance(done, Outcome):
                return done
            if done is None:
                bundles.append(bundle)
            else:
                receipts.extend(done)
        return receipts, pending, bundles

    def _check(self, item: Executable, path: Path) -> tuple[str | None, dict[str, Any]]:
        return _check_signature(
            path,
            item.identifier,
            item.audience,
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
        if not self.journal.is_open(_key(self.loaded, item.name)):
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
        needed = [
            *((item.name, item.audience) for item in self.loaded.signing.executables),
            *((bundle.name, bundle.audience) for bundle in self.loaded.signing.bundles),
        ]
        for name, audience in needed:
            identity = for_audience(audience)
            if identity.certificate_sha1 not in available:
                return _stop(
                    "held",
                    "signing_identity_missing",
                    f"{identity.common_name} is not available to this user",
                    name,
                )
        return None

    def _sign_one(self, item: Executable) -> dict[str, Any] | Outcome:
        candidate = self.work.candidate(item.name)
        shutil.copy2(self.loaded.resolve(item.path), candidate)
        if not _matches(candidate, item.unsigned_sha256):
            return _stop(
                "refused", "input_mismatch", "the candidate copy differs", item.name
            )
        signed = self._sign_code(
            item.name,
            candidate,
            item.identifier,
            item.audience,
            item.entitlements,
        )
        if isinstance(signed, Outcome):
            return signed
        return self._record(item, candidate, signed, reconciled=False)

    def _sign_code(
        self,
        name: str,
        path: Path,
        identifier: str,
        audience: Audience,
        entitlements: Entitlements | Literal["none"],
    ) -> dict[str, Any] | Outcome:
        """One codesign run, journaled, and its result read back.

        Its intent is closed here only on a failure; a signature that verifies
        is closed when its receipt is written.
        """
        identity = for_audience(audience)
        key = _key(self.loaded, name)
        self.journal.append(key, INTENT, self.now())
        try:
            cause = self.toolchain.sign(
                path,
                identifier=identifier,
                identity=identity,
                requirement=designated_requirement(identifier, identity),
                entitlements=(
                    None
                    if entitlements == "none"
                    else self.loaded.resolve(entitlements.path)
                ),
                hardened=audience == "distributed",
            )
        except SignAnswerLostError:
            return _stop(
                "ambiguous",
                "signing_attempt_unreconciled",
                "codesign's answer was lost; reconcile before any new attempt",
                name,
            )
        if cause != "signed":
            self.journal.append(key, FAILED, self.now())
            if cause == "identity_missing":
                return _stop(
                    "held",
                    "signing_identity_missing",
                    f"codesign found no identity {identity.common_name}",
                    name,
                )
            return _stop(
                "held",
                "signing_failed_unknown",
                "codesign failed for a cause that wasn't established",
                name,
            )
        try:
            refusal, facts = _check_signature(
                path, identifier, audience, self.entitlements[name], self.toolchain
            )
        except ToolError:
            self.journal.append(key, FAILED, self.now())
            return _stop(
                "held",
                "signing_failed_unknown",
                "the signed candidate can't be read",
                name,
            )
        if refusal is not None:
            self.journal.append(key, FAILED, self.now())
            return _stop(
                "refused",
                refusal,
                "the signed candidate doesn't verify under its binding",
                name,
            )
        return facts

    def _settle_bundle(self, bundle: Bundle) -> list[dict[str, Any]] | Outcome | None:
        """An earlier run's receipts, a reconciled lost answer, or None to sign.

        A bundle is one unit: an open intent for any part of it, or a receipt
        for a part without the bundle's, is read back as a whole.
        """
        names = [item.name for item in bundle.nested] + [bundle.name]
        existing = {name: self.work.read_receipt(name) for name in names}
        if existing[bundle.name] is not None:
            found = [existing[name] for name in names]
            missing = [name for name in names if existing[name] is None]
            if missing:
                return _stop(
                    "refused",
                    "receipt_mismatch",
                    "the bundle has a sign receipt, but a nested item has none",
                    missing[0],
                )
            if all(
                receipt is not None
                and receipt.get("manifest_sha256") == self.loaded.sha256
                for receipt in found
            ):
                return [receipt for receipt in found if receipt is not None]
            return _stop(
                "refused",
                "receipt_mismatch",
                "this work directory holds a receipt for another manifest",
                bundle.name,
            )
        partial = any(
            self.journal.is_open(key) for key in _bundle_keys(self.loaded, bundle)
        ) or any(receipt is not None for receipt in existing.values())
        if not partial:
            return None
        candidate = self.work.bundle_candidate(bundle)
        try:
            refusal, facts = _check_bundle(
                bundle, candidate, self.entitlements, self.toolchain
            )
        except (ToolError, OSError):
            refusal, facts = _stop("held", "unreadable", "", bundle.name), {}
        if refusal is not None:
            return _stop(
                "ambiguous",
                "signing_attempt_unreconciled",
                "an earlier attempt's answer was lost and its bundle doesn't "
                "verify; reconcile it before any new attempt",
                bundle.name,
            )
        recorded = self._record_bundle(bundle, candidate, facts, reconciled=True)
        if isinstance(recorded, Outcome) and recorded.reason == "bundle_unsafe":
            # The copy can't be described, so it isn't this attempt's answer:
            # close what it left open, and the next run signs afresh.
            opened = [
                name
                for name, key in _bundle_names_and_keys(self.loaded, bundle)
                if self.journal.is_open(key)
            ]
            return self._close(opened, recorded)
        return recorded

    def _sign_bundle(self, bundle: Bundle) -> list[dict[str, Any]] | Outcome:
        """Sign a copy inside-out, then the bundle; never with ``--deep``."""
        candidate = self.work.bundle_candidate(bundle)
        if candidate.parent.exists():
            # Only an attempt that failed, and was closed as failed, leaves a
            # copy with no receipt and no open intent: start again from the
            # unsigned original.
            shutil.rmtree(candidate.parent)
        candidate.parent.mkdir(mode=0o700, parents=True)
        shutil.copytree(
            self.loaded.resolve(bundle.path),
            candidate,
            symlinks=True,
            copy_function=copy_plain,
        )
        try:
            copied = tree_sha256(candidate)
        except TreeError:
            copied = None
        if copied != bundle.unsigned_tree_sha256:
            return _stop(
                "refused", "input_mismatch", "the candidate copy differs", bundle.name
            )
        opened: list[str] = []
        for item in _inside_out(bundle):
            opened.append(item.name)
            signed = self._sign_code(
                item.name,
                candidate / item.path,
                item.identifier,
                bundle.audience,
                item.entitlements,
            )
            if isinstance(signed, Outcome):
                return self._close(opened[:-1], signed)
        opened.append(bundle.name)
        signed = self._sign_code(
            bundle.name,
            candidate,
            bundle.identifier,
            bundle.audience,
            bundle.entitlements,
        )
        if isinstance(signed, Outcome):
            return self._close(opened[:-1], signed)
        try:
            refusal, facts = _check_bundle(
                bundle, candidate, self.entitlements, self.toolchain
            )
        except (ToolError, OSError):
            refusal = _stop(
                "held",
                "signing_failed_unknown",
                "the signed bundle can't be read",
                bundle.name,
            )
        if refusal is not None:
            return self._close(opened, refusal)
        recorded = self._record_bundle(bundle, candidate, facts, reconciled=False)
        if isinstance(recorded, Outcome) and recorded.reason == "bundle_unsafe":
            return self._close(opened, recorded)
        return recorded

    def _close(self, names: list[str], outcome: Outcome) -> Outcome:
        """A bundle that failed cleanly closes every part it opened."""
        if outcome.status != "ambiguous":
            for name in names:
                self.journal.append(_key(self.loaded, name), FAILED, self.now())
        return outcome

    def _record_bundle(
        self,
        bundle: Bundle,
        candidate: Path,
        facts: dict[str, dict[str, Any]],
        *,
        reconciled: bool,
    ) -> list[dict[str, Any]] | Outcome:
        receipts: list[dict[str, Any]] = []
        nested: dict[str, str] = {}
        try:
            # Read before anything is written, so a refusal leaves no receipt.
            digests = {
                item.name: _code_digests(candidate / item.path)
                for item in bundle.nested
            }
            bundle_tree = tree_sha256(candidate)
        except (TreeError, OSError):
            return _stop(
                "refused",
                "bundle_unsafe",
                "the signed bundle's tree can't be described",
                bundle.name,
            )
        for item in bundle.nested:
            signed_sha256, signed_tree = digests[item.name]
            receipt = {
                **_common(self.loaded, item.name, item.identifier, bundle.audience),
                "bundle": bundle.name,
                "path": item.path,
                "unsigned_sha256": item.unsigned_sha256,
                "signed_sha256": signed_sha256,
                "signed_tree_sha256": signed_tree,
                "main_executable": _main_record(
                    candidate / item.path,
                    _main_path(item.main_executable),
                    item.main_executable,
                ),
                **_signed_by(facts[item.name], item.entitlements),
                **_signed_on(self.host, self.now, reconciled=reconciled),
            }
            existing = self.work.read_receipt(item.name)
            if existing is None:
                self.work.write_receipt(item.name, receipt)
                existing = receipt
            elif (
                existing.get("manifest_sha256") != self.loaded.sha256
                or existing.get("signed_sha256") != signed_sha256
                or existing.get("signed_tree_sha256") != signed_tree
            ):
                return _stop(
                    "ambiguous",
                    "signing_attempt_unreconciled",
                    "a nested receipt doesn't match the signed bundle",
                    item.name,
                )
            nested[item.name] = _receipt_sha256(self.work, item.name)
            receipts.append(existing)
        receipt = {
            **_common(self.loaded, bundle.name, bundle.identifier, bundle.audience),
            "bundle_identifier": bundle.bundle_identifier,
            "path": bundle.path,
            "unsigned_tree_sha256": bundle.unsigned_tree_sha256,
            "signed_tree_sha256": bundle_tree,
            "main_executable": _main_record(
                candidate, _main_path(bundle.main_executable), bundle.main_executable
            ),
            "nested_receipts": nested,
            **_signed_by(facts[bundle.name], bundle.entitlements),
            **_signed_on(self.host, self.now, reconciled=reconciled),
        }
        self.work.write_receipt(bundle.name, receipt)
        for key in _bundle_keys(self.loaded, bundle):
            self.journal.append(key, SIGNED, self.now())
        return [*receipts, receipt]

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
        self.journal.append(_key(self.loaded, item.name), SIGNED, self.now())
        return receipt


def _inventory_of(loaded: LoadedManifest) -> dict[str, Any]:
    """The inventory the pins came from, when they came from one (SOW-44 rev 2)."""
    if loaded.inventory_sha256 is None:
        return {}
    return {"inventory_sha256": loaded.inventory_sha256}


def _common(
    loaded: LoadedManifest, name: str, identifier: str, audience: Audience
) -> dict[str, Any]:
    return {
        "receipt_version": RECEIPT_VERSION,
        "step": "sign",
        "manifest_sha256": loaded.sha256,
        **_inventory_of(loaded),
        "product": loaded.signing.product,
        "name": name,
        "identifier": identifier,
        "audience": audience,
        "platform": "darwin",
        "policy": CURRENT,
    }


def _signed_by(
    facts: dict[str, Any], entitlements: Entitlements | Literal["none"]
) -> dict[str, Any]:
    return {
        "certificate_sha1": facts["certificate_sha1"],
        "designated_requirement": facts["designated_requirement"],
        "entitlements_sha256": None if entitlements == "none" else entitlements.sha256,
    }


def _signed_on(host: str, now: Clock, *, reconciled: bool) -> dict[str, Any]:
    return {
        "signing_host": host,
        "signed_at": now().isoformat(),
        "reconciled": reconciled,
    }


def _receipt_sha256(work: WorkDir, name: str) -> str:
    return hashlib.sha256(work.receipt_path(name).read_bytes()).hexdigest()


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
        **_inventory_of(loaded),
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
    try:
        return _verify_all(loaded, work, toolchain, now)
    except RecordInvalidError as error:
        return _stop("refused", error.reason, str(error))


def _unsigned_reason(loaded: LoadedManifest, work: WorkDir, name: str) -> str:
    """``companion_unsigned`` when another part of the release has a receipt."""
    signing = loaded.signing
    every = [
        *(item.name for item in signing.executables),
        *(bundle.name for bundle in signing.bundles),
        *(item.name for bundle in signing.bundles for item in bundle.nested),
    ]
    others = any(
        work.read_receipt(other) is not None for other in every if other != name
    )
    return "companion_unsigned" if others else "not_signed"


def _verify_all(
    loaded: LoadedManifest, work: WorkDir, toolchain: Toolchain, now: Clock
) -> Outcome:
    receipts: list[dict[str, Any]] = []
    for item in loaded.signing.executables:
        verified = _verify_executable(loaded, work, toolchain, item, now)
        if isinstance(verified, Outcome):
            return verified
        receipts.append(verified)
    for bundle in loaded.signing.bundles:
        sealed = _verify_bundle(loaded, work, toolchain, bundle, now)
        if isinstance(sealed, Outcome):
            return sealed
        receipts.extend(sealed)
    what = "executable and bundle" if loaded.signing.bundles else "executable"
    return Outcome("done", "verified", f"every {what} verifies", tuple(receipts))


def _verify_executable(
    loaded: LoadedManifest,
    work: WorkDir,
    toolchain: Toolchain,
    item: Executable,
    now: Clock,
) -> dict[str, Any] | Outcome:
    stop = _unsupported(item, verifying=True)
    if stop is not None:
        return stop
    signed = work.read_receipt(item.name)
    if signed is None:
        return _stop(
            "refused",
            _unsigned_reason(loaded, work, item.name),
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
    expected_entitlements = _expected_entitlements(loaded, item.name, item.entitlements)
    try:
        refusal, facts = _check_signature(
            candidate,
            item.identifier,
            item.audience,
            expected_entitlements,
            toolchain,
        )
    except (ToolError, OSError):
        return _stop(
            "held",
            "verify_unreadable",
            "the signature could not be read",
            item.name,
        )
    if refusal is not None:
        return _stop("refused", refusal, "the signed file doesn't verify", item.name)
    return {
        "receipt_version": RECEIPT_VERSION,
        "step": "verify",
        "manifest_sha256": loaded.sha256,
        "name": item.name,
        "identifier": item.identifier,
        "signed_sha256": signed["signed_sha256"],
        "sign_receipt_sha256": _receipt_sha256(work, item.name),
        "certificate_sha1": facts["certificate_sha1"],
        "designated_requirement": facts["designated_requirement"],
        "verified_at": now().isoformat(),
    }


def _bundle_trees(
    loaded: LoadedManifest, bundle: Bundle, candidate: Path, signed: dict[str, Any]
) -> str | Outcome:
    """The signed tree's digest, once it and the original match their records."""
    try:
        signed_tree: str | None = tree_sha256(candidate)
    except (TreeError, OSError):
        signed_tree = None
    if signed_tree is None or signed_tree != signed.get("signed_tree_sha256"):
        return _stop(
            "refused",
            "signed_digest_mismatch",
            "the signed bundle's tree differs from its sign receipt",
            bundle.name,
        )
    original = loaded.resolve(bundle.path)
    if original.exists() or original.is_symlink():
        try:
            unchanged = tree_sha256(original) == bundle.unsigned_tree_sha256
        except (TreeError, OSError):
            unchanged = False
        if not unchanged:
            return _stop(
                "refused",
                "input_mismatch",
                "the unsigned original no longer matches the manifest",
                bundle.name,
            )
    return signed_tree


Digests = tuple[str | None, str | None]


def _nested_receipts(
    loaded: LoadedManifest, work: WorkDir, bundle: Bundle, candidate: Path
) -> dict[str, Digests] | Outcome:
    """Each nested item's digests, as its receipt records them and as they are."""
    found: dict[str, Digests] = {}
    for item in bundle.nested:
        receipt = work.read_receipt(item.name)
        if receipt is None:
            return _stop(
                "refused",
                "companion_unsigned",
                "a nested item in this bundle has no sign receipt",
                item.name,
            )
        if receipt.get("manifest_sha256") != loaded.sha256:
            return _stop(
                "refused",
                "receipt_mismatch",
                "the sign receipt is for another manifest",
                item.name,
            )
        try:
            digests = _code_digests(candidate / item.path)
        except (TreeError, OSError):
            digests = (None, None)
        recorded = (receipt.get("signed_sha256"), receipt.get("signed_tree_sha256"))
        if digests == (None, None) or digests != recorded:
            return _stop(
                "refused",
                "signed_digest_mismatch",
                "a nested item's digest differs from its sign receipt",
                item.name,
            )
        found[item.name] = digests
    return found


def _mains_match(work: WorkDir, bundle: Bundle, candidate: Path) -> Outcome | None:
    for owner, _, path, main in _mains(bundle):
        recorded = (work.read_receipt(owner) or {}).get("main_executable")
        if recorded != _main_record(candidate, path, main):
            return _stop(
                "refused",
                "signed_digest_mismatch",
                "a main executable's digest differs from its sign receipt",
                owner,
            )
    return None


def _verify_bundle(
    loaded: LoadedManifest,
    work: WorkDir,
    toolchain: Toolchain,
    bundle: Bundle,
    now: Clock,
) -> list[dict[str, Any]] | Outcome:
    """Each nested item, the whole bundle, and every receipt's digest (SOW-32 §3)."""
    signed = work.read_receipt(bundle.name)
    if signed is None:
        return _stop(
            "refused",
            _unsigned_reason(loaded, work, bundle.name),
            "a bundle in this release has no sign receipt",
            bundle.name,
        )
    if signed.get("manifest_sha256") != loaded.sha256:
        return _stop(
            "refused",
            "receipt_mismatch",
            "the sign receipt is for another manifest",
            bundle.name,
        )
    candidate = work.bundle_candidate(bundle)
    signed_tree = _bundle_trees(loaded, bundle, candidate, signed)
    if isinstance(signed_tree, Outcome):
        return signed_tree
    nested = _nested_receipts(loaded, work, bundle, candidate)
    if isinstance(nested, Outcome):
        return nested
    mains = _mains_match(work, bundle, candidate)
    if mains is not None:
        return mains
    if signed.get("nested_receipts") != {
        name: _receipt_sha256(work, name) for name in nested
    }:
        return _stop(
            "refused",
            "receipt_mismatch",
            "the bundle's receipt doesn't name its nested receipts",
            bundle.name,
        )
    expected = _bundle_entitlements(loaded, bundle)
    try:
        refusal, facts = _check_bundle(bundle, candidate, expected, toolchain)
    except (ToolError, OSError):
        return _stop(
            "held", "verify_unreadable", "the signature could not be read", bundle.name
        )
    if refusal is not None:
        return refusal
    # Information only: an internal bundle isn't notarised, so Gatekeeper is
    # expected to reject it, and its answer never makes a pass or a failure.
    assessed = toolchain.assess(candidate)

    def verified(name: str, identifier: str, digests: Digests) -> dict[str, Any]:
        return {
            "receipt_version": RECEIPT_VERSION,
            "step": "verify",
            "manifest_sha256": loaded.sha256,
            "name": name,
            "identifier": identifier,
            "signed_sha256": digests[0],
            "signed_tree_sha256": digests[1],
            "sign_receipt_sha256": _receipt_sha256(work, name),
            "certificate_sha1": facts[name]["certificate_sha1"],
            "designated_requirement": facts[name]["designated_requirement"],
            "verified_at": now().isoformat(),
        }

    return [
        *(
            verified(item.name, item.identifier, nested[item.name])
            for item in bundle.nested
        ),
        {
            **verified(bundle.name, bundle.identifier, (None, signed_tree)),
            "spctl": {"status": assessed.status, "output": assessed.output},
        },
    ]
