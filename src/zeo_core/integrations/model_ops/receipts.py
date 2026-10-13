"""Bounded draft receipt projections; parsing never authenticates provider use."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from zeo_core.contracts.connections import NormalizedErrorCode
from zeo_core.integrations.hosted.client import (
    HostedArtifactDescriptor,
    HostedClientError,
    HostedOperationResponse,
    HostedOperationStatus,
)

RECEIPT_SCHEMA_DRAFT_SHA256 = (
    "e737e8fe615143e776b746d1c7547693e7b29a749f6ca8562fb7057aa6b0e007"
)
Count = Annotated[int, Field(strict=True, ge=0)]
NativeValue = Count | Annotated[str, Field(strict=True, max_length=200)]
NativeKey = Annotated[str, Field(min_length=1, max_length=128)]
Reason = Literal[
    "model_not_allowed",
    "bad_request",
    "budget_exhausted",
    "blocked",
    "provider_refused",
    "rate_limited",
    "provider_unavailable",
    "unknown_outcome",
]


class _Closed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class UsageComponents(_Closed):
    input_uncached_tokens: Count | None = None
    cache_read_tokens: Count | None = None
    cache_write_tokens: Count | None = None
    output_tokens: Count | None = None
    reasoning_tokens: Count | None = None
    other: dict[NativeKey, Count] = Field(default_factory=dict, max_length=32)


class RateSources(_Closed):
    revision: str = Field(min_length=1, max_length=200)
    read_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    pages: tuple[Annotated[str, Field(min_length=1, max_length=2048)], ...] = Field(
        min_length=1, max_length=32
    )

    @model_validator(mode="after")
    def _calendar_date(self) -> RateSources:
        date.fromisoformat(self.read_on)
        return self


class ModelCost(_Closed):
    reservation_micro_usd: Count
    tariff_estimate_micro_usd: Count | None = None
    incomplete: tuple[Annotated[str, Field(min_length=1, max_length=128)], ...] = Field(
        default=(), max_length=32
    )
    rates_source: RateSources

    @model_validator(mode="after")
    def _incomplete_estimate_not_claimed(self) -> ModelCost:
        if self.incomplete and self.tariff_estimate_micro_usd is not None:
            raise ValueError("incomplete rates cannot yield a complete estimate")
        return self


class BudgetRemaining(_Closed):
    authority_remaining_micro_usd: Count
    day_calls_remaining: Count
    day_spend_remaining_micro_usd: Count


class ReceiptArtifact(HostedArtifactDescriptor):
    """Receipt metadata has state; the shared live descriptor remains unchanged."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    size_bytes: Count
    state: Literal["available", "expired"]


class ExpiredCompletion(_Closed):
    expired: bool = Field(strict=True)
    content_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: Count

    @model_validator(mode="after")
    def _expired_is_true(self) -> ExpiredCompletion:
        if not self.expired:
            raise ValueError("expired marker must be true")
        return self


class ModelReceipt(BaseModel):
    """Project model evidence while allowing common member-receipt metadata."""

    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)
    capability: Literal["text_completion"]
    reason: Reason | None = None
    model_requested: str | None = Field(default=None, min_length=1, max_length=200)
    model_reported: str | None = Field(default=None, min_length=1, max_length=200)
    usage: UsageComponents = Field(default_factory=UsageComponents)
    usage_provider: dict[NativeKey, NativeValue | dict[NativeKey, NativeValue]] = Field(
        default_factory=dict, max_length=32
    )
    cost: ModelCost
    budget: BudgetRemaining
    request_artifact: ReceiptArtifact | None = None
    completion: ReceiptArtifact | None = None
    provider_request_id: str | None = Field(default=None, min_length=1, max_length=500)
    replayed: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def _native_usage_bounded(self) -> ModelReceipt:
        keys = len(self.usage_provider)
        forbidden = {
            "token",
            "secret",
            "password",
            "credential",
            "credentials",
            "api_key",
            "access_token",
            "refresh_token",
            "client_secret",
            "authorization",
        }
        for key, value in self.usage_provider.items():
            if key.lower() in forbidden:
                raise ValueError("secret-bearing native usage field")
            if isinstance(value, dict):
                keys += len(value)
                if any(name.lower() in forbidden for name in value):
                    raise ValueError("secret-bearing native usage field")
        if keys > 32:
            raise ValueError("native usage exceeds total key limit")
        return self


