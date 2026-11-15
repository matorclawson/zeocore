"""Fixed-origin Telegram Bot API boundary.

The Bot API puts the bot token in the URL path, so this module never logs,
stores or raises a URL, and silences the standard HTTP loggers for its own
requests. A caller may inject an HTTP transport, never an origin.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

import httpx
from pydantic import SecretStr

_ORIGIN = "https://api.telegram.org"
_private_request: ContextVar[bool] = ContextVar(
    "telegram_private_request", default=False
)


class _PrivateHTTPLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _private_request.get()


_private_filter = _PrivateHTTPLogs()


def _protect_http_logs() -> None:
    for name in (
        "httpx",
        "httpcore.connection",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
        "httpcore.socks",
    ):
        logging.getLogger(name).addFilter(_private_filter)


class TelegramAPIError(RuntimeError):
    """Safe diagnostic: never carries the URL, the token or the response body."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class TelegramTransport:
    def __init__(
        self,
        token: SecretStr,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not token.get_secret_value().strip():
            raise ValueError("Telegram bot token is required")
        if not 0 < timeout <= 120:
            raise ValueError("Telegram timeout must be between zero and 120 seconds")
        _protect_http_logs()
        self._token = token
        self._timeout = timeout
        self._http = httpx.Client(
            transport=transport,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    def call(
        self,
        method: str,
        body: dict[str, Any],
        *,
        extra_timeout: float = 0.0,
    ) -> Any:  # noqa: ANN401 -- the Bot API result type depends on the method
        """POST one Bot API method and return its `result`; never retries."""
        if not method.isalpha():
            raise ValueError("Telegram method name must be letters only")
        url = f"{_ORIGIN}/bot{self._token.get_secret_value()}/{method}"
        marker = _private_request.set(True)
        try:
            response = self._http.post(
                url, json=body, timeout=self._timeout + extra_timeout
            )
        except httpx.TimeoutException:
            raise TelegramAPIError("timeout", "Telegram request timed out") from None
        except httpx.HTTPError:
            raise TelegramAPIError("network", "Telegram request failed") from None
        finally:
            _private_request.reset(marker)

        try:
            payload = response.json()
        except ValueError:
            payload = None
        if (
            response.status_code == 200
            and isinstance(payload, dict)
            and payload.get("ok") is True
        ):
            return payload.get("result")

        retry_after: int | None = None
        if isinstance(payload, dict):
            params = payload.get("parameters")
            if isinstance(params, dict) and isinstance(params.get("retry_after"), int):
                retry_after = params["retry_after"]
        code = {
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            409: "conflict",
            429: "rate_limited",
        }.get(response.status_code, "api_error")
        raise TelegramAPIError(
            code,
            f"Telegram {method} failed with HTTP {response.status_code}",
            status_code=response.status_code,
            retry_after_seconds=retry_after,
        )
