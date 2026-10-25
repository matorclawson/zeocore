"""Explicit successor composition; fakes only, never native qualification."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
import rfc8785

from zeo_core.integrations.hosted.client import HostedClientError
from zeo_core.integrations.model_ops.execution import (
    ModelExecutionAmbiguousError,
    PreparedModelCall,
)
from zeo_core.integrations.model_ops.fenced_execution import (
    FencedModelOperationExecutor,
)
from zeo_core.integrations.model_ops.fenced_receipts import DRAFT_STAMP

from .test_execution import CONTENT, Ports, configuration
from .test_execution import setup as legacy_setup
from .test_fenced_receipts import no_call


def setup() -> tuple[FencedModelOperationExecutor, PreparedModelCall, Ports]:
    _, prepared, ports = legacy_setup()
    ports.response["receipt"]["draft_schema"] = DRAFT_STAMP
    ports.response["receipt"]["binding"] = {
        "connection_id": "con_synthetic",
        "connector_revision": "synthetic-connector",
        "external_identity": "synthetic-account",
        "connection_revision": "1",
    }
    return FencedModelOperationExecutor(**configuration(ports)), prepared, ports


def test_order_and_actual_retained_original_binding() -> None:
    executor, prepared, ports = setup()
    result = executor.execute(prepared)
    assert result.completion == CONTENT
    assert result.parsed.fence_outcome == "matched"
    assert result.prepared is prepared
    assert ports.events == [
        "retain:request",
        "admit",
        "invoke",
        "retain:response_normalized",
        "fetch",
        "retain:completion",
    ]
    assert ports.evidence["request"] == prepared.request_bytes
    assert ports.requests[0].model_dump(exclude_none=True) == json.loads(
        prepared.request_bytes
    )
    assert len(ports.requests) == 1


@pytest.mark.parametrize(
    "failure",
    [
        "request",
        "digest",
        "admit",
        "admission_binding",
        "admission_key",
        "admission_reference",
    ],
)
def test_before_send_failure_sends_nothing(failure: str) -> None:
    executor, prepared, ports = setup()
    ports.fail = failure
    with pytest.raises(HostedClientError, match="no dispatch") as caught:
        executor.execute(prepared)
    assert not ports.requests and not ports.fetched
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize(
    "failure",
    [
        "invoke",
        "response_normalized",
        "fetch",
        "completion",
    ],
)
def test_uncertain_failure_keeps_original_key_without_retry(failure: str) -> None:
    executor, prepared, ports = setup()
    ports.fail = failure
    with pytest.raises(ModelExecutionAmbiguousError) as caught:
        executor.execute(prepared)
    assert caught.value.prepared is prepared
    assert len(ports.requests) == 1
    assert ports.requests[0].idempotency_key == prepared.idempotency_key
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize(
    "field",
    [
        "connection_id",
        "connector_revision",
        "external_identity",
        "connection_revision",
    ],
)
def test_wrong_actual_binding_is_held_before_artifact_fetch(field: str) -> None:
    executor, prepared, ports = setup()
    ports.response["receipt"]["binding"][field] = (
        "2" if field == "connection_revision" else "wrong"
    )
    with pytest.raises(ModelExecutionAmbiguousError):
        executor.execute(prepared)
    assert len(ports.requests) == 1 and not ports.fetched


@pytest.mark.parametrize("mutation", ["missing", "null", "extra"])
def test_malformed_binding_is_held(mutation: str) -> None:
    executor, prepared, ports = setup()
    receipt = ports.response["receipt"]
    if mutation == "missing":
        receipt.pop("binding")
    elif mutation == "null":
        receipt["binding"] = None
    else:
        receipt["binding"]["unknown"] = "wrong"
    with pytest.raises(ModelExecutionAmbiguousError):
        executor.execute(prepared)
    assert not ports.fetched


def test_schema_paths_cannot_be_confused_in_either_direction() -> None:
    legacy, prepared, ports = legacy_setup()
    successor = FencedModelOperationExecutor(**configuration(ports))
    with pytest.raises(ModelExecutionAmbiguousError):
        successor.execute(prepared)  # Legacy11 is refused by successor14.
    _, _, successor_ports = setup()
    ports.response = successor_ports.response
    with pytest.raises(ModelExecutionAmbiguousError):
        legacy.execute(prepared)  # Successor14 is refused by legacy11.
    assert not ports.fetched and len(ports.requests) == 2


@pytest.mark.parametrize(
    "precheck,unavailable", [(True, False), (False, False), (False, True)]
)
def test_early_and_late_no_call_claims_keep_distinct_accounting(
    precheck: bool,
    unavailable: bool,
) -> None:
    executor, prepared, ports = setup()
    response = no_call(precheck=precheck, unavailable=unavailable)
    receipt = response["receipt"]
    receipt["request_sha256"] = prepared.argument_sha256
    receipt["request_size_bytes"] = len(prepared.argument_bytes)
    if not unavailable:
        receipt["binding"].update(
            connection_id="con_synthetic",
            connector_revision="synthetic-connector",
        )
    ports.response = response
    result = executor.execute(prepared)
    assert result.parsed.fence_outcome == (
        "precheck_mismatch"
        if precheck
        else "dispatcher_unavailable"
        if unavailable
        else "dispatcher_mismatch"
    )
    assert (result.parsed.receipt.cost is None) == precheck
    assert result.completion is None and not ports.fetched
    # Ports have no release API: consistent receipt claims grant no refund.
    assert ports.events == [
        "retain:request",
        "admit",
        "invoke",
        "retain:response_normalized",
    ]


@pytest.mark.parametrize(
    "field", ["expect", "connection_id", "operation_id", "idempotency_key"]
)
def test_changed_retained_intent_refuses_before_admission(field: str) -> None:
    executor, prepared, ports = setup()
    document = json.loads(prepared.request_bytes)
    if field == "expect":
        document[field]["connection_revision"] = "2"
    else:
        document[field] = "changed"
    altered = replace(prepared, request_bytes=rfc8785.dumps(document))
    with pytest.raises(HostedClientError, match="do not dispatch"):
        executor.execute(altered)
    assert not ports.events


@pytest.mark.parametrize("content", [b"wrong", b"x" * len(CONTENT)])
def test_artifact_must_match_size_and_digest(content: bytes) -> None:
    executor, prepared, ports = setup()
    ports.fetched_content = content
    with pytest.raises(ModelExecutionAmbiguousError):
        executor.execute(prepared)
    assert "completion" not in ports.evidence


def test_expired_response_never_fetches_or_resends() -> None:
    executor, prepared, ports = setup()
    descriptor = ports.response.pop("artifact")
    ports.response["result"] = {
        "expired": True,
        "content_sha256": descriptor["content_sha256"],
        "size_bytes": descriptor["size_bytes"],
    }
    ports.response["receipt"]["replayed"] = True
    ports.response["receipt"]["completion"]["state"] = "expired"
    result = executor.execute(prepared)
    assert result.parsed.expired and result.completion is None
    assert len(ports.requests) == 1 and not ports.fetched
