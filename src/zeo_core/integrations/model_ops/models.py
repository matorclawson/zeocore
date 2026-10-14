"""Draft keyless model-operation inputs; not an admitted Broker wire contract."""

from __future__ import annotations

import hashlib
from typing import Literal

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, model_validator

Provider = Literal["anthropic", "openai", "google"]
MAX_SAFE_JSON_INTEGER = (1 << 53) - 1
MAX_REQUEST_BYTES = 256 * 1024


class ModelMessage(BaseModel):
    """Text-only slice: no tool call, URL transport, file or credential field."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=MAX_REQUEST_BYTES, repr=False)

    @model_validator(mode="after")
    def _nonempty_text(self) -> ModelMessage:
        if not self.text.strip():
            raise ValueError("message must contain text")
        return self


class TextCompletionRequest(BaseModel):
    """Bounded intent, separate from coding-agent tool-loop qualification."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    messages: tuple[ModelMessage, ...] = Field(min_length=1, max_length=64, repr=False)
    max_output_tokens: int = Field(strict=True, gt=0, le=MAX_SAFE_JSON_INTEGER)
    system: str | None = Field(default=None, max_length=MAX_REQUEST_BYTES, repr=False)

    temperature: float | None = Field(
        default=None, strict=True, ge=0, le=1, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def _bounded_utf8(self) -> TextCompletionRequest:
        try:
            encoded = self.canonical_bytes()
        except rfc8785.CanonicalizationError, UnicodeEncodeError, ValueError:
            raise ValueError("request must encode as finite UTF-8 JSON") from None
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError("serialized model request exceeds candidate byte limit")
        return self

    def canonical_bytes(self) -> bytes:
        """Caller-owned request bytes under RFC 8785; never stored or dispatched."""
        return rfc8785.dumps(self.model_dump(exclude_none=True))

    def request_sha256(self) -> str:
        """Digest of caller-retained canonical bytes, not proof of dispatch."""
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()


class CataloguedModel(BaseModel):
    """One owner-selected model and its required output-token ceiling."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    max_output_tokens: int = Field(strict=True, gt=0, le=MAX_SAFE_JSON_INTEGER)

    @model_validator(mode="after")
    def _known_aliases_refused(self) -> CataloguedModel:
        if self.model in {"latest", "default"} or self.model.endswith("-latest"):
            raise ValueError("model aliases are not exact catalogue IDs")
        return self


class ModelCatalogue(BaseModel):
    """Trusted exact models and ceilings; not a spending or invocation grant."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    provider: Provider
    models: tuple[CataloguedModel, ...] = Field(min_length=1, max_length=100)
    connector_revision: str = Field(min_length=1, max_length=200)
    # Owner-selected draft provenance only, never frozen-contract admission.
    proposed_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def model_ids(self) -> tuple[str, ...]:
        return tuple(item.model for item in self.models)

    @model_validator(mode="after")
    def _distinct_exact_ids(self) -> ModelCatalogue:
        if len(self.model_ids) != len(set(self.model_ids)):
            raise ValueError("model catalogue contains duplicate IDs")
        return self
