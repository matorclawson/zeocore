"""
Moonshot (Kimi) LLM client.

Moonshot's API is OpenAI-compatible, so this is the OpenAI client pointed at
Moonshot's base URL, with the three things a compatible provider must not
inherit from it: its key never goes into ``OPENAI_API_KEY``, the SDK is told
not to read OpenAI's organisation and project from the environment, and the
sampling parameters Moonshot fixes for K3 are left out of the request.

The base URL, model id and request rules are from Moonshot's own documentation
(https://platform.kimi.ai/docs/guide/kimi-k3-1-pro-quickstart). This client
returns the reply text only: it does not carry ``reasoning_content`` or a
tool-call round trip, so it supports plain and streaming chat, not tool use.
"""

from __future__ import annotations

from typing import Any, ClassVar

from zeo_core.integrations.llms.clients.openai import OpenAIClient

MOONSHOT_API_BASE = "https://api.moonshot.ai/v1"
MOONSHOT_DEFAULT_MODEL = "kimi-k3.1-pro"

#: Parameters Moonshot fixes for the K3 series; its documentation says to omit them.
_FIXED_SAMPLING = ("temperature", "top_p", "n", "presence_penalty", "frequency_penalty")


class MoonshotClient(OpenAIClient):
    """Kimi through Moonshot's OpenAI-compatible API."""

    API_KEY_ENV = "MOONSHOT_API_KEY"  # noqa: S105 -- the variable's NAME, not a credential
    EXPOSE_KEY_IN_ENV = False
    # Empty, not absent: absent makes the SDK fall back to OPENAI_ORG_ID and
    # OPENAI_PROJECT_ID and send them to Moonshot.
    SDK_KWARGS: ClassVar[dict[str, Any]] = {"organization": "", "project": ""}

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        **kwargs: Any,  # noqa: ANN401 -- forwarded to OpenAIClient, as the registry does for every client
    ) -> None:
        kwargs.pop("organization", None)
        super().__init__(
            model=model,
            api_key=api_key,
            api_base=api_base or MOONSHOT_API_BASE,
            **kwargs,
        )

    @property
    def model(self) -> str:
        if not self._model:
            self._model = MOONSHOT_DEFAULT_MODEL
        return self._model

    def _provider_params(self, params: dict[str, Any], model: str) -> dict[str, Any]:
        if not model.lower().startswith("kimi-k3"):
            return params
        shaped = {k: v for k, v in params.items() if k not in _FIXED_SAMPLING}
        if "max_tokens" in shaped:
            shaped["max_completion_tokens"] = shaped.pop("max_tokens")
        return shaped
