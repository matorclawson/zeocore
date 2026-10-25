"""Proofs that LLM calls expose one attempt to the outer runner."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from unittest.mock import MagicMock, patch

from zeo_core.core.errors import ZeoApiError
from zeo_core.execution import (
    ExecutionOutcome,
    ExecutionPolicy,
    FailureKind,
    ResilientExecutionResult,
    run_sync,
)
from zeo_core.integrations.core.results import IntegrationResult
from zeo_core.integrations.llms.clients.base import LLMClient
from zeo_core.integrations.llms.execution import llm_chat_target
from zeo_core.integrations.llms.models import ChatMessage, LLMOptions
from zeo_core.integrations.llms.protocols import (
    LLMProviderProtocol,
    OneAttemptLLMProviderProtocol,
)


class ScriptedClient(LLMClient):
    def __init__(self, script: list[object], *, retry_count: int = 3) -> None:
        super().__init__(model="test-model", retry_count=retry_count)
        self.script = script
        self.calls = 0

    def _chat_with_provider(
        self,
        messages: list[ChatMessage],
        options: LLMOptions,
        callback: Callable[[str], None] | None = None,
    ) -> IntegrationResult[str]:
        del messages, options, callback
        item = self.script[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, IntegrationResult)
        return item

    def _count_tokens_with_provider(
        self, messages: list[ChatMessage]
    ) -> IntegrationResult[int]:
        return IntegrationResult.success_result(len(messages))


def policy(*targets: str) -> ExecutionPolicy:
    return ExecutionPolicy(
        total_timeout_seconds=2,
        attempt_timeout_seconds=1,
        attempt_targets=targets,
    )


def messages() -> Sequence[dict]:
    return [{"role": "user", "content": "answer concisely"}]


def test_one_attempt_protocol_is_additive_to_legacy_runtime_contract() -> None:
    class LegacyClient:
        @property
        def model(self) -> str:
            return "legacy"

        def chat(
            self,
            messages: Sequence[ChatMessage] | Sequence[dict],
            options: LLMOptions | None = None,
            callback: Callable[[str], None] | None = None,
        ) -> IntegrationResult[str]:
            return IntegrationResult.success_result("legacy")

        def count_tokens(
            self, messages: Sequence[ChatMessage] | Sequence[dict]
        ) -> IntegrationResult[int]:
            return IntegrationResult.success_result(len(messages))

    legacy = LegacyClient()

    assert isinstance(legacy, LLMProviderProtocol)
    assert not isinstance(legacy, OneAttemptLLMProviderProtocol)


def test_chat_once_never_uses_clients_configured_retry_loop() -> None:
    client = ScriptedClient(
        [
            ZeoApiError("first failed", status_code=503),
            IntegrationResult.success_result("second"),
        ],
        retry_count=3,
    )

    try:
        client.chat_once(messages())
    except ZeoApiError:
        pass

    assert client.calls == 1


def test_outer_policy_owns_retry_and_records_provider_attempts() -> None:
    client = ScriptedClient(
        [
            ZeoApiError("provider canary", status_code=503),
            IntegrationResult.success_result("recovered"),
        ]
    )
    target = llm_chat_target("api", client, messages())

    result = run_sync(policy("api", "api"), {"api": target})

    assert result.outcome is ExecutionOutcome.SUCCEEDED
    assert result.value == "recovered"
    assert client.calls == 2
    assert [item.failure_kind for item in result.attempts] == [
        FailureKind.TRANSIENT,
        None,
    ]


def test_authentication_failure_never_retries_or_retains_provider_text() -> None:
    canary = "LLM-AUTH-CANARY-e439"
    client = ScriptedClient(
        [
            ZeoApiError(canary, status_code=401),
            IntegrationResult.success_result("must-not-run"),
        ]
    )
    target = llm_chat_target("api", client, messages())

    result = run_sync(policy("api", "api"), {"api": target})

    assert result.outcome is ExecutionOutcome.FAILED_SAFE
    assert result.failure_kind is FailureKind.AUTHENTICATION
    assert client.calls == 1
    assert canary not in result.model_dump_json()


def test_unknown_api_error_is_not_guessed_retryable_from_its_text() -> None:
    client = ScriptedClient(
        [
            ZeoApiError("rate limit words without structured status"),
            IntegrationResult.success_result("must-not-run"),
        ]
    )
    target = llm_chat_target("api", client, messages())

    result = run_sync(policy("api", "api"), {"api": target})

    assert result.failure_kind is FailureKind.PERMANENT
    assert client.calls == 1


def test_unsuccessful_integration_result_is_malformed_without_error_leak() -> None:
    canary = "LLM-RESULT-CANARY-13d8"
    client = ScriptedClient([IntegrationResult.error_result(canary)])
    target = llm_chat_target("api", client, messages())

    result = run_sync(policy("api"), {"api": target})

    assert result.failure_kind is FailureKind.MALFORMED_RESPONSE
    assert canary not in result.model_dump_json()


# --- a Moonshot reply that was sent and paid for but is unusable (zeocore-SOW-44) ---


def _moonshot_attempt(
    reply_text: str, finish_reason: str | None
) -> tuple[ResilientExecutionResult, MagicMock]:
    """One run through the outer policy with a Moonshot client whose SDK is mocked."""
    from types import SimpleNamespace

    from zeo_core.integrations.llms.clients.moonshot import MoonshotClient

    sdk = MagicMock()
    message = SimpleNamespace(content=reply_text)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    sdk.chat.completions.create.return_value = SimpleNamespace(choices=[choice])
    client = MoonshotClient(api_key="k", retry_count=3, initial_retry_delay=0.0)
    target = llm_chat_target("api", client, messages())
    with patch.object(client, "_get_client", return_value=sdk):
        result = run_sync(policy("api"), {"api": target})
    return result, sdk


def test_a_cut_off_moonshot_reply_counts_as_a_started_attempt() -> None:
    result, sdk = _moonshot_attempt("cut", "length")

    assert result.failure_kind is FailureKind.MALFORMED_RESPONSE
    assert [item.dispatch_started for item in result.attempts] == [True]
    assert sdk.chat.completions.create.call_count == 1


def test_an_empty_moonshot_reply_counts_as_a_started_attempt() -> None:
    result, _sdk = _moonshot_attempt("", "stop")

    assert result.failure_kind is FailureKind.MALFORMED_RESPONSE
    assert [item.dispatch_started for item in result.attempts] == [True]


def test_a_refused_moonshot_request_is_still_not_a_started_attempt() -> None:
    from zeo_core.integrations.llms.clients.moonshot import MoonshotClient
    from zeo_core.integrations.llms.models import FunctionDefinition, ToolDefinition

    client = MoonshotClient(api_key="k")
    options = LLMOptions(tools=[ToolDefinition(function=FunctionDefinition(name="f"))])
    target = llm_chat_target("api", client, messages(), options)
    sdk = MagicMock()
    with patch.object(client, "_get_client", return_value=sdk):
        result = run_sync(policy("api"), {"api": target})

    assert result.failure_kind is FailureKind.PERMANENT
    assert [item.dispatch_started for item in result.attempts] == [False]
    sdk.chat.completions.create.assert_not_called()
