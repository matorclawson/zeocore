"""zeocore against ZEOconnect Broker contract 1.4.0 (zeoconnect #73, merge c134661e).

Two parts, as ZEOCORE-SOW-27 commits them:
- the parity fixtures: zeocore's digest vectors, which ZEO-RT's Go door checks
  against, recomputed here and checked against the frozen schemas, and
  zeoconnect's own conformance cases, checked against the same schemas;
- zeocore's client, which doesn't declare ``github-keymaster``: the three
  access refusals of base §9 in both of their forms.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from tests.test_integrations.hosted.test_transport import (
    NOW,
    request,
    response,
    session,
    transport,
)
from zeo_core.adapters.runtime_host.canonical import canonical_bytes
from zeo_core.integrations.hosted import (
    HostedClientError,
    HostedOperationResponse,
)
from zeo_core.integrations.hosted.client import (
    ACCESS_GUIDANCE,
    ACCESS_REFUSALS,
    HostedAccessRefusedError,
    HostedStoppedError,
    access_refusal_of,
    stop_of,
)

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "broker-1.4"
DIGESTED = ("connection_id", "operation_id", "arguments", "on_behalf_of")


def _json(name: str) -> Any:  # noqa: ANN401 - decoded JSON
    return json.loads((CONTRACT / name).read_text(encoding="utf-8"))


def _schemas() -> dict[str, Any]:
    return dict(_json("schemas.json"))


def _validator(schema_id: str) -> Draft202012Validator:
    schemas = _schemas()
    registry = Registry().with_resources(
        (key, Resource.from_contents(value, default_specification=DRAFT202012))
        for key, value in schemas.items()
    )
    return Draft202012Validator(schemas[schema_id], registry=registry)


# -- the vendored files ------------------------------------------------------------


def test_the_vendored_files_are_the_pinned_bytes() -> None:
    for line in (CONTRACT / "SHA256SUMS").read_text().splitlines():
        expected, name = line.split()
        assert hashlib.sha256((CONTRACT / name).read_bytes()).hexdigest() == expected


def test_the_annex_fixtures_are_zeoconnects_frozen_file() -> None:
    sums = (CONTRACT / "SHA256SUMS").read_text()
    assert "3394ad34aabfeae2e5cab717d8e31845be66eaa54915f60713cf8cb04c7d45ca" in sums


# -- the digest vectors: zeocore's parity fixtures for the door --------------------


VECTORS = _json("digest-vectors.json")["vectors"]


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_each_vector_is_a_valid_request_with_its_exact_digest(
    vector: dict[str, Any],
) -> None:
    _validator("zeoconnect:1.4:request").validate(vector["request"])
    raw = canonical_bytes({key: vector["request"][key] for key in DIGESTED})
    assert raw.decode("utf-8") == vector["canonical"]
    assert len(raw) == vector["canonical_bytes"]
    assert hashlib.sha256(raw).hexdigest() == vector["request_sha256"]


def test_there_is_a_vector_for_every_operation_and_the_digests_differ() -> None:
    operations = _schemas()["zeoconnect:1.4:request"]["properties"]["operation_id"]
    covered = {v["request"]["operation_id"] for v in VECTORS}
    assert covered == set(operations["enum"])
    digests = [v["request_sha256"] for v in VECTORS]
    assert len(set(digests)) == len(digests)


def test_pr_open_matches_zeoconnects_published_fixture() -> None:
    published = _json("1.4.0-annex-a.json")["digest"]
    (pr_open,) = [v for v in VECTORS if v["name"] == "github.pr.open"]
    assert pr_open["request_sha256"] == published["request_sha256"]
    assert pr_open["canonical_bytes"] == published["canonical_bytes"]


@pytest.mark.parametrize("field", ["door_request_id", "idempotency_key"])
def test_the_excluded_fields_never_change_the_digest(field: str) -> None:
    for vector in VECTORS:
        changed = copy.deepcopy(vector["request"])
        changed[field] = changed[field] + "-other-value"
        raw = canonical_bytes({key: changed[key] for key in DIGESTED})
        assert hashlib.sha256(raw).hexdigest() == vector["request_sha256"]


# -- zeoconnect's own conformance cases, against the same schemas -------------------


CASES = _json("1.4.0-annex-a.json")["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_zeoconnects_cases_validate_as_they_say(case: dict[str, Any]) -> None:
    valid = _validator(case["schema"]).is_valid(case["instance"])
    assert valid is case["valid"]


# -- zeocore's client: the three access refusals (base §9) ---------------------------


def _failed_safe(
    message: str, code: str = "REQUEST_REFUSED"
) -> HostedOperationResponse:
    return HostedOperationResponse.model_validate(
        {
            "status": "failed_safe",
            "execution_id": "exe-1",
            "normalized_error": {"code": code, "message": message},
        }
    )


@pytest.mark.parametrize("name", sorted(ACCESS_REFUSALS))
def test_an_invoke_refusal_is_named_and_is_not_a_stop(name: str) -> None:
    answer = _failed_safe(name)
    assert access_refusal_of(answer) == name
    assert stop_of(answer) is None


@pytest.mark.parametrize(
    "answer",
    [
        _failed_safe("door_required "),
        _failed_safe("Door_Required"),
        _failed_safe("door_required", code="FAILED_SAFE"),
        _failed_safe("stopped:dispatch:global"),
        HostedOperationResponse.model_validate(
            {"status": "confirmed", "execution_id": "exe-1", "result": "ok"}
        ),
    ],
)
def test_only_the_exact_message_is_an_access_refusal(
    answer: HostedOperationResponse,
) -> None:
    assert access_refusal_of(answer) is None


def test_the_guidance_names_the_way_back() -> None:
    assert "zeo" in ACCESS_GUIDANCE["door_required"]
    assert "zs-<product>" in ACCESS_GUIDANCE["app_identity_unverified"]
    assert "adopt" in ACCESS_GUIDANCE["no_principal"]


@pytest.mark.parametrize("name", sorted(ACCESS_REFUSALS))
def test_a_403_refusal_on_another_endpoint_raises_its_reason(name: str) -> None:
    calls: list[str] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        calls.append(http_request.url.path)
        return response(403, {"code": name}, request=http_request)

    hosted, store = transport(httpx.MockTransport(handler))
    store.save(session(NOW))
    with pytest.raises(HostedAccessRefusedError) as caught:
        hosted.list_connections(session(NOW))
    assert caught.value.reason == name
    assert str(caught.value) == ACCESS_GUIDANCE[name]
    assert calls == ["/v1/connections"]  # refused once, never retried


def test_a_403_stop_is_still_a_stop() -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        body = {"code": "stopped", "control": "dispatch", "scope": "global"}
        return response(403, body, request=http_request)

    hosted, store = transport(httpx.MockTransport(handler))
    store.save(session(NOW))
    with pytest.raises(HostedStoppedError):
        hosted.list_connections(session(NOW))


@pytest.mark.parametrize(
    "body", [{"code": "door_required_x"}, {"detail": "door_required"}]
)
def test_any_other_403_stays_a_plain_refusal(body: dict[str, str]) -> None:
    def handler(http_request: httpx.Request) -> httpx.Response:
        return response(403, body, request=http_request)

    hosted, store = transport(httpx.MockTransport(handler))
    store.save(session(NOW))
    with pytest.raises(HostedClientError) as caught:
        hosted.invoke(request("google.drive.file.download"))
    assert not isinstance(caught.value, HostedAccessRefusedError)
