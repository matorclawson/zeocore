"""Authenticating a private release event by a pinned key (SOW-44 amendment).

The test keys' SEEDS exist only in this file: they are public constants of the
tests, never a real key. The denylist holds the derived PUBLIC values and a test
asserts they equal what the seed derives, so the list cannot drift. Every file
in ``fixtures/event_signature`` is public test data, built by ``build_fixtures``
below and held to it by a test, so no file can be edited by hand unnoticed.

The valid path is exercised through the private ``_verify`` with an empty
denylist; the public ``verify_pinned_event`` (which the verb calls) refuses the
test key, and named tests show it.
"""

from __future__ import annotations

import hashlib
import json
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from zeo_core.cli.protocol import EXIT_DONE, EXIT_HELD, EXIT_INVALID
from zeo_core.cli.release import make_command
from zeo_core.release.signing.event_attestation import Expected, GhResult
from zeo_core.release.signing.event_auth import EVENT_SCHEMA, canonical
from zeo_core.release.signing.event_signature import (
    DOMAIN,
    MAX_SIGNATURE_BYTES,
    MAX_TRUST_BYTES,
    PURPOSE,
    SIGNATURE_SCHEMA,
    TEST_KEY_DENYLIST,
    TRUST_SCHEMA,
    PinnedAuthentication,
    _verify,
    derive_key_id,
    signed_message,
    system_utc,
    verify_pinned_event,
)

FIXTURES = Path(__file__).parent / "fixtures" / "event_signature"
SEED = bytes(range(32))  # the public test key
SEED_TWO = bytes(range(32, 64))  # a second public test key, also on the denylist
REPO = "acme/private-engine"
TAG = "v1.2.3"
COMMIT = "b" * 40
FROM = "2026-01-01T00:00:00Z"
UNTIL = "2026-03-01T00:00:00Z"
NOW = datetime(2026, 2, 1, tzinfo=UTC)
EMPTY: frozenset[str] = frozenset()


def _public(seed: bytes) -> str:
    key = Ed25519PrivateKey.from_private_bytes(seed).public_key()
    return key.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()


def _unknown(n: int) -> str:
    """A public key no one holds a private key for (a hash, not a derived key):
    the 'non-test' entry of a fixture, which can authenticate nothing."""
    return hashlib.sha256(
        f"zeocore fixture key with no private half {n}".encode()
    ).hexdigest()


def _sign(seed: bytes, payload: bytes) -> str:
    return (
        Ed25519PrivateKey.from_private_bytes(seed).sign(signed_message(payload)).hex()
    )


def _event(**changes: Any) -> dict[str, Any]:  # noqa: ANN401
    body: dict[str, Any] = {
        "schema": EVENT_SCHEMA,
        "repository": REPO,
        "tag": TAG,
        "tag_object": "1" * 40,
        "commit": COMMIT,
        "tree": "3" * 40,
        "notes_sha256": "4" * 64,
        "artifacts": [{"path": "engine.tgz", "size": 5, "sha256": "5" * 64}],
    }
    body.update(changes)
    return {**body, "event_sha256": hashlib.sha256(canonical(body)).hexdigest()}


def _payload(event: dict[str, Any]) -> bytes:
    return canonical({k: v for k, v in event.items() if k != "event_sha256"})


def _json(document: object) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def _key_entry(seed: bytes, **changes: str) -> dict[str, str]:
    public = _public(seed)
    entry = {
        "key_id": derive_key_id(public),
        "algorithm": "ed25519",
        "public_key": public,
        "valid_from": FROM,
        "valid_until": UNTIL,
    }
    entry.update(changes)
    return entry


def _unknown_entry(n: int) -> dict[str, str]:
    return {
        **_unknown_fields(n),
        "algorithm": "ed25519",
        "valid_from": FROM,
        "valid_until": UNTIL,
    }


def _unknown_fields(n: int) -> dict[str, str]:
    public = _unknown(n)
    return {"public_key": public, "key_id": derive_key_id(public)}


def _trust(*entries: dict[str, str], **changes: Any) -> bytes:  # noqa: ANN401
    document: dict[str, Any] = {
        "schema": TRUST_SCHEMA,
        "purpose": PURPOSE,
        "repository": REPO,
        "keys": list(entries) or [_key_entry(SEED)],
    }
    document.update(changes)
    return _json(document)


