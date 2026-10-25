"""Authored receipt fixtures, never proof of a native call or ledger transition."""

import hashlib
from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from zeo_core.integrations.hosted.client import (
    HostedOperationRequest,
    HostedOperationResponse,
)
from zeo_core.integrations.model_ops import (
    AmbiguousModelReceiptError,
    TextCompletionRequest,
)
from zeo_core.integrations.model_ops.fenced_receipts import (
    DRAFT_STAMP,
    FencedModelReceipt,
    ParsedFencedModelResponse,
    ReceiptBinding,
    parse_fenced_model_response,
)
from zeo_core.integrations.model_ops.receipts import ModelReceipt, parse_model_response

from .test_receipts import live as legacy_live


def request() -> HostedOperationRequest:
    return HostedOperationRequest.model_validate(
        {
            "connection_id": "con_test",
            "connector_revision": "models@pinned",
            "operation_id": "anthropic.messages.create",
            "idempotency_key": "original-key",
            "expect": {
                "external_identity": "synthetic-account",
                "connection_revision": "1",
            },
            "arguments": {
                "model": "test-model-20261014",
                "messages": [{"role": "user", "text": "Hello"}],
                "max_output_tokens": 10,
            },
        }
    )


def binding() -> dict[str, Any]:
    return {
        "connection_id": "con_test",
        "connector_revision": "models@pinned",
        "external_identity": "synthetic-account",
        "connection_revision": "1",
    }


def live() -> dict[str, Any]:
    value = legacy_live()
    receipt = value["receipt"]
    receipt["draft_schema"] = DRAFT_STAMP
    receipt["binding"] = binding()
    raw = TextCompletionRequest.model_validate(request().arguments).canonical_bytes()
    receipt["request_sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    receipt["request_size_bytes"] = len(raw)
    return value


def parse(
    value: dict[str, Any], original: HostedOperationRequest | None = None
) -> ParsedFencedModelResponse:
    return parse_fenced_model_response(
        HostedOperationResponse.model_validate(value),
        original_request=request() if original is None else original,
        catalogue_revision="draft-test",
        catalogued_reported_models=(),
    )


def no_call(*, unavailable: bool = False, precheck: bool = False) -> dict[str, Any]:
    value = live()
    receipt = value["receipt"]
    for field in [
        "completion",
        "usage",
        "usage_provider",
        "model_reported",
        "model_mismatch",
        "stop_reason",
        "stop_reason_provider",
        "truncated",
    ]:
        receipt.pop(field)
    value.pop("artifact")
    value["status"] = "failed_safe"
    receipt["reason"] = "provider_unavailable" if unavailable else "binding_mismatch"
    value["normalized_error"] = {
        "code": "PROVIDER_UNAVAILABLE" if unavailable else "REQUEST_REFUSED",
        "message": "binding_unavailable"
        if unavailable
        else "binding_mismatch:connection_revision",
    }
    if unavailable:
        receipt.pop("binding")
    else:
        receipt["binding"]["connection_revision"] = "2"
    if precheck:
        receipt.pop("cost")
        receipt.pop("budget")
    else:
        receipt["cost"].pop("tariff_estimate_micro_usd")
    return value


def test_confirmed_and_expired_are_matched_to_original_intent() -> None:
    value = live()
    result = parse(value)
    assert result.fence_outcome == "matched" and result.stage == "reserved"
    assert result.receipt.binding is not None and not result.expired
    value["receipt"]["completion"]["state"] = "expired"
    value["receipt"]["replayed"] = True
    art = value.pop("artifact")
    value["result"] = {
        "expired": True,
        "content_sha256": art["content_sha256"],
        "size_bytes": art["size_bytes"],
    }
    assert parse(value).expired and parse(value).completion is None


@pytest.mark.parametrize("precheck", [False, True])
@pytest.mark.parametrize("replayed", [False, True])
def test_stored_mismatch_stages_and_replay_remain_distinct(
    precheck: bool, replayed: bool
) -> None:
    value = no_call(precheck=precheck)
    value["receipt"]["replayed"] = replayed
    result = parse(value)
    assert result.fence_outcome == (
        "precheck_mismatch" if precheck else "dispatcher_mismatch"
    )
    assert result.stage == ("before_budget" if precheck else "reserved")
    assert (result.receipt.cost is None) == precheck
    assert result.receipt.replayed == replayed


def test_dispatcher_unavailable_is_no_comparison_and_no_invented_binding() -> None:
    result = parse(no_call(unavailable=True))
    assert result.fence_outcome == "dispatcher_unavailable"
    assert result.receipt.binding is None and result.receipt.cost is not None
    assert not result.receipt.cost.held
    # A generic provider outage is a different recorded claim.
    value = no_call(unavailable=True)
    value["normalized_error"]["message"] = "provider unavailable"
    with pytest.raises(AmbiguousModelReceiptError):
        parse(value)  # A compared fenced provider-stage receipt now needs binding.
    value["receipt"]["binding"] = binding()
    assert parse(value).fence_outcome == "matched"


@pytest.mark.parametrize(
    "field,value",
    [
        ("external_identity", "other"),
        ("connection_revision", "2"),
        ("connector_revision", "other@2"),
        ("connection_id", "con_other"),
    ],
)
def test_confirmed_wrong_binding_refuses(field: str, value: str) -> None:
    response = live()
    response["receipt"]["binding"][field] = value
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {**binding(), "extra": "no"},
        {**binding(), "connection_revision": "0"},
        {**binding(), "external_identity": 123},
        {**binding(), "connector_revision": None},
    ],
)
def test_binding_is_closed_typed_and_nonnull(value: object) -> None:
    response = live()
    response["receipt"]["binding"] = value
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)


