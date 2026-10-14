"""Synthetic draft receipts: no transport, provider or authentic usage evidence."""

from copy import deepcopy
from typing import Any

import pytest

from zeo_core.integrations.hosted.client import (
    HostedClientError,
    HostedOperationResponse,
)
from zeo_core.integrations.model_ops import (
    AmbiguousModelReceiptError,
    ParsedModelResponse,
    parse_model_response,
)


def receipt() -> dict[str, Any]:
    return {
        "draft_schema": (
            "model-ops-draft-3 sha256:"
            "753d8b1ca692f7b579ff340544d4e2321525464919ecf1a0ac418bc1598449d9"
        ),
        "capability": "text_completion",
        "model_requested": "test-model-20261014",
        "model_reported": "test-model-20261014",
        "model_mismatch": False,
        "stop_reason": "end",
        "stop_reason_provider": "synthetic-end",
        "truncated": False,
        "request_sha256": "sha256:" + "a" * 64,
        "request_size_bytes": 32,
        "completion": artifact() | {"state": "available"},
        "usage": {"cache_read_tokens": 20},
        "usage_provider": {"input_tokens": 30, "details": {"cached": 20}},
        "cost": {
            "reservation_micro_usd": 100,
            "held": True,
            "tariff_estimate_micro_usd": 10,
            "rates_source": {
                "revision": "draft-test",
                "read_on": "2026-10-14",
                "pages": ["https://example.invalid/rates"],
            },
        },
        "budget": {
            "provider": "anthropic",
            "day": "2026-10-14",
            "scope": "broker_total",
            "authority_remaining_micro_usd": 900,
            "day_calls_remaining": 4,
            "day_spend_remaining_micro_usd": 900,
        },
    }


def artifact(
    filename: str = "completion.txt", media: str = "text/plain"
) -> dict[str, Any]:
    return {
        "artifact_id": "art_synthetic01",
        "content_sha256": "sha256:" + "a" * 64,
        "size_bytes": 32,
        "media_type": media,
        "filename": filename,
    }


def parse(value: dict[str, Any]) -> ParsedModelResponse:
    return parse_model_response(
        HostedOperationResponse.model_validate(value),
        expected_model="test-model-20261014",
        catalogue_revision="draft-test",
        expected_request_sha256="sha256:" + "a" * 64,
        expected_request_size_bytes=32,
    )


def live() -> dict[str, Any]:
    return {
        "status": "confirmed",
        "execution_id": "synthetic",
        "artifact": artifact(),
        "receipt": receipt(),
    }


def expired() -> dict[str, Any]:
    r = receipt()
    r["replayed"] = True
    r["completion"] = artifact() | {"state": "expired"}
    return {
        "status": "confirmed",
        "execution_id": "synthetic",
        "result": {
            "expired": True,
            "content_sha256": "sha256:" + "a" * 64,
            "size_bytes": 32,
        },
        "receipt": r,
    }


def test_live_keeps_costs_distinct_and_missing_usage_unknown() -> None:
    parsed = parse(live())
    assert parsed.completion is not None
    assert not parsed.expired
    assert parsed.receipt.usage.input_uncached_tokens is None
    assert parsed.receipt.usage.cache_read_tokens == 20
    assert parsed.receipt.cost is not None
    assert parsed.receipt.cost.reservation_micro_usd == 100
    assert parsed.receipt.cost.tariff_estimate_micro_usd == 10
    assert parsed.receipt.usage_provider["input_tokens"] == 30


def test_live_replay_descriptor_matches() -> None:
    value = live()
    value["receipt"]["completion"] = artifact() | {"state": "available"}
    value["receipt"]["replayed"] = True
    assert parse(value).receipt.replayed


def test_expired_replay_has_no_fetchable_completion() -> None:
    parsed = parse(expired())
    assert parsed.expired and parsed.completion is None


