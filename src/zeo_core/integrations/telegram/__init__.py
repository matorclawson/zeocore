"""Minimal Telegram Bot API integration: get updates, send, answer callback.

Quickstart::

    from pydantic import SecretStr
    from zeo_core.integrations.telegram import (
        SendMessageRequest, TelegramClient,
    )

    client = TelegramClient(SecretStr(token))
    client.send_message(SendMessageRequest(chat_id=123, text="hello"))

The token comes from the caller (or `TELEGRAM_BOT_TOKEN` through
`TelegramIntegration.initialize()`); it is never written to logs, errors or
dumps. Tests use `httpx.MockTransport`, so no network and no token are needed.
"""

from __future__ import annotations

import os

from pydantic import SecretStr

from zeo_core.integrations.core import IntegrationResult

from .client import TelegramClient
from .models import (
    AnswerCallbackQueryRequest,
    GetUpdatesRequest,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    SendMessageRequest,
    TelegramCallbackQuery,
    TelegramChat,
    TelegramMessage,
    TelegramUpdate,
    TelegramUser,
)
from .transport import TelegramAPIError


class TelegramIntegration:
    integration_id = "telegram"
    name = "Telegram"
    version = "1.0.0"

    def __init__(self, client: TelegramClient | None = None) -> None:
        self._client = client

    @property
    def client(self) -> TelegramClient:
        if self._client is None:
            raise ValueError("Telegram integration is not initialized")
        return self._client

    def initialize(self) -> IntegrationResult[object]:
        if self._client is None:
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            if not token.strip():
                return IntegrationResult.error_result("Set TELEGRAM_BOT_TOKEN")
            self._client = TelegramClient(SecretStr(token.strip()))
        return IntegrationResult.success_result(
            message="Telegram client configured; token validity unverified"
        )

    def is_available(self) -> bool:
        return self._client is not None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def create_integration() -> TelegramIntegration:
    return TelegramIntegration()


__all__ = [
    "AnswerCallbackQueryRequest",
    "GetUpdatesRequest",
    "InlineKeyboardButton",
    "InlineKeyboardMarkup",
    "SendMessageRequest",
    "TelegramAPIError",
    "TelegramCallbackQuery",
    "TelegramChat",
    "TelegramClient",
    "TelegramIntegration",
    "TelegramMessage",
    "TelegramUpdate",
    "TelegramUser",
    "create_integration",
]
