"""The port that would authenticate the release event after the seal (SOW-44 rev 2, b).

Order: seal first (``verify`` passes), then the release event is made from the
signed hashes, then the event is authenticated. This module is the verifier
side of that last step and nothing more: it checks that the event is the
unchanged ``zeo.release-event/v1`` document and that it comes after a verified
seal.

It never answers "authenticated". The producer's only adopted authentication
is a code signature (``codesign``) and the tailnet identity of a caller;
neither covers a JSON payload, and using a signing key for one is a new key
purpose, which is the operator's decision. So whatever else is true, the
answer for a well-formed event after a verified seal is ``held``, and there is
no way to inject an authenticator that turns it into ``done``: a returned hash
is not an authentication. When a mechanism is adopted, the check that replaces
this one verifies a signature against a pinned key. Nothing here creates a
key, a service or a second signer, and nothing re-encodes the event: the codec
is the one in ``.github/release/event.py``, byte for byte.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Final, Literal

from .inventory import strict_json
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


@dataclass(frozen=True)
class CanonicalEvent:
    #: The canonical bytes: every field but ``event_sha256``, sorted keys, no spaces.
    payload: bytes
    event_sha256: str


@dataclass(frozen=True)
class EventOutcome:
    status: Literal["refused", "held"]
    reason: str
    message: str


def canonical(body: dict[str, Any]) -> bytes:
    """The codec of ``.github/release/event.py``; it is not redefined here."""
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def read_event(content: bytes) -> CanonicalEvent:
    """The event's canonical bytes, or why it is not the unchanged document."""
    try:
        document = strict_json(content)
    except ValueError:
        raise EventRefusalError(
            "the event is not valid JSON, or repeats a key"
        ) from None
    if not isinstance(document, dict) or set(document) != _FIELDS:
        raise EventRefusalError("the event does not have exactly its fields")
    if document["schema"] != EVENT_SCHEMA:
        raise EventRefusalError(f"the event's schema is {EVENT_SCHEMA}")
    claimed = document.pop("event_sha256")
    payload = canonical(document)
    if not isinstance(claimed, str) or hashlib.sha256(payload).hexdigest() != claimed:
        raise EventRefusalError("event_sha256 is not the digest of the payload")
    return CanonicalEvent(payload=payload, event_sha256=claimed)


def authenticate_event(content: bytes, sealed: Outcome) -> EventOutcome:
    """Check the event after the seal; say that nothing can authenticate it yet.

    ``sealed`` is the outcome of ``verify`` on the seal: a seal that is not
    verified is not a seal, and an event is never looked at ahead of it. A
    well-formed event is then ``held``: no mechanism is adopted.
    """
    if (sealed.status, sealed.reason) != ("done", "verified"):
        return EventOutcome(
            "refused",
            "seal_unverified",
            "the event is authenticated only after the seal has verified",
        )
    try:
        read_event(content)
    except EventRefusalError as error:
        return EventOutcome("refused", "event_invalid", str(error))
    return EventOutcome(
        "held",
        "event_authentication_unadopted",
        "no mechanism for authenticating a release event payload is adopted",
    )
