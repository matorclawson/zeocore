"""Explicit successor receipt projection; no dispatch or financial authority.

Draft 14 is voiced, not a callable contract. Stored pre-check mismatches
carry explicit replay state; other early refusals do not. Legacy
model_ops.receipts and its exact draft-11 stamp are deliberately unchanged.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from math import isfinite
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    model_validator,
)

from zeo_core.contracts.connections import NormalizedErrorCode
from zeo_core.integrations.hosted.client import (
    HostedArtifactDescriptor,
    HostedExpectedBinding,
    HostedOperationRequest,
    HostedOperationResponse,
    HostedOperationStatus,
)

from .models import MAX_REQUEST_BYTES, MAX_SAFE_JSON_INTEGER, TextCompletionRequest
from .receipts import (
    AmbiguousModelReceiptError,
    BudgetRemaining,
    Count,
    ExpiredCompletion,
    ModelCost,
    NativeKey,
    ReceiptArtifact,
    UsageComponents,
)

DRAFT_STAMP = (
    "model-ops-draft-14 sha256:"
    "93267ddc27c95747fbb20b98f1ee64e1bc791e53e038d0d95b62faf8043091ab"
)
Reason = Literal[
    "bad_request",
    "model_not_allowed",
    "budget_exhausted",
    "request_in_progress",
    "idempotency_key_reused",
    "connection_revoked",
    "connection_repair_required",
    "connector_revision_unavailable",
    "binding_mismatch",
    "blocked",
    "provider_refused",
    "rate_limited",
    "provider_unavailable",
    "unknown_outcome",
]
Stage = Literal["before_budget", "budget_read", "reserved"]
FenceOutcome = Literal[
    "not_requested",
    "not_compared",
    "matched",
    "precheck_mismatch",
    "dispatcher_mismatch",
    "dispatcher_unavailable",
]
# One table fixes mismatch precedence; the same order is used in tests.
MISMATCH_FIELDS = ("external_identity", "connection_revision")
_EARLY = frozenset(
    {
        "bad_request",
        "model_not_allowed",
        "request_in_progress",
        "idempotency_key_reused",
        "connection_revoked",
        "connection_repair_required",
        "connector_revision_unavailable",
    }
)
_LIFECYCLE = frozenset(
    {
        "connection_revoked",
        "connection_repair_required",
        "connector_revision_unavailable",
    }
)
_UNREAD_BUDGET = frozenset({"caps_missing", "unpriced", "ledger_inconsistent"})
_CODES = {
    "connection_revoked": NormalizedErrorCode.CONNECTION_REVOKED,
    "connection_repair_required": NormalizedErrorCode.CONNECTION_REPAIR_REQUIRED,
    "connector_revision_unavailable": (
        NormalizedErrorCode.CONNECTOR_REVISION_UNAVAILABLE
    ),
    "rate_limited": NormalizedErrorCode.RATE_LIMITED,
    "provider_unavailable": NormalizedErrorCode.PROVIDER_UNAVAILABLE,
    "unknown_outcome": NormalizedErrorCode.RESULT_AMBIGUOUS,
}


class ReceiptBinding(BaseModel):
    """Actual compared values; typing does not authenticate their producer."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", strict=True, hide_input_in_errors=True
    )
    connection_id: str = Field(min_length=1, max_length=200)
    external_identity: str = Field(min_length=1, max_length=500)
    connection_revision: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    connector_revision: str = Field(min_length=1, max_length=200)


class FencedModelReceipt(BaseModel):
    """Own successor projection; common member metadata remains ignored."""

    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)
    draft_schema: str
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
    model_mismatch: bool | None = Field(default=None, strict=True)
    request_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    request_size_bytes: Count | None = Field(default=None, le=MAX_REQUEST_BYTES)
    binding: ReceiptBinding | None = None
    cost: ModelCost | None = None
    budget: BudgetRemaining | None = None
    usage: UsageComponents = Field(default_factory=UsageComponents)
    usage_provider: dict[str, JsonValue] = Field(default_factory=dict, max_length=32)
    usage_provider_omitted: bool | None = Field(default=None, strict=True)
    replayed: bool = Field(default=False, strict=True)
    completion: ReceiptArtifact | None = None
    excluded_blocks: dict[NativeKey, Count] = Field(default_factory=dict, max_length=32)
    provider_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    stop_reason: (
        Literal["end", "max_tokens", "stop_sequence", "content_filter", "other"] | None
    ) = None
    stop_reason_provider: str | None = Field(default=None, min_length=1, max_length=200)
    truncated: bool | None = Field(default=None, strict=True)

    @model_validator(mode="before")
    @classmethod
    def _closed_known_fields(cls, value: object) -> object:
        if isinstance(value, dict):
            if value.get("draft_schema") != DRAFT_STAMP or "request_artifact" in value:
                raise ValueError("unaccepted draft or withdrawn request artifact")
            if any(
                k in cls.model_fields and child is None for k, child in value.items()
            ):
                raise ValueError("known fields are absent or valid, never null")
        return value


