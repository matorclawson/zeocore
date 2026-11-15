# Telegram

A minimal, typed Telegram Bot API client: get updates, send a message, answer a
callback query. It is a composition API, not a launcher-selected provider, so
it has no package entry point.

```python
from pydantic import SecretStr
from zeo_core.integrations.telegram import (
    AnswerCallbackQueryRequest, GetUpdatesRequest, InlineKeyboardButton,
    InlineKeyboardMarkup, SendMessageRequest, TelegramClient,
)

client = TelegramClient(SecretStr(token))
client.send_message(SendMessageRequest(
    chat_id=123,
    text="Approve?",
    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Yes", callback_data="approve:9"),
    ]]),
))
offset = None
for update in client.get_updates(GetUpdatesRequest(offset=offset, timeout=25)):
    offset = update.update_id + 1          # confirms this update
    if update.callback_query:
        client.answer_callback_query(
            AnswerCallbackQueryRequest(callback_query_id=update.callback_query.id)
        )
```

## Credentials

Create a bot with BotFather and keep its token in your own environment
(`TELEGRAM_BOT_TOKEN` when you use `TelegramIntegration.initialize()`). The
token is a `SecretStr`. The Bot API puts it in the URL path, so the client never
logs, stores or raises a URL, and mutes the standard HTTP loggers for its own
requests. Errors carry a short code (`unauthorized`, `forbidden`, `rate_limited`,
`conflict`, `timeout`, `network`, `api_error`) and `retry_after_seconds` when
Telegram sends one.

## Behavior to know

- Nothing is retried. A repeated `send_message` would message twice; the caller
  decides.
- `get_updates` long-polls up to 50 seconds; the HTTP timeout grows to match.
  Telegram redelivers any update you do not confirm with `offset`.
- Only the origin `https://api.telegram.org` is used. You may inject an
  `httpx.BaseTransport` for tests, never a different origin.
- Requests reject unknown fields and over-long text (4096) or callback data
  (64 bytes). Responses ignore fields zeocore does not read.

Tests use `httpx.MockTransport`: no network and no token.
