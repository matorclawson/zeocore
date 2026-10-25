"""Keyless model execution through caller-injected authority and evidence ports.

No transport, admission policy, storage or credential source is constructed here.
The transport remains responsible for real Broker authority and deadline policy.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import rfc8785
from pydantic import ValidationError

from zeo_core.integrations.hosted.client import (
    HostedAuthorizedTransport,
    HostedClientError,
    HostedConnectionClient,
    HostedOperationRequest,
    HostedOperationResponse,
    HostedOperationStatus,
)

from .models import TextCompletionRequest
from .receipts import (
    AmbiguousModelReceiptError,
    ParsedModelResponse,
    parse_model_response,
)
from .service import DraftModelOperationClient

_MAX_REQUEST_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
EvidenceKind = Literal["request", "response_normalized", "completion"]


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class PreparedModelCall:
    """Immutable canonical intent, not an admission or a raw HTTP-wire capture."""

    request_bytes: bytes = field(repr=False)
    argument_bytes: bytes = field(repr=False)
    model: str
    idempotency_key: str

    @property
    def request_sha256(self) -> str:
        """Canonical full-envelope identity used by the admission callback."""
        return _digest(self.request_bytes)

    @property
    def argument_sha256(self) -> str:
        """Draft-11 model-request identity checked in the returned receipt."""
        return _digest(self.argument_bytes)


@dataclass(frozen=True)
class ModelAdmission:
    """Echo of external durable admission; this object grants no authority.

    The caller validates the actual permission and reservation outside this
    library. The Broker independently enforces its own controls and budgets.
    """

    request_sha256: str
    idempotency_key: str
    reference: str


@dataclass(frozen=True)
class ModelExecution:
    """Validated disposition and verified bytes; never proof of an AX build."""

    prepared: PreparedModelCall
    parsed: ParsedModelResponse
    normalized_response: bytes = field(repr=False)
    completion: bytes | None = field(repr=False)


class ModelExecutionAmbiguousError(AmbiguousModelReceiptError):
    """Unsettled post-dispatch outcome with the original replay intent intact."""

    def __init__(self, prepared: PreparedModelCall) -> None:
        super().__init__(
            "model execution may have been dispatched; retain its reservation "
            "and reconcile or replay the same prepared request and idempotency "
            "key under valid authority; never retry with a new key"
        )
        self.prepared = prepared


class ModelOperationExecutor:
    """Compose existing draft request/receipt checks with injected dispatch.

    This is not registration or activation of the design-only model operations.
    ``retain(kind, bytes)`` must persist protected bytes before returning their
    ``sha256:`` digest. ``admit(prepared)`` must durably authorize/reserve the
    exact intent before returning its binding. There are no default ports.
    """

    def __init__(
        self,
        *,
        preparer: DraftModelOperationClient,
        transport: HostedAuthorizedTransport,
        retain: Callable[[EvidenceKind, bytes], str],
        admit: Callable[[PreparedModelCall], ModelAdmission],
        rate_revision: str,
        catalogued_reported_models: tuple[str, ...],
        max_completion_bytes: int,
    ) -> None:
        if (
            not isinstance(preparer, DraftModelOperationClient)
            or not isinstance(transport, HostedAuthorizedTransport)
            or not callable(retain)
            or not callable(admit)
            or not isinstance(rate_revision, str)
            or not 1 <= len(rate_revision) <= 200
            or type(catalogued_reported_models) is not tuple
            or len(catalogued_reported_models) > 100
            or any(
                not isinstance(model, str) or not 1 <= len(model) <= 200
                for model in catalogued_reported_models
            )
            or type(max_completion_bytes) is not int
            or not 1 <= max_completion_bytes <= _MAX_ARTIFACT_BYTES
        ):
            raise HostedClientError(
                "model executor configuration is invalid; supply explicit "
                "authority/evidence ports and bounded catalogue limits"
            )
        self._preparer = preparer
        self._client = HostedConnectionClient(transport=transport)
        self._retain = retain
        self._admit = admit
        self._rate_revision = rate_revision
        self._reported_models = catalogued_reported_models
        self._completion_limit = max_completion_bytes

    def prepare(
        self, request: TextCompletionRequest, *, idempotency_key: str
    ) -> PreparedModelCall:
        """Prepare exact fenced canonical intent without dispatch or retention."""
        try:
            checked = TextCompletionRequest.model_validate(
                request.model_dump(exclude_unset=True)
            )
            hosted = self._preparer.prepare(checked, idempotency_key=idempotency_key)
            envelope = rfc8785.dumps(hosted.model_dump(mode="json", exclude_none=True))
            if len(envelope) > _MAX_REQUEST_BYTES:
                raise ValueError("envelope exceeds member request bound")
            return PreparedModelCall(
                request_bytes=envelope,
                argument_bytes=checked.canonical_bytes(),
                model=checked.model,
                idempotency_key=hosted.idempotency_key,
            )
        except ValueError, TypeError, AttributeError:
            raise HostedClientError(
                "model intent cannot be prepared; select the pinned model and "
                "connection, reduce the request and retain the original replay key"
            ) from None

    def _checked_request(self, prepared: PreparedModelCall) -> HostedOperationRequest:
        try:
            if (
                not isinstance(prepared, PreparedModelCall)
                or type(prepared.request_bytes) is not bytes
                or not 1 <= len(prepared.request_bytes) <= _MAX_REQUEST_BYTES
            ):
                raise ValueError("invalid prepared request")
            request = HostedOperationRequest.model_validate(
                json.loads(prepared.request_bytes)
            )
            regenerated = self.prepare(
                TextCompletionRequest.model_validate(request.arguments),
                idempotency_key=prepared.idempotency_key,
            )
            if regenerated != prepared:
                raise ValueError("prepared binding changed")
            return request
        except (
            HostedClientError,
            ValidationError,
            RecursionError,
            ValueError,
            TypeError,
            AttributeError,
        ):
            raise HostedClientError(
                "prepared model request or fence changed; do not dispatch it; "
                "recover the original retained intent before reconciling"
            ) from None

    def _store(self, kind: EvidenceKind, content: bytes) -> None:
        if self._retain(kind, content) != _digest(content):
            raise ValueError("retained bytes do not match their binding")

    def execute(self, prepared: PreparedModelCall) -> ModelExecution:
        """Dispatch once; never retry, replace a key or release a reservation."""
        request = self._checked_request(prepared)
        try:
            self._store("request", prepared.request_bytes)
            admission = self._admit(prepared)
            if (
                not isinstance(admission, ModelAdmission)
                or admission.request_sha256 != prepared.request_sha256
                or admission.idempotency_key != prepared.idempotency_key
                or not isinstance(admission.reference, str)
                or not 1 <= len(admission.reference) <= 200
            ):
                raise ValueError("admission does not bind the prepared request")
        except Exception:
            raise HostedClientError(
                "model request evidence or admission is unavailable; the executor "
                "has not dispatched; reconcile admission before retrying"
            ) from None

        try:
            response = self._client.invoke(request)
            response = HostedOperationResponse.model_validate(response.model_dump())
            normalized = response.model_dump_json(exclude_none=True).encode("utf-8")
            if len(normalized) > _MAX_RESPONSE_BYTES:
                raise ValueError("response exceeds bound")
            self._store("response_normalized", normalized)
            # Validate exactly the retained snapshot, not a shallowly frozen
            # response whose nested dictionaries a caller could still mutate.
            response = HostedOperationResponse.model_validate_json(normalized)
            parsed = parse_model_response(
                response,
                expected_model=prepared.model,
                catalogue_revision=self._rate_revision,
                expected_request_sha256=prepared.argument_sha256,
                expected_request_size_bytes=len(prepared.argument_bytes),
                catalogued_reported_models=self._reported_models,
            )
            if parsed.status is HostedOperationStatus.AMBIGUOUS:
                raise ModelExecutionAmbiguousError(prepared)
            completion = None
            if parsed.completion is not None:
                if parsed.completion.size_bytes > self._completion_limit:
                    raise ValueError("completion exceeds bound")
                completion = self._client.download_artifact(parsed.completion)
                self._store("completion", completion)
            return ModelExecution(prepared, parsed, normalized, completion)
        except Exception:
            raise ModelExecutionAmbiguousError(prepared) from None