@pytest.mark.parametrize(
    "reason,status,code",
    [
        ("model_not_allowed", "refused", "REQUEST_REFUSED"),
        ("bad_request", "refused", "REQUEST_REFUSED"),
        ("budget_exhausted", "refused", "REQUEST_REFUSED"),
        ("request_in_progress", "refused", "REQUEST_REFUSED"),
        ("idempotency_key_reused", "refused", "REQUEST_REFUSED"),
        ("blocked", "failed_safe", "REQUEST_REFUSED"),
        ("provider_refused", "failed_safe", "REQUEST_REFUSED"),
        ("rate_limited", "failed_safe", "RATE_LIMITED"),
        ("provider_unavailable", "failed_safe", "PROVIDER_UNAVAILABLE"),
        ("unknown_outcome", "ambiguous", "RESULT_AMBIGUOUS"),
        ("connection_revoked", "refused", "CONNECTION_REVOKED"),
        ("connection_repair_required", "refused", "CONNECTION_REPAIR_REQUIRED"),
        ("connector_revision_unavailable", "refused", "CONNECTOR_REVISION_UNAVAILABLE"),
    ],
)
def test_fixed_error_mapping(reason: str | None, status: str, code: str) -> None:
    r = receipt()
    r["reason"] = reason
    r.pop("completion")
    r["cost"]["tariff_estimate_micro_usd"] = None
    if reason in {
        "model_not_allowed",
        "bad_request",
        "budget_exhausted",
        "request_in_progress",
        "idempotency_key_reused",
    }:
        r = {
            key: value
            for key, value in r.items()
            if key
            in {
                "draft_schema",
                "capability",
                "reason",
                "model_requested",
                "cost",
                "budget",
            }
        }
        if reason != "bad_request":
            r.update(request_sha256="sha256:" + "a" * 64, request_size_bytes=32)
        if reason == "budget_exhausted":
            r["cost"].pop("tariff_estimate_micro_usd")
            r["cost"]["held"] = False
        else:
            r.pop("cost")
            r.pop("budget")
    if reason in {
        "connection_revoked",
        "connection_repair_required",
        "connector_revision_unavailable",
    }:
        r = {
            "draft_schema": receipt()["draft_schema"],
            "capability": "text_completion",
            "reason": reason,
            "model_requested": "test-model-20261014",
        }
    value: dict[str, Any] = {
        "status": status,
        "execution_id": "synthetic",
        "receipt": r,
        "normalized_error": {"code": code, "message": "Synthetic refusal."},
    }
    parsed = parse(value)
    assert parsed.completion is None and not parsed.expired
    wrong = deepcopy(value)
    wrong["normalized_error"]["code"] = "FAILED_SAFE"
    with pytest.raises(HostedClientError, match="receipt is invalid"):
        parse(wrong)


