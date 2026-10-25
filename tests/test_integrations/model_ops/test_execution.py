"""Injected model execution; no provider, credential or live Broker."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

import pytest
import rfc8785

from zeo_core.integrations.hosted.client import (
    HostedClientError,
    HostedExpectedBinding,
    HostedOperationRequest,
    HostedOperationResponse,
    HostedOperationStatus,
)
from zeo_core.integrations.model_ops import (
    CataloguedModel,
    DraftModelOperationClient,
    ModelCatalogue,
    ModelMessage,
    TextCompletionRequest,
    parse_model_response,
)
from zeo_core.integrations.model_ops.execution import (
    EvidenceKind,
    ModelAdmission,
    ModelExecutionAmbiguousError,
    ModelOperationExecutor,
    PreparedModelCall,
)

from .test_receipts import live

MODEL = "test-model-20261014"
CONTENT = b'{"kind":"finish","summary":"Synthetic completion"}'


def digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


class Ports:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.requests: list[HostedOperationRequest] = []
        self.fetched: list[str] = []
        self.evidence: dict[str, bytes] = {}
        self.fail: str | None = None
        self.prepared: PreparedModelCall | None = None
        self.response: dict[str, Any] = live()
        self.fetched_content = CONTENT

    def retain(self, kind: EvidenceKind, content: bytes) -> str:
        self.events.append("retain:" + kind)
        if self.fail == kind:
            raise OSError("PRIVATE FAILURE SENTINEL")
        self.evidence[kind] = content
        return "sha256:" + "0" * 64 if self.fail == "digest" else digest(content)

    def admit(self, prepared: PreparedModelCall) -> ModelAdmission:
        self.events.append("admit")
        if self.fail == "admit":
            raise ValueError("PRIVATE FAILURE SENTINEL")
        return ModelAdmission(
            request_sha256=(
                "sha256:" + "0" * 64
                if self.fail == "admission_binding"
                else prepared.request_sha256
            ),
            idempotency_key=(
                "changed" if self.fail == "admission_key" else prepared.idempotency_key
            ),
            reference="" if self.fail == "admission_reference" else "synthetic-reserve",
        )

    def invoke(self, request: HostedOperationRequest) -> HostedOperationResponse:
        self.events.append("invoke")
        self.requests.append(request)
        if self.fail == "invoke":
            raise TimeoutError("PRIVATE FAILURE SENTINEL")
        return HostedOperationResponse.model_validate(self.response)

    def fetch_artifact(self, *, artifact_id: str, max_bytes: int) -> bytes:
        self.events.append("fetch")
        self.fetched.append(artifact_id)
        if self.fail == "fetch":
            raise OSError("PRIVATE FAILURE SENTINEL")
        return self.fetched_content


def configuration(ports: Ports) -> dict[str, Any]:
    return {
        "preparer": DraftModelOperationClient(
            catalogue=ModelCatalogue(
                provider="anthropic",
                models=(CataloguedModel(model=MODEL, max_output_tokens=1000),),
                connector_revision="synthetic-connector",
                proposed_contract_sha256="a" * 64,
            ),
            connection_id="con_synthetic",
            connection_output_token_limit=1000,
            expected_binding=HostedExpectedBinding(
                external_identity="synthetic-account", connection_revision="1"
            ),
        ),
        "transport": ports,
        "retain": ports.retain,
        "admit": ports.admit,
        "rate_revision": "draft-test",
        "catalogued_reported_models": (MODEL,),
        "max_completion_bytes": 1024,
    }


def setup() -> tuple[ModelOperationExecutor, PreparedModelCall, Ports]:
    ports = Ports()
    executor = ModelOperationExecutor(**configuration(ports))
    prepared = executor.prepare(
        TextCompletionRequest(
            model=MODEL,
            messages=(ModelMessage(role="user", text="Synthetic task"),),
            max_output_tokens=100,
        ),
        idempotency_key="synthetic-occurrence-1",
    )
    ports.prepared = prepared
    artifact = ports.response["artifact"]
    artifact["content_sha256"] = digest(CONTENT)
    artifact["size_bytes"] = len(CONTENT)
    ports.response["receipt"]["completion"] = artifact | {"state": "available"}
    ports.response["receipt"]["request_sha256"] = prepared.argument_sha256
    ports.response["receipt"]["request_size_bytes"] = len(prepared.argument_bytes)
    return executor, prepared, ports


def test_retention_and_admission_precede_one_fenced_dispatch() -> None:
    executor, prepared, ports = setup()
    assert not ports.events  # prepare is pure
    result = executor.execute(prepared)
    assert ports.events == [
        "retain:request",
        "admit",
        "invoke",
        "retain:response_normalized",
        "fetch",
        "retain:completion",
    ]
    assert len(ports.requests) == 1
    assert ports.requests[0].expect == HostedExpectedBinding(
        external_identity="synthetic-account", connection_revision="1"
    )
    assert ports.requests[0].idempotency_key == prepared.idempotency_key
    assert result.completion == CONTENT
    assert result.prepared is prepared
    assert ports.evidence["request"] == prepared.request_bytes
    assert json.loads(result.normalized_response)["status"] == "confirmed"


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
def test_pre_dispatch_failures_never_send(failure: str) -> None:
    executor, prepared, ports = setup()
    ports.fail = failure
    with pytest.raises(HostedClientError, match="has not dispatched") as caught:
        executor.execute(prepared)
    assert "PRIVATE" not in str(caught.value)
    assert not ports.requests
    assert not ports.fetched


@pytest.mark.parametrize(
    "failure", ["invoke", "response_normalized", "fetch", "completion"]
)
def test_post_dispatch_failures_preserve_original_replay_intent(failure: str) -> None:
    executor, prepared, ports = setup()
    ports.fail = failure
    with pytest.raises(ModelExecutionAmbiguousError) as caught:
        executor.execute(prepared)
    assert caught.value.prepared is prepared
    assert "same prepared request" in str(caught.value)
    assert "PRIVATE" not in str(caught.value)
    assert len(ports.requests) == 1
    assert ports.requests[0].idempotency_key == prepared.idempotency_key


@pytest.mark.parametrize(
    "field", ["expect", "connection_id", "operation_id", "idempotency_key"]
)
def test_changed_prepared_envelope_refuses_before_retention(field: str) -> None:
    executor, prepared, ports = setup()
    envelope = json.loads(prepared.request_bytes)
    if field == "expect":
        envelope[field]["connection_revision"] = "2"
    else:
        envelope[field] = "changed"
    changed = replace(prepared, request_bytes=rfc8785.dumps(envelope))
    with pytest.raises(
        HostedClientError, match="prepared model request or fence changed"
    ):
        executor.execute(changed)
    assert not ports.events


@pytest.mark.parametrize("field", ["request_sha256", "model_requested", "rates"])
def test_receipt_mismatch_is_ambiguous_and_never_fetches(field: str) -> None:
    executor, prepared, ports = setup()
    receipt = ports.response["receipt"]
    if field == "rates":
        receipt["cost"]["rates_source"]["revision"] = "wrong-rates"
    else:
        receipt[field] = (
            "sha256:" + "0" * 64 if field == "request_sha256" else "wrong-model"
        )
    with pytest.raises(ModelExecutionAmbiguousError) as caught:
        executor.execute(prepared)
    assert caught.value.prepared is prepared
    assert len(ports.requests) == 1 and not ports.fetched


@pytest.mark.parametrize("content", [b"wrong", b"X" * len(CONTENT)])
def test_size_or_digest_tampering_never_returns_completion(content: bytes) -> None:
    executor, prepared, ports = setup()
    ports.fetched_content = content
    with pytest.raises(ModelExecutionAmbiguousError):
        executor.execute(prepared)
    assert "completion" not in ports.evidence


def test_oversized_artifact_is_refused_before_fetch() -> None:
    executor, prepared, ports = setup()
    ports.response["artifact"]["size_bytes"] = 1025
    ports.response["receipt"]["completion"]["size_bytes"] = 1025
    with pytest.raises(ModelExecutionAmbiguousError):
        executor.execute(prepared)
    assert not ports.fetched


def test_expired_replay_is_retained_without_fetch_or_new_dispatch() -> None:
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


def test_replay_is_explicit_and_keeps_the_same_request_and_key() -> None:
    executor, prepared, ports = setup()
    executor.execute(prepared)
    ports.response["receipt"]["replayed"] = True
    replay = executor.execute(prepared)
    assert replay.parsed.receipt.replayed
    assert ports.requests[0] == ports.requests[1]
    assert len(ports.requests) == 2  # two explicit calls, not an automatic retry


def test_no_default_ports_exist() -> None:
    with pytest.raises(TypeError):
        ModelOperationExecutor()  # type: ignore[call-arg]


@pytest.mark.parametrize("field", ["model", "argument_bytes", "idempotency_key"])
def test_changed_prepared_metadata_cannot_reach_dispatch(field: str) -> None:
    executor, prepared, ports = setup()
    if field == "argument_bytes":
        changed = replace(prepared, argument_bytes=b"different")
    elif field == "model":
        changed = replace(prepared, model="different")
    else:
        changed = replace(prepared, idempotency_key="different")
    with pytest.raises(HostedClientError):
        executor.execute(changed)
    assert not ports.events


def test_request_over_member_envelope_bound_refuses_without_effects() -> None:
    executor, _, ports = setup()
    with pytest.raises(HostedClientError):
        executor.prepare(
            TextCompletionRequest(
                model=MODEL,
                messages=(ModelMessage(role="user", text="x" * 65536),),
                max_output_tokens=100,
            ),
            idempotency_key="synthetic-large",
        )
    assert not ports.events


def test_unknown_receiver_outcome_is_ambiguous_without_an_artifact_fetch() -> None:
    executor, prepared, ports = setup()
    ports.response.pop("artifact")
    ports.response["status"] = "ambiguous"
    ports.response["normalized_error"] = {
        "code": "RESULT_AMBIGUOUS",
        "message": "The outcome is unknown.",
    }
    receipt = ports.response["receipt"]
    for key in [
        "completion",
        "stop_reason",
        "stop_reason_provider",
        "truncated",
        "model_mismatch",
        "model_reported",
    ]:
        receipt.pop(key, None)
    receipt["reason"] = "unknown_outcome"
    receipt["cost"]["held"] = True
    parsed = parse_model_response(
        HostedOperationResponse.model_validate(ports.response),
        expected_model=prepared.model,
        catalogue_revision="draft-test",
        expected_request_sha256=prepared.argument_sha256,
        expected_request_size_bytes=len(prepared.argument_bytes),
        catalogued_reported_models=(MODEL,),
    )
    assert parsed.status is HostedOperationStatus.AMBIGUOUS
    with pytest.raises(ModelExecutionAmbiguousError) as caught:
        executor.execute(prepared)
    assert caught.value.prepared is prepared
    assert len(ports.requests) == 1 and not ports.fetched


def test_explicit_safe_refusal_returns_disposition_without_fetch() -> None:
    executor, prepared, ports = setup()
    ports.response = {
        "status": "refused",
        "execution_id": "synthetic-refusal",
        "normalized_error": {"code": "REQUEST_REFUSED", "message": "Not allowed."},
        "receipt": {
            "draft_schema": ports.response["receipt"]["draft_schema"],
            "capability": "text_completion",
            "reason": "model_not_allowed",
            "model_requested": MODEL,
            "request_sha256": prepared.argument_sha256,
            "request_size_bytes": len(prepared.argument_bytes),
        },
    }
    result = executor.execute(prepared)
    assert result.parsed.status is HostedOperationStatus.REFUSED
    assert result.completion is None and not ports.fetched


@pytest.mark.parametrize(
    "field,value",
    [
        ("preparer", None),
        ("transport", None),
        ("retain", None),
        ("admit", None),
        ("rate_revision", ""),
        ("catalogued_reported_models", []),
        ("max_completion_bytes", True),
        ("max_completion_bytes", 0),
        ("max_completion_bytes", 10 * 1024 * 1024 + 1),
    ],
)
def test_each_explicit_configuration_boundary_refuses(
    field: str, value: object
) -> None:
    ports = Ports()
    config = configuration(ports)
    config[field] = value
    with pytest.raises(HostedClientError):
        ModelOperationExecutor(**config)
    assert not ports.events
