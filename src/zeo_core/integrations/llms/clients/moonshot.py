"""
Moonshot (Kimi) LLM client.

Moonshot's API is OpenAI-compatible, so this is the OpenAI client pointed at
Moonshot's base URL, with what a compatible provider must not inherit from it:
its key never goes into ``OPENAI_API_KEY``, the SDK is told not to read
OpenAI's organisation and project from the environment, the sampling parameters
Moonshot fixes for K3 are left out of the request, and the base URL must be
https.

The base URL, model id and request rules are from Moonshot's own documentation
(https://platform.kimi.ai/docs/guide/kimi-k3-1-pro-quickstart). This client
returns the reply text only. It does not carry ``reasoning_content`` or a
tool-call round trip, so it REFUSES tools, functions and tool messages rather
than send them, and it reports a reply that is empty, cut at the token limit or
ended as a tool call as an error instead of as an empty success.

Prompts and replies pass to Moonshot, a third party; its terms apply to them.
"""

from __future__ import annotations

from typing import Any, ClassVar

from zeo_core.core.errors import ZeoApiError, ZeoIntegrationError
from zeo_core.integrations.llms.clients.openai import OpenAIClient
from zeo_core.integrations.llms.models import ChatMessage, LLMOptions, RoleType

MOONSHOT_API_BASE = "https://api.moonshot.ai/v1"
MOONSHOT_DEFAULT_MODEL = "kimi-k3.1-pro"
_SERVICE = "Moonshot"

#: Parameters Moonshot fixes for the K3 series; its documentation says to omit them.
_FIXED_SAMPLING = (
    "temperature",
    "top_p",
    "n",
    "presence_penalty",
    "frequency_penalty",
)


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
        base = api_base or MOONSHOT_API_BASE
        if not base.lower().startswith("https://"):
            raise ZeoIntegrationError(
                "Moonshot's base URL must be https: the key is sent to it"
            )
        super().__init__(model=model, api_key=api_key, api_base=base, **kwargs)

    @property
    def model(self) -> str:
        if not self._model:
            self._model = MOONSHOT_DEFAULT_MODEL
        return self._model

    def _check_request(self, messages: list[ChatMessage], options: LLMOptions) -> None:
        carries_tools = bool(options.tools or options.functions) or any(
            m.tool_calls
            or m.function_call
            or m.role in (RoleType.TOOL, RoleType.FUNCTION)
            for m in messages
        )
        if carries_tools:
            raise ZeoIntegrationError(
                "Moonshot: tool calling is not supported by this client; it returns "
                "reply text only and does not carry reasoning_content or tool-call "
                "round trips"
            )

    def _check_ending(self, text: str, finish_reason: str | None) -> None:
        if finish_reason not in (None, "stop"):
            raise ZeoApiError(
                f"Moonshot ended the reply with finish_reason={finish_reason!r}"
                + (
                    "; K3.1 always thinks, so a small token limit can leave no reply:"
                    " raise max_tokens"
                    if finish_reason == "length"
                    else ""
                ),
                service=_SERVICE,
                api_method="chat.completions.create",
            )
        if not text.strip():
            raise ZeoApiError(
                "Moonshot returned an empty reply",
                service=_SERVICE,
                api_method="chat.completions.create",
            )

    def _convert_error(self, error: Exception) -> ZeoApiError:
        if isinstance(error, ZeoApiError):
            return error
        converted = super()._convert_error(error)
        return ZeoApiError(
            str(converted).replace("OpenAI", _SERVICE),
            service=_SERVICE,
            api_method="chat.completions.create",
            original_error=error,
        )

    def _provider_params(self, params: dict[str, Any], model: str) -> dict[str, Any]:
        if not model.lower().startswith("kimi-k3"):
            return params
        shaped = {k: v for k, v in params.items() if k not in _FIXED_SAMPLING}
        if "max_tokens" in shaped:
            shaped["max_completion_tokens"] = shaped.pop("max_tokens")
        return shaped