@pytest.mark.parametrize(
    "path,value",
    [
        (("model_requested",), "other-model"),
        (("cost", "rates_source", "revision"), "other-revision"),
        (("cost", "rates_source", "read_on"), "2026-02-30"),
        (("cost", "incomplete"), ["cache_read_tokens"]),
        (("usage", "cache_read_tokens"), True),
        (("usage", "output_tokens"), -1),
        (("usage_provider",), {"depth": {"too": {"deep": 1}}}),
        (("usage_provider",), {"a": {str(n): 1 for n in range(32)}}),
        (("reason",), "stopped"),
        (("completion",), artifact() | {"state": "available", "size_bytes": 99}),
    ],
)
def test_malformed_or_unbound_receipts_are_refused(
    path: tuple[str, ...], value: object
) -> None:
    response = live()
    target = response["receipt"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(HostedClientError, match="receipt is invalid"):
        parse(response)


@pytest.mark.parametrize(
    "path,value",
    [
        (("receipt", "replayed"), False),
        (("receipt", "completion", "state"), "available"),
        (("result", "size_bytes"), 99),
        (("result", "content_sha256"), "sha256:" + "b" * 64),
        (("result", "expired"), False),
    ],
)
def test_expired_metadata_must_agree(path: tuple[str, ...], value: object) -> None:
    response = expired()
    target = response
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(HostedClientError, match="receipt is invalid"):
        parse(response)


def test_copied_response_cannot_bypass_outer_shape_validation() -> None:
    response = HostedOperationResponse.model_validate(live()).model_copy(
        update={"status": "refused"}
    )
    with pytest.raises(HostedClientError, match="receipt is invalid"):
        parse_model_response(
            response,
            expected_model="test-model-20261014",
            catalogue_revision="draft-test",
            expected_request_sha256="sha256:" + "a" * 64,
            expected_request_size_bytes=32,
        )


def test_missing_receipt_does_not_leak_supplied_text() -> None:
    response = HostedOperationResponse.model_validate(live()).model_copy(
        update={"receipt": {"model_requested": "synthetic-private-text"}}
    )
    with pytest.raises(HostedClientError) as error:
        parse_model_response(
            response,
            expected_model="test-model-20261014",
            catalogue_revision="draft-test",
            expected_request_sha256="sha256:" + "a" * 64,
            expected_request_size_bytes=32,
        )
    assert "synthetic-private-text" not in str(error.value)


def test_incomplete_estimate_preserves_missing_rate() -> None:
    value = live()
    value["receipt"]["cost"].update(
        tariff_estimate_micro_usd=None, incomplete=["cache_read_tokens"]
    )
    cost = parse(value).receipt.cost
    assert cost is not None and cost.tariff_estimate_micro_usd is None


def early(reason: str = "bad_request") -> dict[str, Any]:
    value = {
        "status": "refused",
        "execution_id": "synthetic",
        "normalized_error": {
            "code": "REQUEST_REFUSED",
            "message": "Synthetic refusal.",
        },
        "receipt": {
            "draft_schema": receipt()["draft_schema"],
            "capability": "text_completion",
            "reason": reason,
            "model_requested": "test-model-20261014",
            "request_sha256": "sha256:" + "a" * 64,
            "request_size_bytes": 32,
        },
    }

    if reason == "bad_request":
        value["receipt"].pop("request_sha256")
        value["receipt"].pop("request_size_bytes")
    return value


def test_pre_budget_refusal_has_no_computed_money() -> None:
    parsed = parse(early())
    assert parsed.receipt.cost is None and parsed.receipt.budget is None
    assert parsed.receipt.usage.input_uncached_tokens is None
    assert "cost" not in parsed.receipt.model_fields_set


@pytest.mark.parametrize(
    "field",
    [
        "cost",
        "budget",
        "request_artifact",
        "completion",
        "usage",
        "usage_provider",
        "provider_request_id",
        "model_reported",
    ],
)
def test_early_refusal_forbids_even_null_uncomputed_fields(field: str) -> None:
    value = early()
    value["receipt"][field] = None
    with pytest.raises(HostedClientError):
        parse(value)


@pytest.mark.parametrize("field", ["request_sha256", "request_size_bytes"])
def test_early_refusal_requires_request_fingerprint(field: str) -> None:
    value = early("model_not_allowed")
    value["receipt"].pop(field)
    with pytest.raises(HostedClientError):
        parse(value)


def test_budget_refusal_reports_worst_case_but_holds_nothing() -> None:
    value = early("budget_exhausted")
    cost = receipt()["cost"]
    cost.pop("tariff_estimate_micro_usd")
    cost["held"] = False
    value["receipt"].update(cost=cost, budget=receipt()["budget"])
    parsed = parse(value)
    assert parsed.receipt.cost is not None and not parsed.receipt.cost.held
    assert parsed.receipt.cost.reservation_micro_usd == 100
    value["receipt"]["cost"]["held"] = True
    with pytest.raises(HostedClientError):
        parse(value)


@pytest.mark.parametrize("field", ["cost", "budget", "completion"])
def test_provider_receipt_requires_computed_fields(field: str) -> None:
    value = live()
    value["receipt"].pop(field)
    with pytest.raises(HostedClientError):
        parse(value)


@pytest.mark.parametrize("held", [False, "true", 1])
def test_unknown_outcome_requires_strict_held_reservation(held: object) -> None:
    value = live()
    value.pop("artifact")
    value["status"] = "ambiguous"
    value["normalized_error"] = {
        "code": "RESULT_AMBIGUOUS",
        "message": "Synthetic unknown.",
    }
    value["receipt"]["reason"] = "unknown_outcome"
    value["receipt"].pop("completion")
    value["receipt"]["cost"].update(held=held, tariff_estimate_micro_usd=None)
    with pytest.raises(AmbiguousModelReceiptError):
        parse(value)


@pytest.mark.parametrize("field", ["tariff_estimate_micro_usd", "incomplete"])
def test_budget_refusal_cannot_report_uncomputed_usage_estimate(field: str) -> None:
    value = early("budget_exhausted")
    cost = receipt()["cost"]
    cost.pop("tariff_estimate_micro_usd")
    cost["held"] = False
    cost[field] = None if field == "tariff_estimate_micro_usd" else []
    value["receipt"].update(cost=cost, budget=receipt()["budget"])
    with pytest.raises(HostedClientError):
        parse(value)


@pytest.mark.parametrize(
    "field",
    [
        "model_requested",
        "request_sha256",
        "request_size_bytes",
        "cost",
        "budget",
        "request_artifact",
        "usage",
        "usage_provider",
    ],
)
def test_lifecycle_refusal_has_no_uncomputed_request_fields(field: str) -> None:
    value = early("connection_revoked")
    value["normalized_error"]["code"] = "CONNECTION_REVOKED"
    value["receipt"] = {
        "draft_schema": receipt()["draft_schema"],
        "capability": "text_completion",
        "reason": "connection_revoked",
        field: None,
    }
    with pytest.raises(HostedClientError):
        parse(value)


def test_old_missing_lifecycle_reason_is_rejected() -> None:
    value = early()
    value["normalized_error"]["code"] = "CONNECTION_REVOKED"
    value["receipt"] = {
        "draft_schema": receipt()["draft_schema"],
        "capability": "text_completion",
    }
    with pytest.raises(HostedClientError):
        parse(value)


def test_ambiguous_missing_reason_preserves_dispatch_uncertainty() -> None:
    value = live()
    value.pop("artifact")
    value["status"] = "ambiguous"
    value["normalized_error"] = {
        "code": "RESULT_AMBIGUOUS",
        "message": "Synthetic unknown.",
    }
    value["receipt"].pop("reason", None)
    with pytest.raises(
        AmbiguousModelReceiptError, match="may have been dispatched; do not retry"
    ):
        parse(value)


@pytest.mark.parametrize(
    "field", ["reservation_micro_usd", "tariff_estimate_micro_usd"]
)
def test_cost_counters_fit_canonical_json(field: str) -> None:
    value = live()
    value["receipt"]["cost"][field] = 1 << 53
    with pytest.raises(HostedClientError):
        parse(value)


@pytest.mark.parametrize(
    "field",
    [
        "apiKey",
        "x-api-key",
        "bearer",
        "auth_token",
        "id_token",
        "secretkey",
        "privatekey",
        "apisecret",
        "nested_private_key_value",
    ],
)
def test_native_usage_rejects_normalized_secret_names(field: str) -> None:
    value = live()
    value["receipt"]["usage_provider"] = {"nested": {field: "synthetic-private-text"}}
    with pytest.raises(HostedClientError) as error:
        parse(value)
    assert "synthetic-private-text" not in str(error.value)


@pytest.mark.parametrize(
    "path",
    [
        ("receipt", "budget", "authority_remaining_micro_usd"),
        ("receipt", "budget", "day_calls_remaining"),
        ("receipt", "budget", "day_spend_remaining_micro_usd"),
        ("artifact", "size_bytes"),
        ("receipt", "cost", "reservation_micro_usd"),
        ("receipt", "cost", "tariff_estimate_micro_usd"),
    ],
)
def test_money_and_size_canonical_json_boundary(path: tuple[str, ...]) -> None:
    value = live()
    target = value
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = (1 << 53) - 1
    if path == ("receipt", "cost", "tariff_estimate_micro_usd"):
        value["receipt"]["cost"]["reservation_micro_usd"] = (1 << 53) - 1
    expected_size = ((1 << 53) - 1) if path == ("receipt", "request_size_bytes") else 32
    if path == ("artifact", "size_bytes"):
        value["receipt"]["completion"]["size_bytes"] = (1 << 53) - 1
    assert (
        parse_model_response(
            HostedOperationResponse.model_validate(value),
            expected_model="test-model-20261014",
            catalogue_revision="draft-test",
            expected_request_sha256="sha256:" + "a" * 64,
            expected_request_size_bytes=expected_size,
        ).completion
        is not None
    )
    target[path[-1]] = 1 << 53
    with pytest.raises(HostedClientError):
        parse(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("draft_schema", "a" * 64),
        ("request_sha256", "sha256:" + "b" * 64),
        ("request_size_bytes", 99),
        ("truncated", None),
        ("model_mismatch", True),
    ],
)
def test_consolidated_binding_and_flags(field: str, value: object) -> None:
    response = live()
    response["receipt"][field] = value
    with pytest.raises(HostedClientError):
        parse(response)


