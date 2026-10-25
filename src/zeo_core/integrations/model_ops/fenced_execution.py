"""Explicit draft-14 execution composition; no activation or refund authority.

The separate name prevents silent selection or downgrade of a receipt schema.
All authority, durable storage, transport and deadlines are supplied by callers.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field

from zeo_core.integrations.hosted.client import (
    HostedAuthorizedTransport,
    HostedClientError,
    HostedConnectionClient,
    HostedOperationRequest,
    HostedOperationResponse,
    HostedOperationStatus,
)

from .execution import (
    EvidenceKind,
    ModelAdmission,
    ModelExecutionAmbiguousError,
    ModelOperationExecutor,
    PreparedModelCall,
)
from .fenced_receipts import ParsedFencedModelResponse, parse_fenced_model_response
from .models import TextCompletionRequest
from .service import DraftModelOperationClient


@dataclass(frozen=True)
class FencedModelExecution:
    """Validated claims and verified completion bytes, never a refund grant."""

    prepared: PreparedModelCall
    parsed: ParsedFencedModelResponse
    normalized_response: bytes = field(repr=False)
    completion: bytes | None = field(repr=False)


class FencedModelOperationExecutor:
    """Prepare, retain, admit and send once using only the draft-14 parser.

    The legacy executor is composed solely for its public pure preparation
    method and configuration validation. Its dispatch method is never used.
    The transport owns authentication and deadlines. A consistent no-call
    receipt does not itself authorise this class to release a reservation.
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
        self._intent = ModelOperationExecutor(
            preparer=preparer,
            transport=transport,
            retain=retain,
            admit=admit,
            rate_revision=rate_revision,
            catalogued_reported_models=catalogued_reported_models,
            max_completion_bytes=max_completion_bytes,
        )
        self._client = HostedConnectionClient(transport=transport)
        self._retain, self._admit = retain, admit
        self._rate_revision = rate_revision
        self._reported_models = catalogued_reported_models
        self._completion_limit = max_completion_bytes

    def prepare(
        self, request: TextCompletionRequest, *, idempotency_key: str
    ) -> PreparedModelCall:
        """Reuse the public pure preparer; never invokes the legacy executor."""
        return self._intent.prepare(request, idempotency_key=idempotency_key)

    def _original(self, prepared: PreparedModelCall) -> HostedOperationRequest:
        try:
            if (
                not isinstance(prepared, PreparedModelCall)
                or type(prepared.request_bytes) is not bytes
                or not 1 <= len(prepared.request_bytes) <= 65536
            ):
                raise ValueError("invalid intent")
            original = HostedOperationRequest.model_validate_json(
                prepared.request_bytes
            )
            regenerated = self.prepare(
                TextCompletionRequest.model_validate(original.arguments),
                idempotency_key=prepared.idempotency_key,
            )
            if original.expect is None or regenerated != prepared:
                raise ValueError("changed intent")
            return original
        except ValueError, TypeError, AttributeError, RecursionError, HostedClientError:
            raise HostedClientError(
                "prepared fenced request changed; do not dispatch; recover the "
                "original retained intent before reconciling"
            ) from None

    def _store(self, kind: EvidenceKind, content: bytes) -> None:
        if (
            self._retain(kind, content)
            != "sha256:" + hashlib.sha256(content).hexdigest()
        ):
            raise ValueError("retained bytes differ")

    def execute(self, prepared: PreparedModelCall) -> FencedModelExecution:
        """One dispatch only; no retry, new key, automatic replay or refund."""
        original = self._original(prepared)
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
                raise ValueError("admission differs")
        except Exception:
            raise HostedClientError(
                "fenced request retention or admission is unavailable; no dispatch "
                "was made here; reconcile admission before retrying"
            ) from None

        try:
            response = self._client.invoke(original)
            response = HostedOperationResponse.model_validate(response.model_dump())
            normalized = response.model_dump_json(exclude_none=True).encode("utf-8")
            if len(normalized) > 1024 * 1024:
                raise ValueError("response exceeds bound")
            self._store("response_normalized", normalized)
            # Parse the retained snapshot against the actual original envelope,
            # not against an expected binding reconstructed from the response.
            snapshot = HostedOperationResponse.model_validate_json(normalized)
            parsed = parse_fenced_model_response(
                snapshot,
                original_request=original,
                catalogue_revision=self._rate_revision,
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
            return FencedModelExecution(prepared, parsed, normalized, completion)
        except Exception:
            raise ModelExecutionAmbiguousError(prepared) from None
