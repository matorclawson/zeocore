"""The refusing port for authenticating the release event after the seal.

No mechanism for a JSON payload is adopted, so by default it holds; these
tests pin the controls around that refusal: the event is the unchanged
document, it comes after a verified seal, and what was authenticated is
exactly its payload.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from zeo_core.release.signing import Outcome
from zeo_core.release.signing.event_auth import (
    EVENT_SCHEMA,
    AuthorityUnavailableError,
    CanonicalEvent,
    EventRefusalError,
    authenticate_event,
    canonical,
    read_event,
)

ROOT = Path(__file__).resolve().parents[2]
VERIFIED = Outcome("done", "verified", "verified")


def _event(**changes: Any) -> dict[str, Any]:  # noqa: ANN401
    body: dict[str, Any] = {
        "schema": EVENT_SCHEMA,
        "repository": "org/demo",
        "tag": "v1.2.4",
        "tag_object": "1" * 40,
        "commit": "2" * 40,
        "tree": "3" * 40,
        "notes_sha256": "4" * 64,
        "artifacts": [{"path": "demo.whl", "size": 5, "sha256": "5" * 64}],
    }
    body.update(changes)
    digest = hashlib.sha256(canonical(body)).hexdigest()
    return {**body, "event_sha256": digest}


def _bytes(document: dict[str, Any]) -> bytes:
    return json.dumps(document, indent=2, sort_keys=True).encode()


class _Authenticator:
    def __init__(self, answer: str | None = None, *, down: bool = False) -> None:
        self.answer = answer
        self.down = down
        self.calls: list[CanonicalEvent] = []

    def authenticate(self, event: CanonicalEvent) -> str:
        self.calls.append(event)
        if self.down:
            raise AuthorityUnavailableError("the authority is unreachable")
        return event.event_sha256 if self.answer is None else self.answer


def test_the_codec_is_the_one_the_release_event_is_written_with() -> None:
    spec = importlib.util.spec_from_file_location(
        "release_event", ROOT / ".github" / "release" / "event.py"
    )
    assert spec is not None and spec.loader is not None
    written = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(written)
    body = {k: v for k, v in _event().items() if k != "event_sha256"}

    assert canonical(body) == written.canonical(body)  # unchanged, byte for byte
    assert written.SCHEMA == EVENT_SCHEMA


def test_a_golden_event_reads_back_to_its_exact_canonical_bytes() -> None:
    document = _event()
    event = read_event(_bytes(document))

    assert event.event_sha256 == document["event_sha256"]
    assert hashlib.sha256(event.payload).hexdigest() == event.event_sha256
    assert json.loads(event.payload) == {
        k: v for k, v in document.items() if k != "event_sha256"
    }
    assert b" " not in event.payload.replace(b"org/demo", b"")  # compact, sorted


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(commit="0" * 40),
        lambda d: d["artifacts"][0].update(sha256="6" * 64),
        lambda d: d.update(event_sha256="7" * 64),
        lambda d: d.update(extra=1),
        lambda d: d.pop("tree"),
        lambda d: d.update(schema="zeo.release-event/v2"),
    ],
    ids=["commit", "artifact", "digest", "extra field", "missing field", "schema"],
)
def test_an_event_that_is_not_the_unchanged_document_is_refused(
    change: Any,  # noqa: ANN401
) -> None:
    document = copy.deepcopy(_event())
    change(document)
    with pytest.raises(EventRefusalError):
        read_event(_bytes(document))


def _resealed(**changes: Any) -> dict[str, Any]:  # noqa: ANN401
    """A document whose digest is right for what it holds, but is not the schema."""
    body = {k: v for k, v in _event().items() if k != "event_sha256"}
    body.update(changes)
    body = {k: v for k, v in body.items() if v is not None}
    return {**body, "event_sha256": hashlib.sha256(canonical(body)).hexdigest()}


@pytest.mark.parametrize(
    "document",
    [_resealed(extra=1), _resealed(tree=None), _resealed(schema="other/v1")],
    ids=["extra field", "missing field", "other schema"],
)
def test_a_self_consistent_document_that_is_not_the_schema_is_refused(
    document: dict[str, Any],
) -> None:
    with pytest.raises(EventRefusalError):
        read_event(_bytes(document))


@pytest.mark.parametrize("content", [b"not json", b"[]", b"null"])
def test_an_event_that_is_not_an_object_is_refused(content: bytes) -> None:
    with pytest.raises(EventRefusalError):
        read_event(content)


def test_without_an_adopted_mechanism_the_port_holds() -> None:
    outcome = authenticate_event(_bytes(_event()), VERIFIED)
    assert (outcome.status, outcome.reason) == (
        "held",
        "event_authentication_unadopted",
    )


def test_an_authority_that_is_down_holds() -> None:
    outcome = authenticate_event(_bytes(_event()), VERIFIED, _Authenticator(down=True))
    assert (outcome.status, outcome.reason) == (
        "held",
        "event_authentication_unadopted",
    )


def test_an_authenticator_that_authenticated_this_payload_is_accepted() -> None:
    authenticator = _Authenticator()
    outcome = authenticate_event(_bytes(_event()), VERIFIED, authenticator)

    assert (outcome.status, outcome.reason) == ("done", "event_authenticated")
    assert len(authenticator.calls) == 1


def test_an_authenticator_that_authenticated_something_else_is_refused() -> None:
    outcome = authenticate_event(_bytes(_event()), VERIFIED, _Authenticator("9" * 64))
    assert (outcome.status, outcome.reason) == ("refused", "event_payload_mismatch")


@pytest.mark.parametrize(
    "sealed",
    [
        Outcome("done", "signed", "signed, not yet verified"),
        Outcome("refused", "not_signed", "not signed"),
        Outcome("held", "signing_host_mismatch", "elsewhere"),
    ],
    ids=["signed but unverified", "not signed", "held"],
)
def test_an_event_is_never_authenticated_ahead_of_a_verified_seal(
    sealed: Outcome,
) -> None:
    authenticator = _Authenticator()
    outcome = authenticate_event(_bytes(_event()), sealed, authenticator)

    assert (outcome.status, outcome.reason) == ("refused", "seal_unverified")
    assert authenticator.calls == []  # nothing was asked of the authority


def test_an_invalid_event_is_refused_before_the_authority_is_asked() -> None:
    authenticator = _Authenticator()
    tampered = _event()
    tampered["commit"] = "0" * 40

    outcome = authenticate_event(_bytes(tampered), VERIFIED, authenticator)

    assert (outcome.status, outcome.reason) == ("refused", "event_invalid")
    assert authenticator.calls == []