@dataclass(frozen=True)
class ParsedFencedModelResponse:
    """Consistent recorded claims only; not proof of a call, release or retry grant."""

    status: HostedOperationStatus
    execution_id: str
    receipt: FencedModelReceipt
    stage: Stage
    fence_outcome: FenceOutcome
    completion: HostedArtifactDescriptor | None
    expired: bool


def _stage(receipt: FencedModelReceipt) -> Stage:
    if receipt.reason in _EARLY or (
        receipt.reason == "budget_exhausted" and receipt.detail in _UNREAD_BUDGET
    ):
        return "before_budget"
    if receipt.reason == "binding_mismatch" and receipt.cost is None:
        return "before_budget"
    return "budget_read" if receipt.reason == "budget_exhausted" else "reserved"


def _native_usage(value: dict[str, JsonValue]) -> None:
    pending: list[tuple[JsonValue, int]] = [(value, 1)]
    keys = 0
    while pending:
        child, depth = pending.pop()
        if isinstance(child, dict):
            keys += len(child)
            if depth > 2 or keys > 32:
                raise ValueError("native usage bounds")
            _native_names(child)
            pending.extend((item, depth + 1) for item in child.values())
        elif isinstance(child, list):
            if depth > 2:
                raise ValueError("native usage bounds")
            pending.extend((item, depth + 1) for item in child)
        else:
            _native_scalar(child)


