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

from .models import MAX_SAFE_JSON_INTEGER

Count = Annotated[int, Field(strict=True, ge=0, le=MAX_SAFE_JSON_INTEGER)]
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
    "connection_revoked",
    "connection_repair_required",
    "connector_revision_unavailable",
]


def _secret_usage_name(name: str) -> bool:
    normalized = "".join(c for c in name.lower() if c.isalnum())
    markers = (
        "secret",
        "password",
        "credential",
        "privatekey",
        "apikey",
        "authorization",
        "bearer",
        "authtoken",
        "idtoken",
        "accesstoken",
        "refreshtoken",
    )
    return normalized == "token" or any(marker in normalized for marker in markers)


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
    held: bool = Field(strict=True)
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
    """Project model evidence while allowing common member-receipt metadata.

    Extra metadata is ignored for hosted protocol forward compatibility; known
    model fields are validated and absent stage fields are never invented.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)
    capability: Literal["text_completion"]
    reason: Reason | None = None
    model_requested: str | None = Field(default=None, min_length=1, max_length=200)
    model_reported: str | None = Field(default=None, min_length=1, max_length=200)
    usage: UsageComponents = Field(default_factory=UsageComponents)
    usage_provider: dict[NativeKey, NativeValue | dict[NativeKey, NativeValue]] = Field(
        default_factory=dict, max_length=32
    )
    cost: ModelCost | None = None
    budget: BudgetRemaining | None = None
    request_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    request_size_bytes: Count | None = None
    request_artifact: ReceiptArtifact | None = None
    completion: ReceiptArtifact | None = None
    provider_request_id: str | None = Field(default=None, min_length=1, max_length=500)
    replayed: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def _stage_fields(self) -> ModelReceipt:
        if self.reason in _LIFECYCLE_REASONS:
            if self.model_fields_set - {"capability", "reason", "replayed"}:
                raise ValueError("lifecycle refusal carries uncomputed fields")
            return self
        pre_budget = self.reason in {"bad_request", "model_not_allowed"}
        budget_refused = self.reason == "budget_exhausted"
        if pre_budget:
            forbidden = {
                "cost",
                "budget",
                "request_artifact",
                "completion",
                "usage",
                "usage_provider",
                "provider_request_id",
                "model_reported",
            }
            if forbidden & self.model_fields_set:
                raise ValueError("pre-budget refusal carries uncomputed fields")
        else:
            if self.cost is None or self.budget is None:
                raise ValueError("budget-read receipt requires cost and budget")
        if pre_budget or budget_refused:
            if self.request_sha256 is None or self.request_size_bytes is None:
                raise ValueError("request fingerprint is required")
        return self

    @model_validator(mode="after")
    def _provider_fields(self) -> ModelReceipt:
        pre_budget = self.reason in {"bad_request", "model_not_allowed"}
        budget_refused = self.reason == "budget_exhausted"
        if self.reason in _LIFECYCLE_REASONS:
            return self
        if not pre_budget and not budget_refused:
            if self.request_artifact is None:
                raise ValueError("provider-reached receipt requires request artifact")
            if (
                self.reason == "unknown_outcome"
                and self.cost is not None
                and not self.cost.held
            ):
                raise ValueError("unknown outcome must retain the reservation")
        return self

    @model_validator(mode="after")
    def _budget_refusal(self) -> ModelReceipt:
        if self.reason == "budget_exhausted":
            forbidden = {
                "request_artifact",
                "completion",
                "usage",
                "usage_provider",
                "provider_request_id",
                "model_reported",
            }
            if forbidden & self.model_fields_set or self.cost is None or self.cost.held:
                raise ValueError(
                    "budget refusal must not hold funds or carry provider evidence"
                )
            if {"tariff_estimate_micro_usd", "incomplete"} & self.cost.model_fields_set:
                raise ValueError("budget refusal carries no usage estimate")
        return self

    @model_validator(mode="after")
    def _native_usage_bounded(self) -> ModelReceipt:
        keys = len(self.usage_provider)
        for key, value in self.usage_provider.items():
            if _secret_usage_name(key):
                raise ValueError("secret-bearing native usage field")
            if isinstance(value, dict):
                keys += len(value)
                if any(_secret_usage_name(name) for name in value):
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
_LIFECYCLE_REASONS = {
    "connection_revoked": NormalizedErrorCode.CONNECTION_REVOKED,
    "connection_repair_required": NormalizedErrorCode.CONNECTION_REPAIR_REQUIRED,
    "connector_revision_unavailable": (
        NormalizedErrorCode.CONNECTOR_REVISION_UNAVAILABLE
    ),
}
_REASON_MAPPING.update(
    {
        reason: (HostedOperationStatus.REFUSED, code)
        for reason, code in _LIFECYCLE_REASONS.items()
    }
)


class AmbiguousModelReceiptError(HostedClientError):
    """Parsing failed after an ambiguous response; dispatch cannot be ruled out."""


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
        ReceiptArtifact.model_validate(
            response.artifact.model_dump() | {"state": "available"}
        )
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
    else:
        raise ValueError("missing refusal reason")
    if (
        response.status is HostedOperationStatus.AMBIGUOUS
        and receipt.cost is not None
        and receipt.cost.tariff_estimate_micro_usd is not None
    ):
        raise ValueError("unknown outcome cannot carry a usage estimate")


def parse_model_response(
    response: HostedOperationResponse, *, expected_model: str, catalogue_revision: str
) -> ParsedModelResponse:
    """Refuse mismatched evidence without returning raw provider/input text."""
    ambiguous = response.status == HostedOperationStatus.AMBIGUOUS
    try:
        response = HostedOperationResponse.model_validate(response.model_dump())
        receipt = ModelReceipt.model_validate(response.receipt)
        if (
            receipt.model_requested is not None
            and receipt.model_requested != expected_model
        ):
            raise ValueError("model mismatch")
        if (
            receipt.cost is not None
            and receipt.cost.rates_source.revision != catalogue_revision
        ):
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
        if ambiguous:
            raise AmbiguousModelReceiptError(
                "model operation receipt is invalid; "
                "the call may have been dispatched; "
                "do not retry until its outcome and reservation are settled"
            ) from None
        raise HostedClientError(
            "model operation receipt is invalid; inspect the receiving contract and "
            "retained evidence without assuming the call was not dispatched"
        ) from None
