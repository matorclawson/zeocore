"""Typed Telegram Bot API shapes for the three calls zeocore makes.

Responses ignore fields zeocore does not read, so new Bot API fields never
break a caller. Requests forbid unknown fields, so a typo fails here instead
of being sent.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TelegramUser(_Response):
    id: int
    is_bot: bool = False
    first_name: str = ""
    username: str | None = None


class TelegramChat(_Response):
    id: int
    type: str = ""
    title: str | None = None
    username: str | None = None


class TelegramMessage(_Response):
    message_id: int
    date: int = 0
    chat: TelegramChat
    from_user: TelegramUser | None = Field(default=None, alias="from")
    text: str | None = None


class TelegramCallbackQuery(_Response):
    id: str
    from_user: TelegramUser = Field(alias="from")
    message: TelegramMessage | None = None
    data: str | None = None


class TelegramUpdate(_Response):
    update_id: int
    message: TelegramMessage | None = None
    callback_query: TelegramCallbackQuery | None = None


class InlineKeyboardButton(_Request):
    text: str = Field(min_length=1)
    #: Telegram accepts 1 to 64 bytes of callback data.
    callback_data: str | None = Field(default=None, min_length=1, max_length=64)
    url: str | None = None

    @model_validator(mode="after")
    def _exactly_one_action(self) -> InlineKeyboardButton:
        if (self.callback_data is None) == (self.url is None):
            raise ValueError("Set exactly one of callback_data or url")
        if self.callback_data is not None and len(self.callback_data.encode()) > 64:
            raise ValueError("callback_data must be at most 64 bytes")
        if self.url is not None and not self.url.startswith("https://"):
            raise ValueError("Button url must start with https://")
        return self


class InlineKeyboardMarkup(_Request):
    inline_keyboard: list[list[InlineKeyboardButton]] = Field(min_length=1)


class GetUpdatesRequest(_Request):
    offset: int | None = None
    limit: int = Field(default=100, ge=1, le=100)
    #: Long-poll seconds; 0 returns at once.
    timeout: int = Field(default=0, ge=0, le=50)
    allowed_updates: list[Literal["message", "callback_query"]] | None = None


class SendMessageRequest(_Request):
    chat_id: int | str
    text: str = Field(min_length=1, max_length=4096)
    parse_mode: Literal["HTML", "MarkdownV2"] | None = None
    reply_markup: InlineKeyboardMarkup | None = None
    reply_to_message_id: int | None = None
    disable_notification: bool | None = None


class AnswerCallbackQueryRequest(_Request):
    callback_query_id: str = Field(min_length=1)
    text: str | None = Field(default=None, max_length=200)
    show_alert: bool | None = None
