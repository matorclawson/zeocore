"""Authenticating a private release event by a pinned Ed25519 key (SOW-44 amendment).

The public repository's ``release-event.json`` is authenticated by GitHub
attestation (``event_attestation``). A private repository has no attestation on
the current plan, so its event is signed by the Engine's release signer with a
key whose public half the CONSUMER pins. This module is the verifier side
only: it holds no key, makes no signature and reads no network.

The signed bytes are the domain string followed by the event's canonical
payload (``event_auth.read_event``), so a signature made for another purpose
or schema is not a signature of this one. The pin is a ``zeo.release-trust/v1``
file the caller names; where that file comes from (the installed copy, never a
candidate release) is the consumer's rule and is not something Core can see.
The clock is an injected callable: the command line passes the system UTC
clock and has no way to supply a time.

Three outcomes, as with the attestation check: ``authenticated``; ``refused``
(a check ran and failed); ``held`` (it could not complete: a missing file or
package). Nothing falls back to the attestation path or to an unauthenticated
event, and ``authenticated`` is never release authorization.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from .event_attestation import REPOSITORY as PUBLIC_REPOSITORY
from .event_attestation import Expected, parse_utc
from .event_auth import EventRefusalError, read_event
from .inventory import strict_json

SIGNATURE_SCHEMA: Final = "zeo.release-event-signature/v1"
TRUST_SCHEMA: Final = "zeo.release-trust/v1"
PURPOSE: Final = "release_event_signing/v1"
ALGORITHM: Final = "ed25519"
#: What is signed is ``DOMAIN`` followed by the event's canonical payload.
DOMAIN: Final = f"{SIGNATURE_SCHEMA}\n{PURPOSE}\n".encode()
MAX_SIGNATURE_BYTES: Final = 512
MAX_TRUST_BYTES: Final = 4096
MAX_EVENT_BYTES: Final = 1 << 20
MAX_KEYS: Final = 2
MAX_WINDOW: Final = timedelta(days=90)
#: The public key (hex) and ``key_id`` of EVERY seed that is a public constant of
#: the tests (anyone can sign with them). A trust file that pins any of them is
#: refused by ``verify_pinned_event``.
TEST_KEY_DENYLIST: Final = frozenset(
    {
        "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8",
        "rk_56475aa75463474c0285df5dbf2bcab7",
        "29acbae141bccaf0b22e1a94d34d0bc7361e526d0bfe12c89794bc9322966dd7",
        "rk_24f6ed6acbfe1009c030d7ca567c33ca",
    }
)
INSTALL_HINT: Final = "install the extra: pip install 'zeocore[release-keys]'"

_PUBLIC_MESSAGE: Final = (
    f"{PUBLIC_REPOSITORY} is authenticated only by attestation, never by a pinned key"
)
_SIGNATURE_FIELDS: Final = frozenset(
    {"schema", "purpose", "key_id", "algorithm", "event_sha256", "signature"}
)
_TRUST_FIELDS: Final = frozenset({"schema", "purpose", "repository", "keys"})
_KEY_FIELDS: Final = frozenset(
    {"key_id", "algorithm", "public_key", "valid_from", "valid_until"}
)
_HEX = frozenset("0123456789abcdef")
_REPOSITORY: Final = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
_KEY_ID: Final = re.compile(r"rk_[0-9a-f]{32}")


@dataclass(frozen=True)
class PinnedAuthentication:
    status: Literal["authenticated", "refused", "held"]
    reason: str
    message: str
    mechanism: Literal["pinned_key"] = "pinned_key"
    key_id: str | None = None
    tag: str | None = None
    commit: str | None = None


@dataclass(frozen=True)
class _Key:
    key_id: str
    public_key: str
    valid_from: datetime
    valid_until: datetime


class _Stop(Exception):  # noqa: N818 -- control flow carrying a finished answer
    def __init__(self, answer: PinnedAuthentication) -> None:
        super().__init__(answer.message)
        self.answer = answer


def _refuse(message: str, reason: str = "event_authentication_failed") -> _Stop:
    return _Stop(PinnedAuthentication("refused", reason, message))


def _hold(message: str) -> _Stop:
    return _Stop(
        PinnedAuthentication("held", "event_authentication_unavailable", message)
    )


def _is_public(repository: str) -> bool:
    """GitHub names are case-insensitive, so the refusal is too."""
    return repository.lower() == PUBLIC_REPOSITORY.lower()


def derive_key_id(public_key_hex: str) -> str:
    """``rk_`` and the first 32 hex characters of the sha256 of the 32 raw key bytes."""
    return "rk_" + hashlib.sha256(bytes.fromhex(public_key_hex)).hexdigest()[:32]


def signed_message(payload: bytes) -> bytes:
    return DOMAIN + payload


def _is_hex(value: object, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and set(value) <= _HEX


def _read(path: Path, what: str, limit: int) -> bytes:
    try:
        with path.open("rb") as handle:
            content = handle.read(limit + 1)
    except OSError as error:
        raise _hold(f"cannot read the {what}: {error.strerror or error}") from None
    if len(content) > limit:
        raise _refuse(f"the {what} is larger than {limit} bytes")
    return content


def _document(content: bytes, what: str) -> dict[str, object]:
    try:
        document = strict_json(content)
    except (ValueError, RecursionError):
        raise _refuse(f"the {what} is not valid JSON, or repeats a key") from None
    if not isinstance(document, dict):
        raise _refuse(f"the {what} is not a JSON object")
    return document


def _parse_key(entry: object, denylist: frozenset[str]) -> _Key:
    if not isinstance(entry, dict) or set(entry) != _KEY_FIELDS:
        raise _refuse("a trust key does not have exactly its fields")
    public_key = entry["public_key"]
    if entry["algorithm"] != ALGORITHM or not _is_hex(public_key, 64):
        raise _refuse("a trust key is not a 64-hex Ed25519 public key")
    assert isinstance(public_key, str)  # noqa: S101 -- narrowing after _is_hex
    key_id = derive_key_id(public_key)
    if entry["key_id"] != key_id:
        raise _refuse("a trust key's key_id is not derived from its public key")
    if public_key in denylist or key_id in denylist:
        raise _refuse("the trust file pins a test key", "trust_key_is_a_test_key")
    valid_from, valid_until = (
        parse_utc(entry["valid_from"]),
        parse_utc(entry["valid_until"]),
    )
    if valid_from is None or valid_until is None:
        raise _refuse(
            "a trust key's times are not RFC 3339 UTC with a Z, to the second"
        )
    if not valid_from < valid_until <= valid_from + MAX_WINDOW:
        raise _refuse(
            "a trust key's valid_until is not after valid_from within 90 days"
        )
    return _Key(key_id, public_key, valid_from, valid_until)


def _parse_trust(
    content: bytes, request_repository: str, denylist: frozenset[str]
) -> list[_Key]:
    """The pinned keys, or the refusal. The denylist is applied here, before any
    clock is read."""
    document = _document(content, "trust file")
    if set(document) != _TRUST_FIELDS:
        raise _refuse("the trust file does not have exactly its fields")
    if document["schema"] != TRUST_SCHEMA or document["purpose"] != PURPOSE:
        raise _refuse(f"the trust file is not {TRUST_SCHEMA} for {PURPOSE}")
    repository = document["repository"]
    if not isinstance(repository, str) or _REPOSITORY.fullmatch(repository) is None:
        raise _refuse("the trust file's repository is not one owner/name")
    if _is_public(repository):
        raise _refuse(_PUBLIC_MESSAGE)
    entries = document["keys"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_KEYS:
        raise _refuse(f"the trust file pins one or two keys, at most {MAX_KEYS}")
    keys = [_parse_key(entry, denylist) for entry in entries]
    if len({key.key_id for key in keys}) != len(keys):
        raise _refuse("the trust file pins the same key twice")
    if repository != request_repository:
        raise _refuse("the trust file's repository is not the repository asked for")
    return keys


def _parse_signature(
    content: bytes, event_sha256: str, keys: list[_Key]
) -> tuple[_Key, bytes]:
    document = _document(content, "signature file")
    if set(document) != _SIGNATURE_FIELDS:
        raise _refuse("the signature file does not have exactly its fields")
    if document["schema"] != SIGNATURE_SCHEMA:
        raise _refuse(f"the signature file's schema is {SIGNATURE_SCHEMA}")
    if document["purpose"] != PURPOSE:
        raise _refuse(f"the signature's purpose is {PURPOSE}")
    if document["algorithm"] != ALGORITHM:
        raise _refuse(f"the signature's algorithm is {ALGORITHM}")
    if document["event_sha256"] != event_sha256:
        raise _refuse("the signature is for another event")
    key_id, signature = document["key_id"], document["signature"]
    if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
        raise _refuse("the signature's key_id is not rk_ and 32 hex characters")
    if not _is_hex(signature, 128):
        raise _refuse("the signature is not 128 hex characters")
    assert isinstance(signature, str)  # noqa: S101 -- narrowing after _is_hex
    pinned = next((key for key in keys if key.key_id == key_id), None)
    if pinned is None:
        raise _refuse("the signature's key is not one the trust file pins")
    return pinned, bytes.fromhex(signature)


def _identify(
    content: bytes, repository: str, expected: Expected
) -> tuple[str, str, str]:
    """The event's (sha256, tag, commit), or the refusal."""
    try:
        event = read_event(content)
        body = json.loads(event.payload)
    except (EventRefusalError, ValueError, RecursionError) as error:
        raise _refuse(str(error), "event_invalid") from None
    if body.get("repository") != repository:
        raise _refuse(
            "the event is not for the repository asked for", "event_other_repository"
        )
    tag, commit = body.get("tag"), body.get("commit")
    if not isinstance(tag, str) or not _is_hex(commit, 40):
        raise _refuse("the event's tag or commit is malformed", "event_invalid")
    if (expected.tag and expected.tag != tag) or (
        expected.commit and expected.commit != commit
    ):
        raise _refuse("the event is not the release asked for", "event_not_expected")
    return event.event_sha256, tag, str(commit)