_REASON_MAPPING = {
    "model_not_allowed": (
        HostedOperationStatus.REFUSED,
        NormalizedErrorCode.REQUEST_REFUSED,
    ),
    "bad_request": (HostedOperationStatus.REFUSED, NormalizedErrorCode.REQUEST_REFUSED),
    "budget_exhausted": (
        HostedOperationStatus.REFUSED,
        NormalizedErrorCode.REQUEST_REFUSED,
    ),
    "blocked": (HostedOperationStatus.FAILED_SAFE, NormalizedErrorCode.REQUEST_REFUSED),
    "provider_refused": (
        HostedOperationStatus.FAILED_SAFE,
        NormalizedErrorCode.REQUEST_REFUSED,
    ),
    "rate_limited": (
        HostedOperationStatus.FAILED_SAFE,
        NormalizedErrorCode.RATE_LIMITED,
    ),
    "provider_unavailable": (
        HostedOperationStatus.FAILED_SAFE,
        NormalizedErrorCode.PROVIDER_UNAVAILABLE,
    ),
    "unknown_outcome": (
        HostedOperationStatus.AMBIGUOUS,
        NormalizedErrorCode.RESULT_AMBIGUOUS,
    ),
}
_LIFECYCLE_CODES = {
    NormalizedErrorCode.CONNECTION_REVOKED,
    NormalizedErrorCode.CONNECTION_REPAIR_REQUIRED,
    NormalizedErrorCode.CONNECTOR_REVISION_UNAVAILABLE,
}


@dataclass(frozen=True)
class ParsedModelResponse:
    """Schema conformance only: no verified call, cost settlement or retry grant."""

    status: HostedOperationStatus
    execution_id: str
    receipt: ModelReceipt
    completion: HostedArtifactDescriptor | None
    expired: bool


def _confirmed(response: HostedOperationResponse, receipt: ModelReceipt) -> bool:
    request = receipt.request_artifact
    if (
        request is None
        or request.media_type != "application/json"
        or request.filename != "request.json"
    ):
        raise ValueError("request artifact is missing")
    if receipt.reason is not None or receipt.model_requested is None:
        raise ValueError("completed receipt is not bound to requested model")
    if response.artifact is not None:
        if (
            request.state != "available"
            or response.artifact.media_type != "text/plain"
            or response.artifact.filename != "completion.txt"
        ):
            raise ValueError("live artifact metadata mismatch")
        if receipt.completion is not None and (
            receipt.completion.state != "available"
            or receipt.completion.model_dump(exclude={"state"})
            != response.artifact.model_dump()
        ):
            raise ValueError("completion descriptor mismatch")
    else:
        marker = ExpiredCompletion.model_validate(response.result)
        completion = receipt.completion
        if (
            not receipt.replayed
            or request.state != "expired"
            or completion is None
            or completion.state != "expired"
            or completion.media_type != "text/plain"
            or completion.filename != "completion.txt"
            or completion.content_sha256 != marker.content_sha256
            or completion.size_bytes != marker.size_bytes
        ):
            raise ValueError("expired replay metadata mismatch")
        return True
    return False


def _not_confirmed(response: HostedOperationResponse, receipt: ModelReceipt) -> None:
    if receipt.completion is not None:
        raise ValueError("non-confirmed receipt cannot carry a completion")
    error = response.normalized_error
    if error is None:
        raise ValueError("missing normalized refusal")
    if receipt.reason is not None:
        expected_status, expected_code = _REASON_MAPPING[receipt.reason]
        if response.status != expected_status or error.code != expected_code:
            raise ValueError("reason mapping mismatch")
    elif (
        response.status != HostedOperationStatus.REFUSED
        or error.code not in _LIFECYCLE_CODES
    ):
        raise ValueError("unknown refusal mapping")
    if (
        response.status is HostedOperationStatus.AMBIGUOUS
        and receipt.cost.tariff_estimate_micro_usd is not None
    ):
        raise ValueError("unknown outcome cannot carry a usage estimate")


def parse_model_response(
    response: HostedOperationResponse, *, expected_model: str, catalogue_revision: str
) -> ParsedModelResponse:
    """Refuse mismatched evidence without returning raw provider/input text."""
    try:
        response = HostedOperationResponse.model_validate(response.model_dump())
        receipt = ModelReceipt.model_validate(response.receipt)
        if (
            receipt.model_requested is not None
            and receipt.model_requested != expected_model
        ):
            raise ValueError("model mismatch")
        if receipt.cost.rates_source.revision != catalogue_revision:
            raise ValueError("rate revision mismatch")
        expired = False
        if response.status is HostedOperationStatus.CONFIRMED:
            expired = _confirmed(response, receipt)
        else:
            _not_confirmed(response, receipt)
        return ParsedModelResponse(
            status=response.status,
            execution_id=response.execution_id,
            receipt=receipt,
            completion=response.artifact,
            expired=expired,
        )
    except ValidationError, ValueError, TypeError, KeyError:
        raise HostedClientError("model operation receipt is invalid") from None
