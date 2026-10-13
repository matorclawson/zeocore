"""Prepare fenced named operations; no dispatch before receiving activation."""

from __future__ import annotations

from typing import Final

from pydantic import JsonValue

from zeo_core.integrations.hosted.client import (
    HostedClientError,
    HostedExpectedBinding,
    HostedOperationRequest,
)

from .models import ModelCatalogue, TextCompletionRequest

_PROPOSED_OPERATIONS: Final = {
    "anthropic": "anthropic.messages.create",
    "openai": "openai.responses.create",
    "google": "google.gemini.generate",
}


class DraftModelOperationClient:
    """Inert contribution for contract review; never a native provider client.

    The operation names come from model-ops rev a/b. They remain proposals.
    This class prepares input for fake contract tests, but deliberately exposes
    no invoke method, transport, credential reader or registry registration.
    """

    def __init__(
        self,
        *,
        catalogue: ModelCatalogue,
        connection_id: str,
        expected_binding: HostedExpectedBinding,
    ) -> None:
        self._catalogue = catalogue
        self._connection_id = connection_id
        self._expected_binding = expected_binding

    @property
    def capability(self) -> str:
        return "text_completion"

    def prepare(
        self, request: TextCompletionRequest, *, idempotency_key: str
    ) -> HostedOperationRequest:
        """Validate a selected model and preserve the caller's exact replay key."""
        request = TextCompletionRequest.model_validate(request.model_dump())
        if request.model not in self._catalogue.model_ids:
            raise HostedClientError(
                "model is not allowed by this catalogue; "
                "select an owner-pinned catalogue model"
            )
        limit = next(
            item.max_output_tokens
            for item in self._catalogue.models
            if item.model == request.model
        )
        if request.max_output_tokens > limit:
            raise HostedClientError(
                "output-token limit exceeds the catalogue ceiling; "
                "reduce max_output_tokens"
            )
        arguments: dict[str, JsonValue] = {
            "model": request.model,
            "messages": [
                {"role": message.role, "text": message.text}
                for message in request.messages
            ],
            "max_output_tokens": request.max_output_tokens,
        }
        if request.system is not None:
            arguments["system"] = request.system
        if request.temperature is not None:
            arguments["temperature"] = request.temperature
        return HostedOperationRequest(
            connection_id=self._connection_id,
            connector_revision=self._catalogue.connector_revision,
            operation_id=_PROPOSED_OPERATIONS[self._catalogue.provider],
            arguments=arguments,
            idempotency_key=idempotency_key,
            expect=self._expected_binding,
        )