def test_truncation_is_explicit_on_confirmed_delivery() -> None:
    response = live()
    response["receipt"].update(stop_reason="max_tokens", truncated=True)
    assert parse(response).receipt.truncated is True
    response["receipt"]["truncated"] = False
    with pytest.raises(HostedClientError):
        parse(response)


def test_uncatalogued_reported_model_requires_incomplete_estimate() -> None:
    response = live()
    response["receipt"].update(model_reported="other-model", model_mismatch=True)
    with pytest.raises(HostedClientError):
        parse(response)
    response["receipt"]["cost"].update(
        tariff_estimate_micro_usd=None, incomplete=["model_mismatch"]
    )
    assert parse(response).receipt.model_mismatch is True


def test_overshoot_is_retained_and_must_match_supplied_amounts() -> None:
    value = live()
    value["receipt"]["cost"].update(
        tariff_estimate_micro_usd=120, overshoot_micro_usd=20
    )
    cost = parse(value).receipt.cost
    assert cost is not None and cost.overshoot_micro_usd == 20
    value["receipt"]["cost"]["overshoot_micro_usd"] = 19
    with pytest.raises(HostedClientError):
        parse(value)


@pytest.mark.parametrize("field", ["request_sha256", "request_size_bytes"])
def test_bad_request_forbids_validated_fingerprint(field: str) -> None:
    value = early()
    value["receipt"][field] = receipt()[field]
    with pytest.raises(HostedClientError):
        parse(value)


