"""The port that would authenticate the release event after the seal (SOW-44 rev 2, b).

Order: seal first (``verify`` passes), then the release event is made from the
signed hashes, then the event is authenticated. This module is the verifier
side of that last step and nothing more: it checks that the event is the
unchanged ``zeo.release-event/v1`` document, that it comes after a verified
seal, and that whatever authenticated it authenticated exactly these
canonical bytes.

What it does NOT do is sign. The producer's only adopted authentication is a
code signature (``codesign``) and the tailnet identity of a caller; neither
covers a JSON payload, and using a signing key for one is a new key purpose,
which is the operator's decision. So the default authenticator, ``Unadopted``,
refuses, and the port reports "held" until a mechanism is adopted. Nothing
here creates a key, a service or a second signer, and nothing re-encodes the
event: the codec is the one in ``.github/release/event.py``, byte for byte.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from .step import Outcome

EVENT_SCHEMA: Final = "zeo.release-event/v1"
_FIELDS: Final = frozenset(
    {
        "schema",
        "repository",
        "tag",
        "tag_object",
        "commit",
        "tree",
        "notes_sha256",
        "artifacts",
        "event_sha256",
    }
)


class EventRefusalError(ValueError):
    """The event is not the unchanged canonical document."""


class AuthorityUnavailableError(RuntimeError):
    """No adopted mechanism can authenticate the payload, or it is unreachable."""


@dataclass(frozen=True)
class CanonicalEvent:
    #: The canonical bytes: every field but ``event_sha256``, sorted keys, no spaces.
    payload: bytes
    event_sha256: str


class EventAuthenticator(Protocol):
    def authenticate(self, event: CanonicalEvent) -> str:
        """The sha256 of the payload it authenticated.

        Raises ``AuthorityUnavailableError`` when it cannot authenticate at
        all; any other answer must be the digest of exactly these bytes.
        """
        ...


class Unadopted:
    """The default: no mechanism for a release event payload has been adopted."""

    def authenticate(self, event: CanonicalEvent) -> str:
        raise AuthorityUnavailableError(
            "no mechanism for authenticating a release event payload is adopted"
        )


@dataclass(frozen=True)
class EventOutcome:
    status: Literal["done", "refused", "held"]
    reason: str
    message: str


def canonical(body: dict[str, Any]) -> bytes:
    """The codec of ``.github/release/event.py``; it is not redefined here."""
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def read_event(content: bytes) -> CanonicalEvent:
    """The event's canonical bytes, or why it is not the unchanged document."""
    try:
        document = json.loads(content)
    except ValueError:
        raise EventRefusalError("the event is not valid JSON") from None
    if not isinstance(document, dict) or set(document) != _FIELDS:
        raise EventRefusalError("the event does not have exactly its fields")
    if document["schema"] != EVENT_SCHEMA:
        raise EventRefusalError(f"the event's schema is {EVENT_SCHEMA}")
    claimed = document.pop("event_sha256")
    payload = canonical(document)
    if not isinstance(claimed, str) or hashlib.sha256(payload).hexdigest() != claimed:
        raise EventRefusalError("event_sha256 is not the digest of the payload")
    return CanonicalEvent(payload=payload, event_sha256=claimed)


def authenticate_event(
    content: bytes,
    sealed: Outcome,
    authenticator: EventAuthenticator | None = None,
) -> EventOutcome:
    """Authenticate the event after the seal, or say exactly why not.

    ``sealed`` is the outcome of ``verify`` on the seal: a seal that is not
    verified is not a seal, and an event is never authenticated ahead of it.
    """
    if (sealed.status, sealed.reason) != ("done", "verified"):
        return EventOutcome(
            "refused",
            "seal_unverified",
            "the event is authenticated only after the seal has verified",
        )
    try:
        event = read_event(content)
    except EventRefusalError as error:
        return EventOutcome("refused", "event_invalid", str(error))
    try:
        authenticated = (authenticator or Unadopted()).authenticate(event)
    except AuthorityUnavailableError as error:
        return EventOutcome("held", "event_authentication_unadopted", str(error))
    if authenticated != event.event_sha256:
        return EventOutcome(
            "refused",
            "event_payload_mismatch",
            "what was authenticated is not this event's payload",
        )
    return EventOutcome("done", "event_authenticated", "the event is authenticated")
