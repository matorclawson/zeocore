"""``llm.kimi.ask``: offline, with a fake client. Nothing here calls Moonshot."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import ValidationError

from zeo_core.adapters.mcp import create_server, register_tool
from zeo_core.core.errors.integration import ZeoApiError
from zeo_core.core.registry import OperationRegistry
from zeo_core.integrations.core.results import IntegrationResult
from zeo_core.tools import ToolContext
from zeo_core.tools.builtin.kimi import (
    MAX_PROMPT_CHARS,
    MAX_TOKENS_CEILING,
    KimiAskRequest,
    KimiAskTool,
)


def _ok(result: Any) -> bool:  # noqa: ANN401
    return bool(result.status.value == "success")


KEY = "sk-test-NOT-A-REAL-KEY-0123456789"


def _ctx() -> ToolContext:
    return ToolContext(
        run_id="test-run",
        tool_name="llm.kimi.ask",
        tool_version="1.0.0",
        logger=logging.getLogger("test"),
        fs=object(),
        work_dir=".",
        output_dir=".",
        services={},
    )


class FakeClient:
    model = "kimi-k3.1-pro"

    def __init__(
        self, reply: str | None = "an answer", error: Exception | None = None
    ) -> None:
        self.reply, self.error, self.sent = reply, error, []

    def chat_once(
        self,
        messages: Any,  # noqa: ANN401
        options: Any,  # noqa: ANN401
    ) -> IntegrationResult[str]:
        self.sent.append((messages, options))
        if self.error:
            raise self.error
        return IntegrationResult.success_result(self.reply)


def _tool(client: FakeClient, **kwargs: Any) -> KimiAskTool:  # noqa: ANN401
    return KimiAskTool(client_factory=lambda: client, **kwargs)


def test_it_returns_the_answer_text_model_and_stop_ending() -> None:
    client = FakeClient("42")
    result = _tool(client).run(
        KimiAskRequest(prompt="meaning?", max_tokens=512), _ctx()
    )
    assert _ok(result)
    assert result.data.model_dump() == {
        "text": "42",
        "model": "kimi-k3.1-pro",
        "finish_reason": "stop",
    }
    ((messages, options),) = client.sent
    assert [m.content for m in messages] == ["meaning?"]
    assert options.max_tokens == 512
    assert options.timeout == 120


@pytest.mark.parametrize(
    "fields",
    [
        {"prompt": ""},
        {"prompt": "x" * (MAX_PROMPT_CHARS + 1)},
        {"prompt": "x", "max_tokens": 255},
        {"prompt": "x", "max_tokens": MAX_TOKENS_CEILING + 1},
    ],
)
def test_the_request_bounds_refuse(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        KimiAskRequest(**fields)


def test_the_call_budget_is_enforced_before_anything_is_sent() -> None:
    client = FakeClient()
    tool = _tool(client, max_calls=2)
    request = KimiAskRequest(prompt="q")
    assert _ok(tool.run(request, _ctx()))
    assert _ok(tool.run(request, _ctx()))
    third = tool.run(request, _ctx())
    assert not _ok(third)
    assert third.machine_message == "ZEO_KIMI_BUDGET_EXHAUSTED"
    assert len(client.sent) == 2


def test_a_failed_call_still_spends_the_budget() -> None:
    client = FakeClient(error=ZeoApiError("boom"))
    tool = _tool(client, max_calls=1)
    assert (
        tool.run(KimiAskRequest(prompt="q"), _ctx()).machine_message
        == "ZEO_KIMI_FAILED"
    )
    assert tool.run(KimiAskRequest(prompt="q"), _ctx()).machine_message == (
        "ZEO_KIMI_BUDGET_EXHAUSTED"
    )
    assert len(client.sent) == 1


def test_a_failure_never_echoes_provider_text_or_the_key() -> None:
    client = FakeClient(error=ZeoApiError(f"401 for key {KEY}: provider says no"))
    result = _tool(client).run(KimiAskRequest(prompt="q"), _ctx())
    assert not _ok(result)
    dumped = result.model_dump_json()
    assert KEY not in dumped
    assert "provider says no" not in dumped


def test_an_empty_reply_is_a_failure_not_an_empty_success() -> None:
    result = _tool(FakeClient(reply="")).run(KimiAskRequest(prompt="q"), _ctx())
    assert not _ok(result)
    assert result.machine_message == "ZEO_KIMI_EMPTY"


def test_max_calls_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_calls"):
        KimiAskTool(max_calls=0)


def test_the_default_factory_builds_the_moonshot_client_without_sending_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOONSHOT_API_KEY", KEY)
    client = KimiAskTool()._client_factory()
    assert type(client).__name__ == "MoonshotClient"
    assert client.model == "kimi-k3.1-pro"


def test_it_is_registered_as_an_mcp_tool_with_the_request_schema() -> None:
    registry = OperationRegistry()
    name = register_tool(KimiAskTool(), registry=registry)
    assert name == "llm.kimi.ask"
    operation = registry.get_or_error(name)
    schema = operation.request_model.model_json_schema()
    assert sorted(schema["properties"]) == ["max_tokens", "prompt"]
    server = create_server(registry, name="zeocore-kimi")
    assert server is not None