def test_content_filter_delivery_is_explicitly_truncated() -> None:
    value = live()
    value["receipt"].update(stop_reason="content_filter", truncated=True)
    assert parse(value).receipt.truncated is True
    value["receipt"]["truncated"] = False
    with pytest.raises(HostedClientError):
        parse(value)


def test_incomplete_estimate_cannot_release_hold() -> None:
    value = live()
    value["receipt"]["cost"].update(
        tariff_estimate_micro_usd=None, incomplete=["missing_rate"], held=False
    )
    with pytest.raises(HostedClientError):
        parse(value)


@pytest.mark.parametrize(
    "field",
    [
        "stop_reason",
        "stop_reason_provider",
        "truncated",
        "model_mismatch",
        "excluded_blocks",
    ],
)
def test_budget_refusal_forbids_provider_outcome_fields(field: str) -> None:
    value = early("budget_exhausted")
    cost = receipt()["cost"]
    cost.pop("tariff_estimate_micro_usd")
    cost["held"] = False
    value["receipt"].update(
        cost=cost, budget=receipt()["budget"], **{field: receipt().get(field, {})}
    )
    with pytest.raises(HostedClientError):
        parse(value)


def test_validated_request_size_is_capped_to_canonical_limit() -> None:
    value = live()
    value["receipt"]["request_size_bytes"] = 256 * 1024 + 1
    with pytest.raises(HostedClientError):
        parse(value)