def test_missing_binding_refuses_and_unfenced_does_not_gain_fake_comparison() -> None:
    response = live()
    response["receipt"].pop("binding")
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)
    original = request().model_copy(update={"expect": None})
    assert parse(response, original).fence_outcome == "not_requested"
    response["receipt"]["binding"] = binding()
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response, original)


def test_mismatch_precedence_and_factual_values() -> None:
    response = no_call()
    response["receipt"]["binding"]["external_identity"] = "other"
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)  # connection_revision message loses precedence
    response["normalized_error"]["message"] = "binding_mismatch:external_identity"
    assert parse(response).fence_outcome == "dispatcher_mismatch"
    response["receipt"]["binding"] = binding()
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)  # No actual mismatch.


@pytest.mark.parametrize(
    "field,value",
    [
        ("usage", {"input_uncached_tokens": 0}),
        ("provider_request_id", "invented"),
        ("model_reported", "invented"),
        ("usage_provider", {}),
        ("excluded_blocks", {}),
    ],
)
def test_no_call_cannot_carry_provider_evidence(field: str, value: object) -> None:
    response = no_call()
    response["receipt"][field] = value
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)


@pytest.mark.parametrize(
    "field,value",
    [
        ("held", True),
        ("tariff_estimate_micro_usd", 0),
        ("tariff_estimate_micro_usd", 1),
        ("incomplete", ["usage"]),
    ],
)
def test_no_call_cannot_claim_held_or_invent_estimate(
    field: str, value: object
) -> None:
    response = no_call(unavailable=True)
    response["receipt"]["cost"][field] = value
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)


@pytest.mark.parametrize("field", ["cost", "budget", "replayed"])
def test_dispatcher_no_call_requires_reservation_projection(field: str) -> None:
    response = no_call(unavailable=True)
    response["receipt"].pop(field)
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)


def test_precheck_mismatch_requires_replay_but_other_early_refusals_forbid_it() -> None:
    response = no_call(precheck=True)
    response["receipt"].pop("replayed")
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)
    response["receipt"]["reason"] = "model_not_allowed"
    response["receipt"].pop("binding")
    response["status"] = "refused"
    response["normalized_error"]["message"] = "refused"
    assert parse(response).fence_outcome == "not_compared"
    response["receipt"]["replayed"] = False
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)


@pytest.mark.parametrize(
    "mutation", ["digest", "size", "model", "rate", "status", "code", "stamp"]
)
def test_native_identity_and_reason_boundaries_refuse(mutation: str) -> None:
    response = no_call()
    if mutation == "digest":
        response["receipt"]["request_sha256"] = "sha256:" + "f" * 64
    elif mutation == "size":
        response["receipt"]["request_size_bytes"] += 1
    elif mutation == "model":
        response["receipt"]["model_requested"] = "other-model"
    elif mutation == "rate":
        response["receipt"]["cost"]["rates_source"]["revision"] = "other-rate"
    elif mutation == "status":
        response["status"] = "refused"
    elif mutation == "code":
        response["normalized_error"]["code"] = "PROVIDER_UNAVAILABLE"
    else:
        response["receipt"]["draft_schema"] = "model-ops-draft-13 sha256:" + "a" * 64
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)


