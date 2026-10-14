"""Strict raw request input; no dispatch and no echo of rejected text."""

from __future__ import annotations

import json
import re
from typing import NoReturn

from pydantic import JsonValue

from zeo_core.integrations.hosted.client import HostedClientError

from .models import MAX_REQUEST_BYTES, TextCompletionRequest


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON key")
        result[name] = value
    return result


def _lexical_number(value: str) -> float:
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)\.[0-9]{1,2}", value) is None:
        raise ValueError("number must use at most two decimal places and no exponent")
    return float(value)


def _nonfinite_number(_: str) -> NoReturn:
    raise ValueError("nonfinite JSON number")


def parse_model_request_json(raw: bytes | str) -> TextCompletionRequest:
    """Preserve lexical validation before constructing the typed request.

    Use this for untrusted raw JSON. Pydantic's generic JSON loader may lose
    duplicate-key or number-spelling evidence before model validation.
    """
    try:
        if isinstance(raw, str) and len(raw) > 2 * MAX_REQUEST_BYTES:
            raise ValueError("raw request exceeds HTTP body bound")
        encoded = raw.encode() if isinstance(raw, str) else raw
        if len(encoded) > 2 * MAX_REQUEST_BYTES:
            raise ValueError("raw request exceeds HTTP body bound")
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_float=_lexical_number,
            parse_constant=_nonfinite_number,
        )
        return TextCompletionRequest.model_validate(value)
    except ValueError, UnicodeError, RecursionError:
        raise HostedClientError(
            "model request JSON is invalid; use a bounded UTF-8 request with "
            "unique keys and temperature written without an exponent or excess decimals"
        ) from None
