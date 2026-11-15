"""Telegram client tests: fake transport only, no network, no real token."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from zeo_core.integrations.telegram import (
    AnswerCallbackQueryRequest,
    GetUpdatesRequest,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    SendMessageRequest,
    TelegramAPIError,
    TelegramClient,
    TelegramIntegration,
)

FAKE_TOKEN = "123456:fake-test-token-not-real"


def _client(
    handler: httpx.MockTransport | None = None, seen: list[httpx.Request] | None = None
):
    def respond(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": []})

    return TelegramClient(
        SecretStr(FAKE_TOKEN), transport=handler or httpx.MockTransport(respond)
    )


def test_get_updates_parses_message_and_callback() -> None:
    seen: list[httpx.Request] = []
    body = {
        "ok": True,
        "result": [
            {
                "update_id": 10,
                "unknown_new_field": 1,
                "message": {
                    "message_id": 1,
                    "date": 5,
                    "chat": {"id": 42, "type": "private"},
                    "from": {"id": 7, "is_bot": False, "first_name": "Ana"},
                    "text": "hi",
                },
            },
            {
                "update_id": 11,
                "callback_query": {
                    "id": "cb1",
                    "from": {"id": 7, "first_name": "Ana"},
                    "data": "approve:9",
                },
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    client = _client(httpx.MockTransport(handler))
    updates = client.get_updates(
        GetUpdatesRequest(
            offset=10, timeout=20, allowed_updates=["message", "callback_query"]
        )
    )
    assert [u.update_id for u in updates] == [10, 11]
    assert updates[0].message is not None and updates[0].message.text == "hi"
    assert (
        updates[0].message.from_user is not None
        and updates[0].message.from_user.id == 7
    )
    assert (
        updates[1].callback_query is not None
        and updates[1].callback_query.data == "approve:9"
    )
    request = seen[0]
    assert request.url.path == f"/bot{FAKE_TOKEN}/getUpdates"
    assert json.loads(request.content) == {
        "offset": 10,
        "limit": 100,
        "timeout": 20,
        "allowed_updates": ["message", "callback_query"],
    }


def test_send_message_with_inline_keyboard() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": 3,
                    "date": 1,
                    "chat": {"id": 42},
                    "text": "ok?",
                },
            },
        )

    client = _client(httpx.MockTransport(handler))
    sent = client.send_message(
        SendMessageRequest(
            chat_id=42,
            text="ok?",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Yes", callback_data="yes")]
                ]
            ),
        )
    )
    assert sent.message_id == 3
    assert seen[0].url.path.endswith("/sendMessage")
    assert json.loads(seen[0].content)["reply_markup"] == {
        "inline_keyboard": [[{"text": "Yes", "callback_data": "yes"}]]
    }


def test_answer_callback_query() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})

    client = _client(httpx.MockTransport(handler))
    assert client.answer_callback_query(
        AnswerCallbackQueryRequest(callback_query_id="cb1", text="Done")
    )
    assert json.loads(seen[0].content) == {"callback_query_id": "cb1", "text": "Done"}


def test_error_is_safe_and_carries_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "ok": False,
                "description": f"secret {FAKE_TOKEN}",
                "parameters": {"retry_after": 7},
            },
        )

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(TelegramAPIError) as info:
        client.send_message(SendMessageRequest(chat_id=1, text="x"))
    assert info.value.code == "rate_limited"
    assert info.value.retry_after_seconds == 7
    assert FAKE_TOKEN not in str(info.value)
    assert FAKE_TOKEN not in repr(info.value.__dict__)


def test_network_failure_is_safe_and_not_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError(f"boom {request.url}")

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(TelegramAPIError) as info:
        client.send_message(SendMessageRequest(chat_id=1, text="x"))
    assert info.value.code == "network"
    assert FAKE_TOKEN not in str(info.value)
    assert info.value.__cause__ is None
    assert len(calls) == 1


def test_token_never_appears_in_http_logs(caplog: pytest.LogCaptureFixture) -> None:
    client = _client()
    with caplog.at_level(logging.DEBUG):
        client.get_updates()
    assert FAKE_TOKEN not in caplog.text


def test_requests_reject_bad_input() -> None:
    with pytest.raises(ValidationError):
        SendMessageRequest(chat_id=1, text="")
    with pytest.raises(ValidationError):
        SendMessageRequest(chat_id=1, text="x", unknown="y")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        InlineKeyboardButton(text="a")
    with pytest.raises(ValidationError):
        InlineKeyboardButton(text="a", callback_data="x", url="https://e.com")
    with pytest.raises(ValidationError):
        InlineKeyboardButton(text="a", callback_data="é" * 33)
    with pytest.raises(ValidationError):
        InlineKeyboardButton(text="a", url="http://e.com")
    with pytest.raises(ValidationError):
        GetUpdatesRequest(timeout=51)


def test_empty_token_refused() -> None:
    with pytest.raises(ValueError):
        TelegramClient(SecretStr("  "))


def test_integration_reads_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    integration = TelegramIntegration()
    assert not integration.initialize().success
    assert not integration.is_available()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    assert integration.initialize().success
    assert integration.is_available()
    integration.close()
    assert not integration.is_available()