def test_cross_stamp_refusal_is_not_a_request_identity_accident() -> None:
    old = legacy_live()
    assert ModelReceipt.model_validate(old["receipt"])
    with pytest.raises(ValidationError) as error:
        FencedModelReceipt.model_validate(old["receipt"])
    assert any(
        e["loc"] == ("draft_schema",) or e["loc"] == () for e in error.value.errors()
    )
    new = live()
    assert FencedModelReceipt.model_validate(new["receipt"])
    with pytest.raises(ValidationError) as old_error:
        ModelReceipt.model_validate(new["receipt"])
    assert any(e["loc"] == ("draft_schema",) for e in old_error.value.errors())
    with pytest.raises(AmbiguousModelReceiptError):
        parse_model_response(
            HostedOperationResponse.model_validate(new),
            expected_model="test-model-20261014",
            catalogue_revision="draft-test",
            expected_request_sha256=new["receipt"]["request_sha256"],
            expected_request_size_bytes=new["receipt"]["request_size_bytes"],
            catalogued_reported_models=(),
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"private_key": "PRIVATE SENTINEL"},
        {"nested": {"child": []}},
        {"counter": 2**53},
        {"counter": float("nan")},
    ],
)
def test_unbounded_or_secret_native_usage_is_sanitized(payload: dict[str, Any]) -> None:
    response = live()
    response["receipt"]["usage_provider"] = payload
    with pytest.raises(AmbiguousModelReceiptError) as error:
        parse(response)
    assert "PRIVATE SENTINEL" not in str(error.value)


def test_copied_binding_cannot_bypass_revalidation() -> None:
    forged = ReceiptBinding.model_validate(binding()).model_copy(
        update={"connection_revision": None}
    )
    response = deepcopy(live())
    response["receipt"]["binding"] = forged.model_dump()
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)


def budget_refusal(detail: str) -> dict[str, Any]:
    value = no_call()
    r = value["receipt"]
    r["reason"] = "budget_exhausted"
    r["detail"] = detail
    r["binding"] = binding()
    r.pop("replayed")
    value["status"] = "refused"
    value["normalized_error"] = {"code": "REQUEST_REFUSED", "message": "refused"}
    if detail == "caps_missing":
        r.pop("cost")
        r.pop("budget")
    return value


@pytest.mark.parametrize("detail", ["caps_missing", "authority"])
def test_budget_refusal_keeps_the_already_made_comparison(detail: str) -> None:
    value = budget_refusal(detail)
    assert parse(value).fence_outcome == "matched"
    value["receipt"].pop("binding")
    with pytest.raises(AmbiguousModelReceiptError):
        parse(value)


@pytest.mark.parametrize(
    "field,value", [("replayed", False), ("usage", {}), ("detail", "not-a-detail")]
)
def test_budget_refusal_forbids_uncomputed_or_wrong_stage_fields(
    field: str, value: object
) -> None:
    response = budget_refusal("authority")
    response["receipt"][field] = value
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)


def test_unknown_outcome_cannot_release_its_reservation() -> None:
    response = no_call(unavailable=True)
    response["receipt"]["reason"] = "unknown_outcome"
    response["receipt"]["binding"] = binding()
    response["status"] = "ambiguous"
    response["normalized_error"] = {"code": "RESULT_AMBIGUOUS", "message": "unknown"}
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)
    response["receipt"]["cost"]["held"] = True
    result = parse(response)
    assert result.receipt.cost is not None and result.receipt.cost.held
    assert result.fence_outcome == "matched"


def test_confirmed_missing_usage_stays_unknown_not_zero() -> None:
    response = live()
    response["receipt"].pop("usage")
    response["receipt"]["cost"].pop("tariff_estimate_micro_usd")
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)
    response["receipt"]["cost"]["held"] = True
    assert parse(response).receipt.cost is not None
    response["receipt"]["usage_provider_omitted"] = False
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)


def test_bad_request_cannot_claim_a_validated_fingerprint() -> None:
    response = no_call(precheck=True)
    response["status"] = "refused"
    response["receipt"]["reason"] = "bad_request"
    response["receipt"].pop("binding")
    response["receipt"].pop("replayed")
    response["normalized_error"]["message"] = "refused"
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)
    response["receipt"].pop("request_sha256")
    response["receipt"].pop("request_size_bytes")
    assert parse(response).fence_outcome == "not_compared"


def test_reported_model_flags_and_expired_descriptors_remain_bound() -> None:
    response = live()
    response["receipt"]["model_mismatch"] = True
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)
    response = live()
    response["receipt"]["completion"]["size_bytes"] += 1
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)
    response = live()
    response["receipt"]["stop_reason"] = "max_tokens"
    with pytest.raises(AmbiguousModelReceiptError):
        parse(response)
