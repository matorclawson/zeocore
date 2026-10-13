"""Synthetic draft receipts: no transport, provider or authentic usage evidence."""

from copy import deepcopy
from typing import Any

import pytest

from zeo_core.integrations.hosted.client import (
    HostedClientError,
    HostedOperationResponse,
)
from zeo_core.integrations.model_ops import ParsedModelResponse, parse_model_response


def receipt() -> dict[str, Any]:
    return {
        "capability": "text_completion",
        "model_requested": "test-model-20261014",
        "usage": {"cache_read_tokens": 20},
        "usage_provider": {"input_tokens": 30, "details": {"cached": 20}},
        "cost": {
            "reservation_micro_usd": 100,
            "tariff_estimate_micro_usd": 10,
            "rates_source": {
                "revision": "draft-test",
                "read_on": "2026-10-14",
                "pages": ["https://example.invalid/rates"],
            },
        },
        "budget": {
            "authority_remaining_micro_usd": 900,
            "day_calls_remaining": 4,
            "day_spend_remaining_micro_usd": 900,
        },
        "request_artifact": artifact("request.json", "application/json")
        | {"state": "available"},
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
    r["request_artifact"]["state"] = "expired"
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
        ("blocked", "failed_safe", "REQUEST_REFUSED"),
        ("provider_refused", "failed_safe", "REQUEST_REFUSED"),
        ("rate_limited", "failed_safe", "RATE_LIMITED"),
        ("provider_unavailable", "failed_safe", "PROVIDER_UNAVAILABLE"),
        ("unknown_outcome", "ambiguous", "RESULT_AMBIGUOUS"),
        (None, "refused", "CONNECTION_REVOKED"),
        (None, "refused", "CONNECTION_REPAIR_REQUIRED"),
        (None, "refused", "CONNECTOR_REVISION_UNAVAILABLE"),
    ],
)
def test_fixed_error_mapping(reason: str | None, status: str, code: str) -> None:
    r = receipt()
    r["reason"] = reason
    r["cost"]["tariff_estimate_micro_usd"] = None
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
        (("request_artifact", "state"), "expired"),
        (("request_artifact", "filename"), "other.json"),
        (("request_artifact", "size_bytes"), True),
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
        (("receipt", "request_artifact", "state"), "available"),
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
        )
    assert "synthetic-private-text" not in str(error.value)


def test_incomplete_estimate_preserves_missing_rate() -> None:
    value = live()
    value["receipt"]["cost"].update(
        tariff_estimate_micro_usd=None, incomplete=["cache_read_tokens"]
    )
    assert parse(value).receipt.cost.tariff_estimate_micro_usd is None