def _signature(event: dict[str, Any], seed: bytes = SEED, **changes: Any) -> bytes:  # noqa: ANN401
    document: dict[str, Any] = {
        "schema": SIGNATURE_SCHEMA,
        "purpose": PURPOSE,
        "key_id": derive_key_id(_public(seed)),
        "algorithm": "ed25519",
        "event_sha256": event["event_sha256"],
        "signature": _sign(seed, _payload(event)),
    }
    document.update(changes)
    return _json(document)


def _padded(content: bytes, size: int) -> bytes:
    assert len(content) < size
    return content + b" " * (size - len(content))


def build_fixtures() -> dict[str, bytes]:
    """Every fixture file, by name. The directory must hold exactly these."""
    event = _event()
    flipped = bytearray.fromhex(_sign(SEED, _payload(event)))
    flipped[0] ^= 1
    other = _event(repository="acme/other-engine")
    changed = dict(event)
    changed["notes_sha256"] = "6" * 64  # the payload changes, event_sha256 does not
    three = [_key_entry(SEED), _key_entry(SEED_TWO), _unknown_entry(2)]
    fixtures: dict[str, bytes] = {
        "event-valid.json": _json(event),
        "event-changed-payload-byte.json": _json(changed),
        "event-other-repository.json": _json(other),
        "cross-language-vector.json": _json(
            {
                "event": event,
                "payload": _payload(event).hex(),
                "message": signed_message(_payload(event)).hex(),
                "public_key": _public(SEED),
                "key_id": derive_key_id(_public(SEED)),
                "signature": _sign(SEED, _payload(event)),
            }
        ),
        "signature-valid.json": _signature(event),
        "signature-flipped-bit.json": _signature(event, signature=flipped.hex()),
        "signature-wrong-purpose.json": _signature(event, purpose="other/v1"),
        "signature-wrong-algorithm.json": _signature(event, algorithm="rsa"),
        "signature-wrong-event-sha256.json": _signature(event, event_sha256="7" * 64),
        "signature-unknown-key-id.json": _signature(
            event, key_id=derive_key_id(_public(SEED_TWO))
        ),
        "signature-key-id-not-derived.json": _signature(event, key_id="rk_not-derived"),
        "signature-too-long.json": _padded(_signature(event), MAX_SIGNATURE_BYTES + 1),
        "signature-extra-field.json": _json(
            {**json.loads(_signature(event)), "x": "1"}
        ),
        "signature-duplicate-key.json": _signature(event)[:-1] + b',"purpose":"x"}',
        "trust-valid.json": _trust(),
        "trust-two-keys.json": _trust(_key_entry(SEED), _key_entry(SEED_TWO)),
        "trust-three-keys.json": _trust(*three),
        "trust-wrong-purpose.json": _trust(purpose="other/v1"),
        "trust-wrong-repository.json": _trust(repository="acme/other-engine"),
        "trust-public-repository.json": _trust(repository="zeroemployeeorg/zeocore"),
        "trust-valid-until-absent.json": _trust(
            {k: v for k, v in _key_entry(SEED).items() if k != "valid_until"}
        ),
        "trust-valid-until-91-days.json": _trust(
            _key_entry(SEED, valid_until="2026-04-02T00:00:00Z")
        ),
        "trust-valid-until-not-after-valid-from.json": _trust(
            _key_entry(SEED, valid_until=FROM)
        ),
        "trust-offset-time.json": _trust(
            _key_entry(SEED, valid_from="2026-01-01T00:00:00+00:00")
        ),
        "trust-fractional-time.json": _trust(
            _key_entry(SEED, valid_until="2026-03-01T00:00:00.5Z")
        ),
        "trust-key-id-mismatch.json": _trust(
            _key_entry(SEED, key_id=derive_key_id(_public(SEED_TWO)))
        ),
        # A key that is not a test key first and the test key second, so the
        # denylist is shown to look at every entry, not the first.
        "trust-denylisted-test-key.json": _trust(_unknown_entry(1), _key_entry(SEED)),
        "trust-too-long.json": _padded(_trust(), MAX_TRUST_BYTES + 1),
        "trust-extra-field.json": _json({**json.loads(_trust()), "x": "1"}),
        "trust-key-extra-field.json": _trust({**_key_entry(SEED), "x": "1"}),
        "trust-duplicate-key.json": _trust()[:-1] + b',"purpose":"x"}',
    }
    return fixtures


