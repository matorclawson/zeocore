"""The byte-level entry for ``zeo.ui-result/v1`` (zeocore-SOW-42).

``validate_bytes`` applies every limit that is a property of the JSON text as
received, before any value is trusted, and then the closed model of
:mod:`zeo_core.contracts.ui.card`. It performs no I/O and has no effect: it returns the
typed result or raises :class:`UiRefused` carrying a stable ``code`` and a ``loc`` made
of declared field names and list indexes only, never a value from the input.

Limits (SOW-38 rev 8, section 5): a response of at most 262,144 bytes and a component
of at most 65,536 bytes of JSON text as received, nesting depth 9 (every object and
array counts one level; the envelope is level 1). Refused before any positive output:
invalid UTF-8, a byte-order mark, a lone surrogate escape, a duplicate key at any level,
NaN, Infinity, an integer beyond 2^53 - 1 where a number is allowed, text after the
value, and a top level that is not an object.
"""

from __future__ import annotations

import json
from functools import cache
from typing import Any, Final, Literal

from pydantic import ValidationError

from zeo_core.contracts.ui.card import UiCardResult, UiFreshCardResult

Profile = Literal["default", "fresh"]
MAX_RESPONSE_BYTES: Final = 262_144
MAX_COMPONENT_BYTES: Final = 65_536
MAX_DEPTH: Final = 9
_BOM: Final = b"\xef\xbb\xbf"
#: Where a component object sits: envelope 1, components array 2, component 3.
_COMPONENT_DEPTH: Final = 3


class UiRefused(ValueError):  # noqa: N818 -- a refusal, not an error condition
    """A refused document. ``code`` is stable; ``loc`` names declared fields only."""

    def __init__(self, code: str, loc: tuple[str | int, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.loc = loc


def _scan(raw: bytes) -> None:  # noqa: C901 -- one byte-wise state machine
    """Depth and component size from the bytes, before the JSON is parsed."""
    depth = 0
    in_string = False
    escaped = False
    starts: list[int] = []
    for index, byte in enumerate(raw):
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):  # { [
            depth += 1
            if depth > MAX_DEPTH:
                raise UiRefused("too_deep")
            starts.append(index)
        elif byte in (0x7D, 0x5D):  # } ]
            if depth == 0:
                raise UiRefused("invalid_json")
            if (
                depth == _COMPONENT_DEPTH
                and index + 1 - starts[-1] > MAX_COMPONENT_BYTES
            ):
                raise UiRefused("component_too_large")
            starts.pop()
            depth -= 1


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise UiRefused("duplicate_key")
    return dict(pairs)


def _refuse_constant(_name: str) -> Any:  # noqa: ANN401
    raise UiRefused("not_a_finite_number")


def _int(token: str) -> int:
    """An integer token; beyond 16 digits it is over 2^53 - 1 and is refused unread."""
    if len(token.lstrip("-")) > 16:
        raise UiRefused("integer_too_large")
    return int(token)


def _float(token: str) -> float:
    value = float(token)
    if value != value or abs(value) == float("inf"):  # noqa: PLR0124
        raise UiRefused("not_a_finite_number")
    return value


def _lone_surrogate(value: object) -> bool:
    if isinstance(value, str):
        return any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    if isinstance(value, dict):
        return any(_lone_surrogate(k) or _lone_surrogate(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_lone_surrogate(item) for item in value)
    return False


def parse(raw: bytes) -> dict[str, Any]:
    """The JSON object of ``raw``, or :class:`UiRefused`. Nothing is modelled yet."""
    if len(raw) > MAX_RESPONSE_BYTES:
        raise UiRefused("response_too_large")
    if raw.startswith(_BOM):
        raise UiRefused("byte_order_mark")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise UiRefused("invalid_utf8") from None
    _scan(raw)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_no_duplicates,
            parse_constant=_refuse_constant,
            parse_float=_float,
            parse_int=_int,
        )
    except UiRefused:
        raise
    except (ValueError, RecursionError):
        raise UiRefused("invalid_json") from None
    if not isinstance(value, dict):
        raise UiRefused("not_an_object")
    if _lone_surrogate(value):
        raise UiRefused("lone_surrogate")
    return value


@cache
def _declared() -> frozenset[str]:
    """Every property name the model declares, at any depth."""
    names: set[str] = set()
    stack: list[Any] = [UiCardResult.model_json_schema(by_alias=True)]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                names.update(properties)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return frozenset(names)


def _loc(parts: tuple[str | int, ...]) -> tuple[str | int, ...]:
    """A key the contract does not declare is the caller's data: it shows as ``*``."""
    return tuple(
        part if isinstance(part, int) or part in _declared() else "*" for part in parts
    )


def _model(profile: Profile) -> type[UiCardResult]:
    """The model of ``profile``; ``fresh`` requires the snapshot (zeocore-SOW-46).

    An unknown profile raises: a mistyped name must never fall back to the looser model.
    """
    if profile == "fresh":
        return UiFreshCardResult
    if profile == "default":
        return UiCardResult
    raise ValueError(f"unknown profile: {profile!r}")


def validate_value(value: dict[str, Any], profile: Profile = "default") -> UiCardResult:
    """The closed model of an already parsed object; :class:`UiRefused` on any error."""
    try:
        return _model(profile).model_validate(value)
    except ValidationError as error:
        first = error.errors(include_url=False, include_input=False)[0]
        raise UiRefused(str(first["type"]), _loc(tuple(first["loc"]))) from None


def validate_bytes(raw: bytes, profile: Profile = "default") -> UiCardResult:
    """Validate the JSON text ``raw`` as a ``zeo.ui-result/v1`` card result."""
    return validate_value(parse(raw), profile)


def errors_of(raw: bytes, profile: Profile = "default") -> list[dict[str, Any]]:
    """Every refusal of ``raw`` as ``{"loc", "type"}``, with no input value in it."""
    try:
        value = parse(raw)
    except UiRefused as refusal:
        return [{"loc": list(refusal.loc), "type": refusal.code}]
    try:
        _model(profile).model_validate(value)
    except ValidationError as error:
        return [
            {"loc": list(_loc(tuple(item["loc"]))), "type": item["type"]}
            for item in error.errors(include_url=False, include_input=False)
        ]
    return []


__all__ = [
    "MAX_COMPONENT_BYTES",
    "Profile",
    "MAX_DEPTH",
    "MAX_RESPONSE_BYTES",
    "UiRefused",
    "errors_of",
    "parse",
    "validate_bytes",
    "validate_value",
]
