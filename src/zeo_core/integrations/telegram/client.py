"""Typed Telegram client: get updates, send a message, answer a callback."""

from __future__ import annotations

import httpx
from pydantic import SecretStr

from .models import (
    AnswerCallbackQueryRequest,
    GetUpdatesRequest,
    SendMessageRequest,
    TelegramMessage,
    TelegramUpdate,
)
from .transport import TelegramTransport


class TelegramClient:
    def __init__(
        self,
        token: SecretStr,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._transport = TelegramTransport(token, transport=transport, timeout=timeout)

    def close(self) -> None:
        self._transport.close()

    def get_updates(
        self, request: GetUpdatesRequest | None = None
    ) -> list[TelegramUpdate]:
        """Fetch pending updates. Pass `offset = last update_id + 1` to confirm
        earlier ones; Telegram redelivers anything not confirmed."""
        req = request or GetUpdatesRequest()
        result = self._transport.call(
            "getUpdates",
            req.model_dump(exclude_none=True),
            extra_timeout=float(req.timeout),
        )
        if not isinstance(result, list):
            return []
        return [TelegramUpdate.model_validate(item) for item in result]

    def send_message(self, request: SendMessageRequest) -> TelegramMessage:
        """Send one message. Never retried: a repeat would message twice."""
        result = self._transport.call(
            "sendMessage", request.model_dump(exclude_none=True)
        )
        return TelegramMessage.model_validate(result)

    def answer_callback_query(self, request: AnswerCallbackQueryRequest) -> bool:
        result = self._transport.call(
            "answerCallbackQuery", request.model_dump(exclude_none=True)
        )
        return result is True
