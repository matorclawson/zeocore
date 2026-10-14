"""Bounded draft receipt projections; parsing never authenticates provider use."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isfinite
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    HttpUrl,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from zeo_core.contracts.connections import NormalizedErrorCode
from zeo_core.integrations.hosted.client import (
    HostedArtifactDescriptor,
    HostedClientError,
    HostedOperationResponse,
    HostedOperationStatus,
)

from .models import MAX_REQUEST_BYTES, MAX_SAFE_JSON_INTEGER


def _integral_count(value: object) -> object:
    if isinstance(value, float) and isfinite(value) and value.is_integer():
        return int(value)
    return value


Count = Annotated[
    int,
    Field(strict=True, ge=0, le=MAX_SAFE_JSON_INTEGER),
    BeforeValidator(_integral_count),
]
NativeKey = Annotated[str, Field(min_length=1, max_length=128)]
Reason = Literal[
    "model_not_allowed",
    "bad_request",
    "budget_exhausted",
    "request_in_progress",
    "idempotency_key_reused",
    "blocked",
    "provider_refused",
    "rate_limited",
    "provider_unavailable",
    "unknown_outcome",
    "connection_revoked",
    "connection_repair_required",
    "connector_revision_unavailable",
]

_RATE_URL: TypeAdapter[HttpUrl] = TypeAdapter(HttpUrl)


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


def _native_children(item: object, depth: int) -> list[object] | None:
    if isinstance(item, dict):
        if depth > 2:
            raise ValueError("native usage exceeds two container levels")
        for name in item:
            if not isinstance(name, str):
                raise ValueError("native usage key is invalid")
            if _secret_usage_name(name):
                raise ValueError("secret-bearing native usage field")
        return list(item.values())
    if isinstance(item, list):
        if depth > 2:
            raise ValueError("native usage exceeds two container levels")
        return item
    return None


def _native_scalar(item: object) -> None:
    if isinstance(item, float):
        if not isfinite(item):
            raise ValueError("native usage number must be finite")
        if item.is_integer() and abs(item) > MAX_SAFE_JSON_INTEGER:
            raise ValueError("native integral value exceeds interoperable domain")
    elif isinstance(item, int):
        if abs(item) > MAX_SAFE_JSON_INTEGER:
            raise ValueError("native usage integer exceeds interoperable domain")
    elif item is not None and not isinstance(item, (int, str)):
        raise ValueError("native usage value must be JSON")


class UsageComponents(_Closed):
    input_uncached_tokens: Count | None = None
    cache_read_tokens: Count | None = None
    cache_write_tokens: Count | None = None
    output_tokens: Count | None = None
    reasoning_tokens: Count | None = None
    other: dict[NativeKey, Count] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="before")
    @classmethod
    def _components_not_null(cls, value: object) -> object:
        if isinstance(value, dict) and any(
            name in cls.model_fields and child is None for name, child in value.items()
        ):
            raise ValueError("usage components are absent or valid, never null")
        return value


class RateSources(_Closed):
    revision: str = Field(min_length=1, max_length=200)
    read_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    pages: tuple[Annotated[str, Field(min_length=1, max_length=2048)], ...] = Field(
        min_length=1, max_length=8
    )

    @model_validator(mode="after")
    def _calendar_date(self) -> RateSources:
        date.fromisoformat(self.read_on)
        for page in self.pages:
            _RATE_URL.validate_python(page)
        return self


class ModelCost(_Closed):
    reservation_micro_usd: Count
    held: bool = Field(strict=True)
    tariff_estimate_micro_usd: Count | None = None
    incomplete: tuple[
        Literal["usage", "cache_write", "reasoning", "model", "price", "blocks"], ...
    ] = Field(default=(), max_length=32)
    rates_source: RateSources
    overshoot_micro_usd: Count | None = None

    @model_validator(mode="before")
    @classmethod
    def _computed_amount_not_null(cls, value: object) -> object:
        if isinstance(value, dict) and any(
            key in value and value[key] is None
            for key in ("tariff_estimate_micro_usd", "overshoot_micro_usd")
        ):
            raise ValueError("uncomputed amounts are absent, never null")
        return value

    @model_validator(mode="after")
    def _incomplete_estimate_not_claimed(self) -> ModelCost:
        if self.incomplete and not self.held:
            raise ValueError("incomplete estimates must retain the reservation")
        if self.incomplete and self.tariff_estimate_micro_usd is not None:
            raise ValueError("incomplete rates cannot yield a complete estimate")
        return self

    @model_validator(mode="after")
    def _overshoot_consistent(self) -> ModelCost:
        estimate = self.tariff_estimate_micro_usd
        if estimate is not None and estimate > self.reservation_micro_usd:
            if self.overshoot_micro_usd != estimate - self.reservation_micro_usd:
                raise ValueError("overshoot must match the supplied estimate excess")
        elif self.overshoot_micro_usd is not None:
            raise ValueError("overshoot is absent without a supplied estimate excess")
        return self


class BudgetRemaining(_Closed):
    provider: Literal["anthropic", "openai", "google"]
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    scope: Literal["broker_total", "with_external_entries"]
    authority_remaining_micro_usd: Count
    day_calls_remaining: Count
    day_spend_remaining_micro_usd: Count

    @model_validator(mode="after")
    def _calendar_date(self) -> BudgetRemaining:
        date.fromisoformat(self.day)
        return self


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
    draft_schema: Literal[
        "model-ops-draft-11 sha256:"
        "38fe80e0ac6e35f045d1dd518da9ca31dab19c2c33c26f1d5a042643c8cdff11"
    ]
    capability: Literal["text_completion"]
    reason: Reason | None = None
    detail: (
        Literal[
            "caps_missing",
            "authority",
            "daily_calls",
            "daily_spend",
            "overshoot",
            "unpriced",
            "ledger_inconsistent",
        ]
        | None
    ) = None
    model_requested: str | None = Field(default=None, min_length=1, max_length=200)
    model_reported: str | None = Field(default=None, min_length=1, max_length=200)
    usage: UsageComponents = Field(default_factory=UsageComponents)
    usage_provider: dict[str, JsonValue] = Field(default_factory=dict, max_length=32)
    usage_provider_omitted: bool | None = Field(default=None, strict=True)
    cost: ModelCost | None = None
    budget: BudgetRemaining | None = None
    request_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    request_size_bytes: Count | None = Field(default=None, le=MAX_REQUEST_BYTES)
    completion: ReceiptArtifact | None = None
    excluded_blocks: dict[NativeKey, Count] = Field(default_factory=dict, max_length=32)
    provider_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    replayed: bool = Field(default=False, strict=True)
    stop_reason: (
        Literal["end", "max_tokens", "stop_sequence", "content_filter", "other"] | None
    ) = None
    stop_reason_provider: str | None = Field(default=None, min_length=1, max_length=200)
    truncated: bool | None = Field(default=None, strict=True)
    model_mismatch: bool | None = Field(default=None, strict=True)

    @model_validator(mode="before")
    @classmethod
    def _no_request_artifact(cls, value: object) -> object:
        if isinstance(value, dict) and "request_artifact" in value:
            raise ValueError("request artifacts are withdrawn in this draft")
        if isinstance(value, dict) and any(
            name in cls.model_fields and child is None for name, child in value.items()
        ):
            raise ValueError("receipt fields are absent or valid, never null")
        return value

    @model_validator(mode="after")
    def _stage_fields(self) -> ModelReceipt:
        self._fingerprint_requiredness()
        self._provider_projection_flags()
        early = self.reason in _EARLY_REASONS or self._budget_unread()
        if early:
            forbidden = {
                "cost",
                "budget",
                "completion",
                "usage",
                "usage_provider",
                "provider_request_id",
                "model_reported",
                "stop_reason",
                "stop_reason_provider",
                "truncated",
                "model_mismatch",
                "excluded_blocks",
            }
            if forbidden & self.model_fields_set:
                raise ValueError("pre-budget refusal carries uncomputed fields")
        elif self.cost is None or self.budget is None:
            raise ValueError("budget-read receipt requires cost and budget")
        if (
            self.reason == "unknown_outcome"
            and self.cost is not None
            and not self.cost.held
        ):
            raise ValueError("unknown outcome must retain the reservation")
        return self

    def _provider_projection_flags(self) -> None:
        if self.reason in _EARLY_REASONS or self.reason == "budget_exhausted":
            if {"replayed", "usage_provider_omitted"} & self.model_fields_set:
                raise ValueError("pre-reservation refusal has no stored replay flags")
            return
        if "replayed" not in self.model_fields_set:
            raise ValueError("provider-stage receipt requires replay status")
        if "usage_provider_omitted" in self.model_fields_set and (
            self.usage_provider_omitted is not True
            or "usage_provider" in self.model_fields_set
        ):
            raise ValueError("native omission marker requires absent native data")

    def _budget_unread(self) -> bool:
        return self.reason == "budget_exhausted" and self.detail in {
            "caps_missing",
            "unpriced",
            "ledger_inconsistent",
        }

    def _fingerprint_requiredness(self) -> None:
        present = {"request_sha256", "request_size_bytes"} & self.model_fields_set
        if self.reason == "bad_request":
            if present:
                raise ValueError("bad request carries no validated fingerprint")
            return
        optional_request = self.reason in _LIFECYCLE_REASONS
        if not optional_request and self.model_requested is None:
            raise ValueError("requested model is required after validation")
        if not optional_request or present:
            if self.request_sha256 is None or self.request_size_bytes is None:
                raise ValueError("validated request fingerprint is required")

    @model_validator(mode="after")
    def _reported_outcome(self) -> ModelReceipt:
        if "model_requested" in self.model_fields_set and self.model_requested is None:
            raise ValueError("requested model is absent unless it is a string")
        if (
            self.stop_reason in {"max_tokens", "content_filter"}
            and self.truncated is not True
        ):
            raise ValueError("max-token completion must report truncation")
        if self.model_reported is not None:
            if self.model_requested is None or self.model_mismatch is None:
                raise ValueError(
                    "reported model requires requested identity and mismatch flag"
                )
        elif "model_mismatch" in self.model_fields_set:
            raise ValueError("mismatch flag is absent without a reported model")
        return self

    @model_validator(mode="after")
    def _budget_refusal(self) -> ModelReceipt:
        if self.reason == "budget_exhausted":
            if self.detail is None:
                raise ValueError("budget refusal requires a fixed detail")
            if self._budget_unread():
                return self
            forbidden = {
                "request_artifact",
                "completion",
                "usage",
                "usage_provider",
                "provider_request_id",
                "model_reported",
                "stop_reason",
                "stop_reason_provider",
                "truncated",
                "model_mismatch",
                "excluded_blocks",
            }
            if forbidden & self.model_fields_set or self.cost is None or self.cost.held:
                raise ValueError(
                    "budget refusal must not hold funds or carry provider evidence"
                )
            if {
                "tariff_estimate_micro_usd",
                "incomplete",
                "overshoot_micro_usd",
            } & self.cost.model_fields_set:
                raise ValueError("budget refusal carries no usage estimate")
        elif "detail" in self.model_fields_set:
            raise ValueError("budget detail belongs only to a budget refusal")
        return self

    @field_validator("usage_provider", mode="before")
    @classmethod
    def _native_usage_bounded(cls, value: object) -> object:
        if not isinstance(value, dict):
            raise ValueError("native usage must be a JSON object")
        pending: list[tuple[object, int]] = [(value, 1)]
        keys = 0
        while pending:
            item, depth = pending.pop()
            if isinstance(item, dict):
                keys += len(item)
                if keys > 32:
                    raise ValueError("native usage exceeds total key limit")
            children = _native_children(item, depth)
            if children is None:
                _native_scalar(item)
            else:
                pending.extend((child, depth + 1) for child in children)
        return value

    @model_validator(mode="after")
    def _usage_estimate_presence(self) -> ModelReceipt:
        if self.reason in _EARLY_REASONS or self.reason == "budget_exhausted":
            return self
        reported = bool(self.usage.other) or any(
            value is not None
            for value in self.usage.model_dump(exclude={"other"}).values()
        )
        if self.cost is not None:
            required = reported and not self.cost.incomplete
            if required != (self.cost.tariff_estimate_micro_usd is not None):
                raise ValueError("tariff estimate requires known complete usage")
        return self


_REASON_MAPPING = {
    "request_in_progress": (
        HostedOperationStatus.REFUSED,
        NormalizedErrorCode.REQUEST_REFUSED,
    ),
    "idempotency_key_reused": (
        HostedOperationStatus.REFUSED,
        NormalizedErrorCode.REQUEST_REFUSED,
    ),
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

_EARLY_REASONS = {
    "bad_request",
    "model_not_allowed",
    "request_in_progress",
    "idempotency_key_reused",
    *_LIFECYCLE_REASONS,
}


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
    if receipt.reason is not None or receipt.model_requested is None:
        raise ValueError("completed receipt is not bound to requested model")
    if (
        receipt.model_reported is None
        or receipt.stop_reason is None
        or receipt.stop_reason_provider is None
        or receipt.truncated is None
    ):
        raise ValueError("completed receipt requires reported outcome")
    if receipt.completion is None:
        raise ValueError("completed receipt requires completion metadata")
    _confirmed_usage(receipt)
    if response.artifact is not None:
        ReceiptArtifact.model_validate(
            response.artifact.model_dump() | {"state": "available"}
        )
        if (
            response.artifact.media_type != "text/plain"
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


def _confirmed_usage(receipt: ModelReceipt) -> None:
    usage = receipt.usage.model_dump(exclude={"other"})
    reported = bool(receipt.usage.other) or any(
        value is not None for value in usage.values()
    )
    if not reported and receipt.cost is not None:
        if not receipt.cost.held or receipt.cost.tariff_estimate_micro_usd is not None:
            raise ValueError(
                "confirmed without usage retains its reservation without an estimate"
            )


def _not_confirmed(response: HostedOperationResponse, receipt: ModelReceipt) -> None:
    if {
        "completion",
        "stop_reason",
        "stop_reason_provider",
        "truncated",
    } & receipt.model_fields_set:
        raise ValueError("non-confirmed receipt cannot carry completion outcome fields")
    error = response.normalized_error
    if error is None:
        raise ValueError("missing normalized refusal")
    if receipt.reason is not None:
        expected_status, expected_code = _REASON_MAPPING[receipt.reason]
        if response.status != expected_status or error.code != expected_code:
            raise ValueError("reason mapping mismatch")
    else:
        raise ValueError("missing refusal reason")


def _request_evidence(receipt: ModelReceipt, digest: str, size: int) -> None:
    if receipt.request_sha256 is not None and (
        receipt.request_sha256 != digest or receipt.request_size_bytes != size
    ):
        raise ValueError("caller-retained request fingerprint mismatch")


def _reported_model_evidence(
    receipt: ModelReceipt, catalogued: tuple[str, ...]
) -> None:
    mismatch = (
        receipt.model_reported is not None
        and receipt.model_reported != receipt.model_requested
    )
    if receipt.model_reported is not None and receipt.model_mismatch != mismatch:
        raise ValueError("reported/requested mismatch flag is inconsistent")
    uncatalogued = mismatch and receipt.model_reported not in catalogued
    if receipt.cost is not None and (
        ("model" in receipt.cost.incomplete) != uncatalogued
    ):
        raise ValueError(
            "incomplete model component must match uncatalogued difference"
        )


def parse_model_response(
    response: HostedOperationResponse,
    *,
    expected_model: str,
    catalogue_revision: str,
    expected_request_sha256: str,
    expected_request_size_bytes: int,
    catalogued_reported_models: tuple[str, ...],
) -> ParsedModelResponse:
    """Refuse mismatched evidence without returning raw provider/input text."""
    ambiguous = True
    try:
        status = response.status
        if isinstance(status, HostedOperationStatus):
            ambiguous = status is HostedOperationStatus.AMBIGUOUS
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
        _request_evidence(receipt, expected_request_sha256, expected_request_size_bytes)
        _reported_model_evidence(receipt, catalogued_reported_models)
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
    except ValidationError, ValueError, TypeError, KeyError, AttributeError:
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
