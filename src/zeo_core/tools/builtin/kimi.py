"""``llm.kimi.ask``: one bounded question to Kimi K3.1 Pro, as an MCP-ready tool.

The tool wraps the existing Moonshot client (``get_llm_client("moonshot")``); it adds no
provider code. K3.1 Pro always thinks, so this is the thinking model at full effort. The
answer is the reply text only: the client does not carry Moonshot's
``reasoning_content`` and the tool does not return it.

Every run is bounded by the tool, not by trust in the caller:

* ``max_calls`` per tool instance (default 5): the call after that is refused before
  anything is sent;
* ``max_tokens`` at most ``MAX_TOKENS_CEILING`` and a prompt of at most
  ``MAX_PROMPT_CHARS``;
* one request, never retried (the client's ``chat_once``), with a ``timeout_seconds``
  limit (default 120).

The key is read by the client from ``MOONSHOT_API_KEY`` in the process environment. The
tool never receives, stores, logs or returns it, and a failure answers with a fixed
message and code, never the provider's text.

Expose it over MCP (stdio) with ``python -m zeo_core.tools.builtin.kimi`` (needs the
``mcp`` extra), or register it yourself with ``zeo_core.adapters.mcp.register_tool``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from zeo_core.contracts import CapabilityResult
from zeo_core.core.errors.base import ZeoError
from zeo_core.tools import BaseZeoTool, ToolContext

MAX_PROMPT_CHARS = 8_000
MAX_TOKENS_CEILING = 8_192
DEFAULT_MAX_TOKENS = 2_048
DEFAULT_MAX_CALLS = 5
DEFAULT_TIMEOUT_SECONDS = 120


class KimiAskRequest(BaseModel):
    """One question for Kimi."""

    prompt: str = Field(
        min_length=1,
        max_length=MAX_PROMPT_CHARS,
        description="The question. It leaves your machine for Moonshot, a third party.",
    )
    max_tokens: int = Field(
        default=DEFAULT_MAX_TOKENS,
        ge=256,
        le=MAX_TOKENS_CEILING,
        description=(
            "Output limit. K3.1 Pro always thinks, so a small limit can leave no reply."
        ),
    )


class KimiAskResponse(BaseModel):
    """The answer. No reasoning text and no usage: the client returns neither."""

    text: str
    model: str
    finish_reason: str = "stop"


def _moonshot_client() -> Any:  # noqa: ANN401 -- the registry's client type
    # Imported here, at the registry level, so the llms package can move without
    # this tool importing a provider module path directly.
    from zeo_core.integrations.llms.registry import get_llm_client

    return get_llm_client("moonshot")


class KimiAskTool(BaseZeoTool):
    """Ask Kimi K3.1 Pro one bounded question and return its answer text."""

    name = "llm.kimi.ask"
    version = "1.0.0"

    def __init__(
        self,
        *,
        max_calls: int = DEFAULT_MAX_CALLS,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        client_factory: Callable[[], Any] = _moonshot_client,
    ) -> None:
        super().__init__()
        if max_calls < 1:
            raise ValueError("max_calls must be at least 1")
        self._max_calls = max_calls
        self._timeout = timeout_seconds
        self._client_factory = client_factory
        self._calls = 0

    def run(
        self, request: KimiAskRequest, ctx: ToolContext
    ) -> CapabilityResult[KimiAskResponse]:
        """Send the prompt once and return the answer; refuse past the call budget."""
        if self._calls >= self._max_calls:
            return CapabilityResult.fail(
                "The call budget of this tool instance is used up; nothing was sent",
                "ZEO_KIMI_BUDGET_EXHAUSTED",
            )
        self._calls += 1  # counted before sending: a failed call still spent the budget
        from zeo_core.integrations.llms.models import (
            ChatMessage,
            LLMOptions,
            RoleType,
        )

        try:
            client = self._client_factory()
            result = client.chat_once(
                [ChatMessage(role=RoleType.USER, content=request.prompt)],
                LLMOptions(max_tokens=request.max_tokens, timeout=self._timeout),
            )
        except ZeoError as error:
            # The class name only: the message can carry provider text.
            return CapabilityResult.fail(
                f"Kimi did not answer ({type(error).__name__})", "ZEO_KIMI_FAILED"
            )
        if not result.success or not result.content:
            return CapabilityResult.fail("Kimi returned no answer", "ZEO_KIMI_EMPTY")
        return CapabilityResult.ok(
            data=KimiAskResponse(text=result.content, model=client.model),
            msg="Kimi answered",
            metadata={"tool": f"{self.name} v{self.version}"},
        )


def main() -> None:
    """Serve ``llm.kimi.ask`` over MCP on stdio (needs the ``mcp`` extra)."""
    from zeo_core.adapters.mcp import create_server, register_tool
    from zeo_core.core.registry import OperationRegistry

    registry = OperationRegistry()
    register_tool(
        KimiAskTool(), registry=registry, description=KimiAskTool.__doc__ or ""
    )
    create_server(registry, name="zeocore-kimi").run()


if __name__ == "__main__":
    main()
