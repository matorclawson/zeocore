"""
Tests for the Moonshot (Kimi) client. No test calls the real API or needs a key:
the SDK constructor or the SDK client is replaced, or built without a request.
"""

import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from zeo_core.core.errors import ZeoIntegrationError
from zeo_core.integrations.llms.clients.moonshot import (
    MOONSHOT_API_BASE,
    MOONSHOT_DEFAULT_MODEL,
    MoonshotClient,
)
from zeo_core.integrations.llms.clients.openai import OpenAIClient
from zeo_core.integrations.llms.models import ChatMessage, LLMOptions, RoleType
from zeo_core.integrations.llms.registry import get_llm_client

KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_ORGANIZATION",
    "MOONSHOT_API_KEY",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
)


def _env(**values: str) -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in KEYS} | values


def _reply(text: str = "hi") -> SimpleNamespace:
    message = SimpleNamespace(content=text)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _chat(client: MoonshotClient, options: LLMOptions) -> dict[str, Any]:
    """The keyword arguments the client sent to chat.completions.create."""
    sdk = MagicMock()
    sdk.chat.completions.create.return_value = _reply()
    with patch.object(client, "_get_client", return_value=sdk):
        client._chat_with_provider(
            [ChatMessage(role=RoleType.USER, content="x")], options
        )
    sent: dict[str, Any] = sdk.chat.completions.create.call_args.kwargs
    return sent


def test_the_sdk_is_pointed_at_moonshot_with_the_given_key() -> None:
    with patch.dict(os.environ, _env(), clear=True), patch("openai.OpenAI") as sdk:
        MoonshotClient(api_key="moon-key")._get_client()
    sdk.assert_called_once_with(
        api_key="moon-key",
        timeout=60,
        organization="",
        project="",
        base_url=MOONSHOT_API_BASE,
    )


def test_the_default_model_is_the_documented_id() -> None:
    assert MOONSHOT_DEFAULT_MODEL == "kimi-k3.1-pro"
    assert MoonshotClient().model == "kimi-k3.1-pro"
    assert MoonshotClient(model="kimi-k3.1-pro-highspeed").model == (
        "kimi-k3.1-pro-highspeed"
    )


def test_the_key_never_reaches_the_openai_variable() -> None:
    with patch.dict(os.environ, _env(), clear=True), patch("openai.OpenAI"):
        client = MoonshotClient(api_key="moon-key")
        client._get_client()
        assert "OPENAI_API_KEY" not in os.environ
        assert "MOONSHOT_API_KEY" not in os.environ


def test_the_key_comes_from_moonshot_api_key_only() -> None:
    with patch.dict(os.environ, _env(MOONSHOT_API_KEY="from-env"), clear=True):
        with patch("openai.OpenAI") as sdk:
            MoonshotClient()._get_client()
        assert sdk.call_args.kwargs["api_key"] == "from-env"
    # An OpenAI key in the environment is never borrowed for Moonshot.
    with patch.dict(os.environ, _env(OPENAI_API_KEY="openai-key"), clear=True):
        with pytest.raises(ZeoIntegrationError, match="MOONSHOT_API_KEY"):
            MoonshotClient()._get_client()


def test_the_real_sdk_does_not_send_openai_identifiers_to_moonshot() -> None:
    environment = _env(OPENAI_ORG_ID="org-x", OPENAI_PROJECT_ID="proj-x")
    with patch.dict(os.environ, environment, clear=True):
        sdk = MoonshotClient(api_key="moon-key")._get_client()
    assert str(sdk.base_url).rstrip("/") == MOONSHOT_API_BASE
    assert sdk.default_headers["OpenAI-Organization"] == ""
    assert sdk.default_headers["OpenAI-Project"] == ""


def test_the_fixed_sampling_parameters_are_left_out() -> None:
    options = LLMOptions(temperature=0.2, top_p=0.5, max_tokens=100, seed=3)
    sent = _chat(MoonshotClient(api_key="k"), options)
    assert sent["model"] == "kimi-k3.1-pro"
    for name in ("temperature", "top_p", "n", "presence_penalty", "frequency_penalty"):
        assert name not in sent
    assert sent["max_completion_tokens"] == 100
    assert "max_tokens" not in sent
    assert sent["seed"] == 3


def test_another_model_keeps_the_usual_parameters() -> None:
    options = LLMOptions(temperature=0.2, max_tokens=100)
    sent = _chat(MoonshotClient(model="moonshot-v1-8k", api_key="k"), options)
    assert sent["temperature"] == 0.2
    assert sent["max_tokens"] == 100
    assert "max_completion_tokens" not in sent


def test_the_openai_client_is_unchanged() -> None:
    with patch.dict(os.environ, _env(), clear=True):
        OpenAIClient(api_key="openai-key")
        assert os.environ["OPENAI_API_KEY"] == "openai-key"
    with patch.dict(os.environ, _env(), clear=True), patch("openai.OpenAI") as sdk:
        OpenAIClient(api_key="openai-key")._get_client()
    sdk.assert_called_once_with(api_key="openai-key", timeout=60)


def test_the_registry_resolves_moonshot() -> None:
    client = get_llm_client("moonshot", api_key="k")
    assert isinstance(client, MoonshotClient)
    assert client.model == "kimi-k3.1-pro"


def test_an_openai_organization_argument_is_not_sent_to_moonshot() -> None:
    with patch.dict(os.environ, _env(), clear=True), patch("openai.OpenAI") as sdk:
        MoonshotClient(api_key="k", organization="org-123")._get_client()
        assert "OPENAI_ORGANIZATION" not in os.environ
    assert sdk.call_args.kwargs["organization"] == ""