def _check_signature(public_key: str, message: bytes, signature: bytes) -> None:
    """Ed25519 verification. ``cryptography`` is imported here and only a missing
    package is caught: any other error is a defect and is not hidden."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise _hold(
            f"the cryptography package is not installed; {INSTALL_HINT}"
        ) from None
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(
            signature, message
        )
    except (InvalidSignature, ValueError):
        raise _refuse("the signature does not verify against the pinned key") from None


def _verify(
    *,
    event: Path,
    signature: Path,
    trust: Path,
    repository: str,
    expected: Expected,
    now: Callable[[], datetime],
    denylist: frozenset[str],
) -> PinnedAuthentication:
    try:
        if not expected.tag and not expected.commit:
            raise _refuse(
                "name the tag or the commit you expect; the event has no freshness",
                "expected_release_missing",
            )
        if _is_public(repository):
            raise _refuse(_PUBLIC_MESSAGE)
        if _REPOSITORY.fullmatch(repository) is None:
            raise _refuse("repository is one owner/name")
        keys = _parse_trust(
            _read(trust, "trust file", MAX_TRUST_BYTES), repository, denylist
        )
        event_bytes = _read(event, "event", MAX_EVENT_BYTES)
        digest, tag, commit = _identify(event_bytes, repository, expected)
        pinned, raw_signature = _parse_signature(
            _read(signature, "signature file", MAX_SIGNATURE_BYTES), digest, keys
        )
        moment = now()
        if not pinned.valid_from <= moment <= pinned.valid_until:
            raise _refuse("the pinned key is outside its validity window")
        _check_signature(
            pinned.public_key,
            signed_message(read_event(event_bytes).payload),
            raw_signature,
        )
    except _Stop as stop:
        return stop.answer
    return PinnedAuthentication(
        "authenticated",
        "event_authenticated",
        "the pinned key signed exactly these bytes; this is not release authorization",
        key_id=pinned.key_id,
        tag=tag,
        commit=commit,
    )


def verify_pinned_event(
    *,
    event: Path,
    signature: Path,
    trust: Path,
    repository: str,
    expected: Expected,
    now: Callable[[], datetime],
) -> PinnedAuthentication:
    """Authenticate the event at ``event`` against its detached signature and the
    caller's pin. ``now`` is the consumer's clock; the test key is refused."""
    return _verify(
        event=event,
        signature=signature,
        trust=trust,
        repository=repository,
        expected=expected,
        now=now,
        denylist=TEST_KEY_DENYLIST,
    )


def system_utc() -> datetime:
    return datetime.now(UTC)