def _write(tmp_path: Path, name: str, content: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _run(
    tmp_path: Path,
    *,
    event: bytes | None = None,
    signature: bytes | None = None,
    trust: bytes | None = None,
    repository: str = REPO,
    expected: Expected = Expected(tag=TAG),  # noqa: B008 -- frozen, shared on purpose
    now: datetime = NOW,
    denylist: frozenset[str] = EMPTY,
    public: bool = False,
) -> PinnedAuthentication:
    document = _event()
    paths = {
        "event": _write(tmp_path, "release-event.json", event or _json(document)),
        "signature": _write(
            tmp_path, "release-event.sig.json", signature or _signature(document)
        ),
        "trust": _write(tmp_path, "trust.json", trust or _trust()),
    }
    if public:
        return verify_pinned_event(
            **paths, repository=repository, expected=expected, now=lambda: now
        )
    return _verify(
        **paths,
        repository=repository,
        expected=expected,
        now=lambda: now,
        denylist=denylist,
    )


def _fixture_run(
    tmp_path: Path, *, event: str = "event-valid.json", **names: str
) -> PinnedAuthentication:
    paths = {
        "event": FIXTURES / event,
        "signature": FIXTURES / names.pop("signature", "signature-valid.json"),
        "trust": FIXTURES / names.pop("trust", "trust-valid.json"),
    }
    return _verify(
        **paths,
        repository=names.pop("repository", REPO),
        expected=Expected(tag=TAG),
        now=lambda: NOW,
        denylist=EMPTY,
    )


# --- the fixtures are what this file builds, and nothing else ---


def test_the_fixture_directory_holds_exactly_the_named_files() -> None:
    assert {path.name for path in FIXTURES.iterdir()} == set(build_fixtures())


def test_each_fixture_is_what_this_file_builds() -> None:
    for name, content in build_fixtures().items():
        assert (FIXTURES / name).read_bytes() == content, name


def test_the_denylist_is_what_the_seed_derives() -> None:
    derived = {
        value
        for seed in (SEED, SEED_TWO)
        for value in (_public(seed), derive_key_id(_public(seed)))
    }
    assert derived == set(TEST_KEY_DENYLIST)
    for n in (1, 2):  # the keys with no private half are not test keys
        assert not set(_unknown_fields(n).values()) & TEST_KEY_DENYLIST


def test_the_cross_language_vector() -> None:
    """The signed bytes are spelled out so another language can reproduce them."""
    vector = json.loads((FIXTURES / "cross-language-vector.json").read_text())
    payload = bytes.fromhex(vector["payload"])
    assert bytes.fromhex(vector["message"]) == DOMAIN + payload
    assert DOMAIN == b"zeo.release-event-signature/v1\nrelease_event_signing/v1\n"
    assert payload == _payload(vector["event"])
    assert hashlib.sha256(payload).hexdigest() == vector["event"]["event_sha256"]
    assert (
        vector["key_id"]
        == "rk_" + hashlib.sha256(bytes.fromhex(vector["public_key"])).hexdigest()[:32]
    )
    # Ed25519 is deterministic, so the signature itself is part of the vector.
    assert vector["signature"] == _sign(SEED, payload)


# --- the valid path ---


def test_a_valid_event_is_authenticated(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert (result.status, result.reason) == ("authenticated", "event_authenticated")
    assert (result.mechanism, result.key_id) == (
        "pinned_key",
        derive_key_id(_public(SEED)),
    )
    assert (result.tag, result.commit) == (TAG, COMMIT)
    assert "not release authorization" in result.message


def test_the_fixture_files_authenticate_together(tmp_path: Path) -> None:
    assert _fixture_run(tmp_path).status == "authenticated"


def test_the_second_of_two_keys_authenticates(tmp_path: Path) -> None:
    document = _event()
    result = _run(
        tmp_path,
        trust=_trust(_key_entry(SEED), _key_entry(SEED_TWO)),
        signature=_signature(document, SEED_TWO),
    )
    assert result.status == "authenticated"
    assert result.key_id == derive_key_id(_public(SEED_TWO))


# --- the refusals, each by its fixture ---


@pytest.mark.parametrize(
    "names,fragment",
    [
        ({"event": "event-changed-payload-byte.json"}, "event_sha256"),
        ({"signature": "signature-flipped-bit.json"}, "does not verify"),
        ({"signature": "signature-wrong-purpose.json"}, "purpose"),
        ({"signature": "signature-wrong-algorithm.json"}, "algorithm"),
        ({"signature": "signature-wrong-event-sha256.json"}, "another event"),
        ({"signature": "signature-unknown-key-id.json"}, "not one the trust file pins"),
        ({"signature": "signature-key-id-not-derived.json"}, "key_id is not rk_"),
        ({"signature": "signature-too-long.json"}, "larger than 512"),
        ({"signature": "signature-extra-field.json"}, "exactly its fields"),
        ({"signature": "signature-duplicate-key.json"}, "repeats a key"),
        ({"trust": "trust-three-keys.json"}, "one or two keys"),
        ({"trust": "trust-wrong-purpose.json"}, "not zeo.release-trust/v1"),
        ({"trust": "trust-wrong-repository.json"}, "repository"),
        ({"trust": "trust-public-repository.json"}, "only by attestation"),
        ({"trust": "trust-valid-until-absent.json"}, "exactly its fields"),
        ({"trust": "trust-valid-until-91-days.json"}, "within 90 days"),
        ({"trust": "trust-valid-until-not-after-valid-from.json"}, "within 90 days"),
        ({"trust": "trust-offset-time.json"}, "RFC 3339 UTC"),
        ({"trust": "trust-fractional-time.json"}, "RFC 3339 UTC"),
        ({"trust": "trust-key-id-mismatch.json"}, "not derived"),
        ({"trust": "trust-too-long.json"}, "larger than 4096"),
        ({"trust": "trust-extra-field.json"}, "exactly its fields"),
        ({"trust": "trust-key-extra-field.json"}, "trust key does not have exactly"),
        ({"trust": "trust-duplicate-key.json"}, "repeats a key"),
    ],
)
def test_a_bad_file_is_refused(
    tmp_path: Path, names: dict[str, str], fragment: str
) -> None:
    result = _fixture_run(tmp_path, **names)
    assert result.status == "refused"
    assert result.reason == "event_authentication_failed" or names.get("event")
    assert fragment in result.message


def test_an_event_for_another_repository_is_refused(tmp_path: Path) -> None:
    result = _fixture_run(tmp_path, event="event-other-repository.json")
    assert (result.status, result.reason) == ("refused", "event_other_repository")


def test_the_trust_two_keys_fixture_is_accepted(tmp_path: Path) -> None:
    assert _fixture_run(tmp_path, trust="trust-two-keys.json").status == "authenticated"


def test_the_request_repository_must_equal_the_trust_repository(
    tmp_path: Path,
) -> None:
    other = "acme/other-engine"
    document = _event(repository=other)
    result = _run(
        tmp_path,
        event=_json(document),
        signature=_signature(document),
        repository=other,
    )
    assert result.status == "refused"
    assert "trust file's repository is not the repository asked for" in result.message


def test_the_public_repository_is_refused_in_the_verb_request(tmp_path: Path) -> None:
    document = _event(repository="zeroemployeeorg/zeocore")
    result = _run(
        tmp_path,
        event=_json(document),
        signature=_signature(document),
        repository="zeroemployeeorg/zeocore",
    )
    assert result.status == "refused"
    assert "only by attestation" in result.message


# --- the denylist ---


def test_the_public_function_refuses_the_test_key(tmp_path: Path) -> None:
    result = _run(tmp_path, public=True)
    assert (result.status, result.reason) == ("refused", "trust_key_is_a_test_key")


def test_the_denylist_refuses_either_entry_of_the_trust_file(tmp_path: Path) -> None:
    both = _trust(_unknown_entry(1), _key_entry(SEED))
    result = _run(tmp_path, trust=both, public=True)
    assert result.reason == "trust_key_is_a_test_key"
    assert _run(tmp_path, trust=both).status == "authenticated"  # empty denylist


def test_the_denylist_refuses_before_the_clock_is_read(tmp_path: Path) -> None:
    """Outside the key's window the answer is still the denylist's."""
    late = NOW + timedelta(days=400)
    result = _run(tmp_path, public=True, now=late)
    assert result.reason == "trust_key_is_a_test_key"


def test_the_public_function_takes_no_denylist_argument() -> None:
    import inspect

    assert "denylist" not in inspect.signature(verify_pinned_event).parameters


# --- the key window, at both ends, with the injected clock ---


@pytest.mark.parametrize(
    "now,ok",
    [
        (datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC), False),
        (datetime(2026, 1, 1, tzinfo=UTC), True),
        (datetime(2026, 3, 1, tzinfo=UTC), True),
        (datetime(2026, 3, 1, 0, 0, 1, tzinfo=UTC), False),
    ],
)
def test_the_key_window_at_both_ends(tmp_path: Path, now: datetime, ok: bool) -> None:
    result = _run(tmp_path, now=now)
    assert (result.status == "authenticated") is ok
    if not ok:
        assert "validity window" in result.message


# --- replay: the event has no freshness of its own ---


@pytest.mark.parametrize(
    "expected,reason",
    [
        (Expected(), "expected_release_missing"),
        (Expected(tag="v9.9.9"), "event_not_expected"),
        (Expected(commit="c" * 40), "event_not_expected"),
        (Expected(tag=TAG, commit="c" * 40), "event_not_expected"),
    ],
)
def test_the_event_must_be_the_one_asked_for(
    tmp_path: Path, expected: Expected, reason: str
) -> None:
    result = _run(tmp_path, expected=expected)
    assert (result.status, result.reason) == ("refused", reason)


def test_the_commit_alone_is_enough_to_name_the_release(tmp_path: Path) -> None:
    assert _run(tmp_path, expected=Expected(commit=COMMIT)).status == "authenticated"


# --- held: the check could not complete ---


@pytest.mark.parametrize("missing", ["event", "signature", "trust"])
def test_a_missing_file_is_held_never_authenticated(
    tmp_path: Path, missing: str
) -> None:
    document = _event()
    files = {
        "event": _json(document),
        "signature": _signature(document),
        "trust": _trust(),
    }
    paths = {k: _write(tmp_path, f"{k}.json", v) for k, v in files.items()}
    paths[missing] = tmp_path / "nothing-here"
    result = _verify(
        **paths,
        repository=REPO,
        expected=Expected(tag=TAG),
        now=lambda: NOW,
        denylist=EMPTY,
    )
    assert (result.status, result.reason) == (
        "held",
        "event_authentication_unavailable",
    )


def _block_cryptography(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [n for n in sys.modules if n.split(".")[0] == "cryptography"]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "cryptography", None)


def test_a_missing_package_is_held_with_the_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _block_cryptography(monkeypatch)
    result = _run(tmp_path)
    assert (result.status, result.reason) == (
        "held",
        "event_authentication_unavailable",
    )
    assert "zeocore[release-keys]" in result.message


def test_an_error_other_than_a_missing_package_is_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken(types.ModuleType):
        def __getattr__(self, name: str) -> object:
            raise RuntimeError("a defect, not a missing package")

    name = "cryptography.hazmat.primitives.asymmetric.ed25519"
    monkeypatch.setitem(sys.modules, name, Broken(name))
    with pytest.raises(RuntimeError, match="a defect"):
        _run(tmp_path)


# --- the verb ---


class _NoGh:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def __call__(self, argv: object) -> GhResult:
        self.calls.append(argv)
        raise AssertionError("a pinned_key request must never call gh")


def _verb(
    tmp_path: Path,
    fields: dict[str, str] | None = None,
    gh: _NoGh | None = None,
) -> tuple[int, dict[str, Any]]:
    document = _event()
    body = {
        "event": str(_write(tmp_path, "e.json", _json(document))),
        "signature": str(_write(tmp_path, "s.json", _signature(document))),
        "trust": str(_write(tmp_path, "t.json", _trust())),
        "mechanism": "pinned_key",
        "repository": REPO,
        "tag": TAG,
    }
    body.update(fields or {})
    status, answer = make_command(gh=gh or _NoGh())(
        ["verify-event"], json.dumps(body).encode()
    )
    return status, dict(answer)


def test_the_verb_refuses_the_test_key_and_never_calls_gh(tmp_path: Path) -> None:
    gh = _NoGh()
    status, answer = _verb(tmp_path, gh=gh)
    assert (status, answer["status"], answer["outcome"]) == (
        EXIT_HELD,
        "refused",
        "trust_key_is_a_test_key",
    )
    assert answer["mechanism"] == "pinned_key"
    assert gh.calls == []


def test_the_verb_takes_no_time_and_uses_the_system_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zeo_core.cli.release as verb

    seen: list[datetime] = []

    def fake(**kw: Any) -> PinnedAuthentication:  # noqa: ANN401
        seen.append(kw["now"]())
        assert kw["now"] is system_utc
        return PinnedAuthentication(
            "authenticated", "event_authenticated", "m", key_id="rk_x", tag=TAG
        )

    monkeypatch.setattr(verb, "verify_pinned_event", fake)
    status, answer = _verb(tmp_path)
    assert (status, answer["key_id"], answer["mechanism"]) == (
        EXIT_DONE,
        "rk_x",
        "pinned_key",
    )
    assert abs((seen[0] - datetime.now(UTC)).total_seconds()) < 60
    status, answer = _verb(tmp_path, {"now": "2026-02-01T00:00:00Z"})
    assert status == EXIT_INVALID


@pytest.mark.parametrize(
    "request_fields",
    [
        {"mechanism": "pinned-key"},
        {"mechanism": "anything"},
    ],
)
def test_an_unknown_mechanism_is_an_invalid_request(
    tmp_path: Path, request_fields: dict[str, str]
) -> None:
    status, answer = _verb(tmp_path, request_fields)
    assert (status, answer["outcome"]) == (EXIT_INVALID, "invalid_request")
    assert "github_attestation or pinned_key" in answer["message"]


@pytest.mark.parametrize("field", ["signature", "trust", "repository"])
def test_a_key_field_without_the_mechanism_is_an_invalid_request(
    tmp_path: Path, field: str
) -> None:
    path = _write(tmp_path, "event.json", _json(_event()))
    for mechanism in ({}, {"mechanism": "github_attestation"}):
        body = {"event": str(path), "tag": TAG, field: "x", **mechanism}
        status, answer = make_command(gh=_NoGh())(
            ["verify-event"], json.dumps(body).encode()
        )
        assert status == EXIT_INVALID, (field, mechanism)
        assert "pinned_key" in answer["message"]


@pytest.mark.parametrize("missing", ["event", "signature", "trust", "repository"])
def test_a_pinned_key_request_needs_every_field(tmp_path: Path, missing: str) -> None:
    document = _event()
    body = {
        "event": str(_write(tmp_path, "e.json", _json(document))),
        "signature": str(_write(tmp_path, "s.json", _signature(document))),
        "trust": str(_write(tmp_path, "t.json", _trust())),
        "mechanism": "pinned_key",
        "repository": REPO,
    }
    del body[missing]
    status, _ = make_command(gh=_NoGh())(["verify-event"], json.dumps(body).encode())
    assert status == EXIT_INVALID


def test_the_attestation_mechanism_may_be_named_and_is_unchanged(
    tmp_path: Path,
) -> None:
    from zeo_core.release.signing.event_attestation import REPOSITORY

    body = _event(repository=REPOSITORY)
    path = _write(tmp_path, "e.json", _json(body))
    called: list[object] = []

    def gh(argv: object) -> GhResult:
        called.append(argv)
        return GhResult(1, "", "Error: no attestations found")

    request = {"event": str(path), "tag": TAG, "mechanism": "github_attestation"}
    status, answer = make_command(gh=gh)(["verify-event"], json.dumps(request).encode())
    assert called, "the attestation path still runs gh"
    assert (status, answer["outcome"]) == (
        EXIT_HELD,
        "event_authentication_unavailable",
    )


# --- guards the first review found unexercised ---


def test_the_denylist_fixture_is_refused_by_the_public_function_only(
    tmp_path: Path,
) -> None:
    event = FIXTURES / "event-valid.json"
    signature = FIXTURES / "signature-valid.json"
    trust = FIXTURES / "trust-denylisted-test-key.json"
    public = verify_pinned_event(
        event=event,
        signature=signature,
        trust=trust,
        repository=REPO,
        expected=Expected(tag=TAG),
        now=lambda: NOW,
    )
    assert (public.status, public.reason) == ("refused", "trust_key_is_a_test_key")
    private = _verify(
        event=event,
        signature=signature,
        trust=trust,
        repository=REPO,
        expected=Expected(tag=TAG),
        now=lambda: NOW,
        denylist=EMPTY,
    )
    assert private.status == "authenticated"


def test_the_second_test_key_is_on_the_denylist(tmp_path: Path) -> None:
    both = _trust(_key_entry(SEED_TWO), _unknown_entry(1))
    result = _run(tmp_path, trust=both, public=True)
    assert result.reason == "trust_key_is_a_test_key"


def test_a_signature_of_127_hex_characters_is_refused(tmp_path: Path) -> None:
    document = _event()
    short = _sign(SEED, _payload(document))[:-1]
    result = _run(tmp_path, signature=_signature(document, signature=short))
    assert (result.status, "128 hex" in result.message) == ("refused", True)


def test_a_non_hex_public_key_is_refused(tmp_path: Path) -> None:
    entry = _key_entry(SEED, public_key="Z" * 64)
    result = _run(tmp_path, trust=_trust(entry))
    assert result.status == "refused"
    assert "64-hex Ed25519" in result.message


def test_the_same_key_twice_is_refused(tmp_path: Path) -> None:
    result = _run(tmp_path, trust=_trust(_key_entry(SEED), _key_entry(SEED)))
    assert result.status == "refused"
    assert "same key twice" in result.message


@pytest.mark.parametrize(
    "until,ok",
    [("2026-04-01T00:00:00Z", True), ("2026-04-01T00:00:01Z", False)],
)
def test_the_exact_90_day_boundary(tmp_path: Path, until: str, ok: bool) -> None:
    trust = _trust(_key_entry(SEED, valid_until=until))
    result = _run(tmp_path, trust=trust)
    assert (result.status == "authenticated") is ok
    if not ok:
        assert "within 90 days" in result.message


def test_a_trust_file_that_is_not_an_object_or_not_json_is_refused(
    tmp_path: Path,
) -> None:
    for content in (b"[]", b"not json", b'{"a":' * 3000):
        result = _run(tmp_path, trust=content[:4096])
        assert result.status == "refused", content[:20]


def test_a_deeply_nested_event_is_refused_not_a_crash(tmp_path: Path) -> None:
    nested = b"[" * 20000 + b"]" * 20000
    result = _run(tmp_path, event=nested)
    assert result.status == "refused"
    assert result.reason in {"event_invalid", "event_authentication_failed"}


@pytest.mark.parametrize("name", ["ZeroEmployeeOrg/ZeoCore", "zeroemployeeorg/ZEOCORE"])
def test_the_public_repository_refusal_ignores_case(tmp_path: Path, name: str) -> None:
    result = _run(tmp_path, repository=name)
    assert result.status == "refused"
    assert "only by attestation" in result.message
    trust = _trust(repository=name)
    assert "only by attestation" in _run(tmp_path, trust=trust).message


def test_a_repository_that_is_not_owner_name_is_refused(tmp_path: Path) -> None:
    result = _run(tmp_path, repository="no-slash")
    assert (result.status, "owner/name" in result.message) == ("refused", True)


def test_a_trust_file_with_the_wrong_schema_or_zero_keys_is_refused(
    tmp_path: Path,
) -> None:
    assert _run(tmp_path, trust=_trust(schema="x")).status == "refused"
    empty = json.loads(_trust())
    empty["keys"] = []
    assert _run(tmp_path, trust=_json(empty)).status == "refused"
    bad_repo = _trust(repository="not a repo")
    assert "owner/name" in _run(tmp_path, trust=bad_repo).message


def test_an_event_with_a_malformed_tag_or_commit_is_refused(tmp_path: Path) -> None:
    document = _event(commit="xyz")
    result = _run(
        tmp_path,
        event=_json(document),
        signature=_signature(document),
        expected=Expected(tag=TAG),
    )
    assert (result.status, result.reason) == ("refused", "event_invalid")


def test_a_signature_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    assert _run(tmp_path, signature=b"[]").status == "refused"
    wrong = _signature(_event(), schema="zeo.other/v1")
    assert "schema" in _run(tmp_path, signature=wrong).message