def _native_names(value: dict[str, JsonValue]) -> None:
    for name in value:
        normalized = re.sub(r"[^a-z0-9]", "", name.lower())
        if normalized == "token" or any(
            part in normalized
            for part in (
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
        ):
            raise ValueError("secret native usage field")


def _native_scalar(child: JsonValue) -> None:
    if isinstance(child, float):
        if not isfinite(child) or (
            child.is_integer() and abs(child) > MAX_SAFE_JSON_INTEGER
        ):
            raise ValueError("native number bounds")
    elif isinstance(child, int) and abs(child) > MAX_SAFE_JSON_INTEGER:
        raise ValueError("native number bounds")


_PROVIDER_FIELDS = frozenset(
    {
        "completion",
        "usage",
        "usage_provider",
        "usage_provider_omitted",
        "provider_request_id",
        "model_reported",
        "model_mismatch",
        "stop_reason",
        "stop_reason_provider",
        "truncated",
        "excluded_blocks",
    }
)


def _accounting(receipt: FencedModelReceipt, stage: Stage, no_call: bool) -> None:
    if stage == "before_budget":
        _before_budget(receipt)
        return
    if receipt.cost is None or receipt.budget is None:
        raise ValueError("missing cost or budget")
    if stage == "budget_read":
        _budget_refusal(receipt, receipt.cost)
        return
    if "replayed" not in receipt.model_fields_set:
        raise ValueError("stored receipt requires replay flag")
    if receipt.usage_provider_omitted is not None and (
        receipt.usage_provider_omitted is not True
        or "usage_provider" in receipt.model_fields_set
    ):
        raise ValueError("invalid native omission")
    if receipt.reason == "unknown_outcome" and not receipt.cost.held:
        raise ValueError("unknown outcome must stay held")
    if no_call:
        _no_call_accounting(receipt, receipt.cost)
    else:
        reported = _has_usage(receipt)
        if (reported and not receipt.cost.incomplete) != (
            receipt.cost.tariff_estimate_micro_usd is not None
        ):
            raise ValueError("usage estimate requiredness")


def _before_budget(receipt: FencedModelReceipt) -> None:
    fields = receipt.model_fields_set
    if ({"cost", "budget"} | _PROVIDER_FIELDS) & fields:
        raise ValueError("pre-budget receipt carries uncomputed fields")
    if receipt.reason == "binding_mismatch":
        if "replayed" not in fields:
            raise ValueError("stored pre-check mismatch requires replay state")
    elif "replayed" in fields:
        raise ValueError("other early refusals have no replay state")


def _budget_refusal(receipt: FencedModelReceipt, cost: ModelCost) -> None:
    if (
        _PROVIDER_FIELDS & receipt.model_fields_set
        or cost.held
        or "replayed" in receipt.model_fields_set
    ):
        raise ValueError("budget refusal carries provider outcome")
    if {
        "incomplete",
        "tariff_estimate_micro_usd",
        "overshoot_micro_usd",
    } & cost.model_fields_set:
        raise ValueError("budget refusal carries estimate")


def _no_call_accounting(receipt: FencedModelReceipt, cost: ModelCost) -> None:
    if _PROVIDER_FIELDS & receipt.model_fields_set or cost.held or cost.incomplete:
        raise ValueError("no-call release contradicts provider or held evidence")
    if (
        cost.tariff_estimate_micro_usd is not None
        or cost.overshoot_micro_usd is not None
    ):
        raise ValueError("no-call release cannot invent an estimate")


def _has_usage(receipt: FencedModelReceipt) -> bool:
    return bool(receipt.usage.other) or any(
        v is not None for v in receipt.usage.model_dump(exclude={"other"}).values()
    )


def _mismatch(
    message: str, actual: ReceiptBinding, expected: HostedExpectedBinding, stage: Stage
) -> FenceOutcome:
    changed = next(
        (
            field
            for field in MISMATCH_FIELDS
            if getattr(actual, field) != getattr(expected, field)
        ),
        None,
    )
    if changed is None or message != "binding_mismatch:" + changed:
        raise ValueError("mismatch precedence or message invalid")
    return "precheck_mismatch" if stage == "before_budget" else "dispatcher_mismatch"


def _fence(
    response: HostedOperationResponse,
    receipt: FencedModelReceipt,
    request: HostedOperationRequest,
    stage: Stage,
) -> FenceOutcome:
    error = response.normalized_error
    unavailable = (
        receipt.reason == "provider_unavailable"
        and error is not None
        and error.message == "binding_unavailable"
    )
    if request.expect is None:
        if (
            receipt.binding is not None
            or receipt.reason == "binding_mismatch"
            or unavailable
        ):
            raise ValueError("unfenced request cannot claim a comparison")
        return "not_requested"
    if unavailable:
        if stage != "reserved" or receipt.binding is not None:
            raise ValueError("unreadable comparison cannot invent binding")
        return "dispatcher_unavailable"
    required = (
        receipt.reason in {"binding_mismatch", "budget_exhausted"}
        or stage != "before_budget"
    )
    if not required:
        if receipt.binding is not None:
            raise ValueError("early refusal cannot invent binding")
        return "not_compared"
    actual = receipt.binding
    if actual is None or actual.connection_id != request.connection_id:
        raise ValueError("missing or wrong connection binding")
    if receipt.reason == "binding_mismatch":
        return _mismatch(
            error.message if error is not None else "", actual, request.expect, stage
        )
    changed = any(
        getattr(actual, field) != getattr(request.expect, field)
        for field in MISMATCH_FIELDS
    )
    if changed or (
        request.connector_revision is not None
        and actual.connector_revision != request.connector_revision
    ):
        raise ValueError("reported binding does not match original request")
    return "matched"


def _completion(response: HostedOperationResponse, receipt: FencedModelReceipt) -> bool:
    if response.status != HostedOperationStatus.CONFIRMED:
        if {
            "completion",
            "stop_reason",
            "stop_reason_provider",
            "truncated",
        } & receipt.model_fields_set:
            raise ValueError("refusal carries completion")
        return False
    if receipt.reason is not None or receipt.model_reported is None:
        raise ValueError("confirmed receipt lacks reported identity")
    if (
        receipt.stop_reason is None
        or receipt.stop_reason_provider is None
        or receipt.truncated is None
    ):
        raise ValueError("confirmed receipt lacks outcome")
    if (
        receipt.stop_reason in {"max_tokens", "content_filter"}
        and not receipt.truncated
    ):
        raise ValueError("truncation inconsistent")
    item = receipt.completion
    if (
        item is None
        or item.filename != "completion.txt"
        or item.media_type != "text/plain"
    ):
        raise ValueError("completion metadata invalid")
    if response.artifact is not None:
        if (
            item.state != "available"
            or item.model_dump(exclude={"state"}) != response.artifact.model_dump()
        ):
            raise ValueError("artifact differs from receipt")
        return False
    marker = ExpiredCompletion.model_validate(response.result)
    if (
        not receipt.replayed
        or item.state != "expired"
        or marker.content_sha256 != item.content_sha256
        or marker.size_bytes != item.size_bytes
    ):
        raise ValueError("expired replay metadata invalid")
    return True


def _request_evidence(
    receipt: FencedModelReceipt,
    arguments: TextCompletionRequest,
    catalogue_revision: str,
) -> None:
    if receipt.reason == "bad_request":
        if {"request_sha256", "request_size_bytes"} & receipt.model_fields_set:
            raise ValueError("bad request has no validated fingerprint")
    elif (
        receipt.reason not in _LIFECYCLE
        or receipt.request_sha256 is not None
        or receipt.request_size_bytes is not None
    ):
        canonical = arguments.canonical_bytes()
        if receipt.request_sha256 != "sha256:" + hashlib.sha256(
            canonical
        ).hexdigest() or receipt.request_size_bytes != len(canonical):
            raise ValueError("request fingerprint differs")
    if (
        receipt.reason not in _LIFECYCLE | {"bad_request"}
        and receipt.model_requested is None
    ):
        raise ValueError("validated model missing")
    if (
        receipt.model_requested is not None
        and receipt.model_requested != arguments.model
    ):
        raise ValueError("requested model differs")
    if (
        receipt.cost is not None
        and receipt.cost.rates_source.revision != catalogue_revision
    ):
        raise ValueError("rate revision differs")
    if (receipt.reason == "budget_exhausted") != (receipt.detail is not None):
        raise ValueError("budget detail requiredness")


def _response_status(
    response: HostedOperationResponse, receipt: FencedModelReceipt
) -> None:
    if response.status == HostedOperationStatus.CONFIRMED:
        if receipt.reason is not None:
            raise ValueError("confirmed refusal")
    else:
        error = response.normalized_error
        if error is None or receipt.reason is None:
            raise ValueError("missing normalized refusal")
        expected_status = (
            HostedOperationStatus.AMBIGUOUS
            if receipt.reason == "unknown_outcome"
            else HostedOperationStatus.REFUSED
            if receipt.reason in _EARLY or receipt.reason == "budget_exhausted"
            else HostedOperationStatus.FAILED_SAFE
        )
        if response.status != expected_status or error.code != _CODES.get(
            receipt.reason, NormalizedErrorCode.REQUEST_REFUSED
        ):
            raise ValueError("reason status or code invalid")
        if (
            error.message == "binding_unavailable"
            and receipt.reason != "provider_unavailable"
        ):
            raise ValueError("unavailable message attached to another reason")


def _reported_model(
    receipt: FencedModelReceipt, catalogued_reported_models: tuple[str, ...]
) -> None:
    if receipt.model_reported is not None:
        mismatch = receipt.model_reported != receipt.model_requested
        if receipt.model_mismatch != mismatch:
            raise ValueError("reported model flag inconsistent")
        uncatalogued = (
            mismatch and receipt.model_reported not in catalogued_reported_models
        )
        if (
            receipt.cost is not None
            and ("model" in receipt.cost.incomplete) != uncatalogued
        ):
            raise ValueError("catalogue model incompleteness invalid")
    elif receipt.model_mismatch is not None or (
        receipt.cost is not None and "model" in receipt.cost.incomplete
    ):
        raise ValueError("invented reported model mismatch")


def _confirmed_usage(
    response: HostedOperationResponse, receipt: FencedModelReceipt
) -> None:
    if response.status == HostedOperationStatus.CONFIRMED:
        reported = bool(receipt.usage.other) or any(
            v is not None for v in receipt.usage.model_dump(exclude={"other"}).values()
        )
        if not reported and (
            receipt.cost is None
            or not receipt.cost.held
            or receipt.cost.tariff_estimate_micro_usd is not None
        ):
            raise ValueError("confirmed missing usage must remain held")


def parse_fenced_model_response(
    response: HostedOperationResponse,
    *,
    original_request: HostedOperationRequest,
    catalogue_revision: str,
    catalogued_reported_models: tuple[str, ...],
) -> ParsedFencedModelResponse:
    """Validate this explicitly pinned successor, without weakening draft 11.

    A response does not echo its idempotency key: the authenticated transport
    and retained full request bind that exchange. This parser cannot prove
    ledger transitions or independently authenticate a producer.
    """
    try:
        request = HostedOperationRequest.model_validate(
            original_request.model_dump(exclude_none=True)
        )
        arguments = TextCompletionRequest.model_validate(request.arguments)
        response = HostedOperationResponse.model_validate(response.model_dump())
        receipt = FencedModelReceipt.model_validate(response.receipt)
        stage = _stage(receipt)
        _request_evidence(receipt, arguments, catalogue_revision)
        _response_status(response, receipt)
        outcome = _fence(response, receipt, request, stage)
        no_call = outcome in {"dispatcher_mismatch", "dispatcher_unavailable"}
        _accounting(receipt, stage, no_call)
        _reported_model(receipt, catalogued_reported_models)
        _native_usage(receipt.usage_provider)
        expired = _completion(response, receipt)
        _confirmed_usage(response, receipt)
        return ParsedFencedModelResponse(
            response.status,
            response.execution_id,
            receipt,
            stage,
            outcome,
            response.artifact,
            expired,
        )
    except (
        ValidationError,
        ValueError,
        TypeError,
        AttributeError,
        RecursionError,
        OverflowError,
    ):
        raise AmbiguousModelReceiptError(
            "fenced model evidence is invalid or incomplete; retain the original "
            "request and reservation; reconcile the same key under valid authority; "
            "never retry automatically or replace the key"
        ) from None
