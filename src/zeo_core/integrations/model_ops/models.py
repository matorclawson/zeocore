"""Draft keyless model-operation inputs; not an admitted Broker wire contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Provider = Literal["anthropic", "openai", "google"]


class ModelMessage(BaseModel):
    """Text-only slice: no tool call, URL transport, file or credential field."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=32_768, repr=False)

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
    max_output_tokens: int = Field(strict=True, gt=0, le=32_768)
    system: str | None = Field(default=None, max_length=32_768, repr=False)

    @model_validator(mode="after")
    def _bounded_utf8(self) -> TextCompletionRequest:
        try:
            count = sum(len(item.text.encode("utf-8")) for item in self.messages)
            count += len((self.system or "").encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("text must encode as UTF-8") from None
        if count > 65_536:
            raise ValueError("aggregate model input exceeds candidate byte limit")
        return self


class ModelCatalogue(BaseModel):
    """Trusted explicit model IDs; this inventory is not a spending grant."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    provider: Provider
    model_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    connector_revision: str = Field(min_length=1, max_length=200)
    proposed_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _distinct_exact_ids(self) -> ModelCatalogue:
        if len(self.model_ids) != len(set(self.model_ids)):
            raise ValueError("model catalogue contains duplicate IDs")
        for model in self.model_ids:
            if model in {"latest", "default"} or model.endswith("-latest"):
                raise ValueError("model aliases are not exact catalogue IDs")
            TextCompletionRequest(
                model=model,
                messages=(ModelMessage(role="user", text="catalogue check"),),
                max_output_tokens=1,
            )
        return self
