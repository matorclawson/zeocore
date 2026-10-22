"""The ``zeo.ui-result/v1`` envelope, restricted to ONE read-only card (zeocore-SOW-42).

This is the wire of zeocore-SOW-38 rev 8 (design) for the smallest subset a first
read-only turn needs: an envelope with exactly one component, of kind ``card``, whose
``action_refs`` is empty. Every other kind (table, chart, form, action_button) and any
second component is refused as unsupported in this subset; nothing is coerced to a
card. Every rule of rev 8 that applies to the subset is enforced: closed keys at every
level, the three identifier grammars, the text classes S, D and L with their UTF-8 byte
bounds and the pinned Unicode 16.0.0 tables, the timestamp and number profiles.

Decoding has no side effects and grants nothing. The byte-level limits (response and
component size, depth, duplicate keys, byte-order mark, lone surrogates, trailing data)
are enforced by :mod:`zeo_core.contracts.ui.wire`, which is the entry to use on bytes.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from zeo_core.contracts.ui._unicode16 import (
    is_control,
    is_default_ignorable,
    is_surrogate,
    is_unassigned,
)

SCHEMA_TAG: Final = "zeo.ui-result/v1"

#: Largest integer a JSON number may carry (2^53 - 1).
MAX_SAFE: Final = 2**53 - 1

#: Byte bounds of the three text classes (UTF-8 bytes of the decoded value).
CLASS_S: Final = 256
CLASS_D: Final = 1024
CLASS_L: Final = 4096

_COMPONENT_ID: Final = r"^[a-z][a-z0-9_.-]{0,127}$"
_OPERATION: Final = r"^[a-z][a-z0-9_.-]{0,127}$"
_OWNER_REF: Final = r"^[A-Za-z0-9._:/-]{1,128}$"
_TIMESTAMP: Final = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z"
)
_DECIMAL_SCHEMA_PATTERN: Final = r"^-?(0|[1-9][0-9]{0,30})(\.[0-9]{1,18})?$"
_TIMESTAMP_SCHEMA_PATTERN: Final = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
_URL_SCHEMA_PATTERN: Final = r"^https://[A-Za-z0-9]"
_DECIMAL: Final = re.compile(r"-?(0|[1-9][0-9]{0,30})(\.[0-9]{1,18})?")
_CURRENCY: Final = re.compile(r"[A-Z]{3}")
_URL: Final = re.compile(
    r"https://(?P<host>[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)"
    r"(?::(?P<port>[0-9]{1,5}))?(?P<rest>(?:[/?][\x21-\x7E]*)?)"
)


def _bounded(value: str, limit: int, *, lf: bool, ignorable: bool) -> str:
    if len(value.encode("utf-8", errors="surrogatepass")) > limit:
        raise PydanticCustomError("ui_text_bytes", f"text over {limit} bytes")
    for char in value:
        cp = ord(char)
        if is_surrogate(cp) or is_unassigned(cp):
            raise PydanticCustomError(
                "ui_text_code_point", "text holds a refused code point"
            )
        if is_control(cp) and not (lf and cp == 0x0A):
            raise PydanticCustomError(
                "ui_text_control", "text holds a control character"
            )
        if not ignorable and is_default_ignorable(cp):
            raise PydanticCustomError(
                "ui_text_ignorable", "text holds a default-ignorable code point"
            )
    return value


def _class_s(value: str) -> str:
    return _bounded(value, CLASS_S, lf=False, ignorable=False)


def _class_d(value: str) -> str:
    return _bounded(value, CLASS_D, lf=False, ignorable=True)


def _class_l(value: str) -> str:
    return _bounded(value, CLASS_L, lf=True, ignorable=True)


def _timestamp(value: str) -> str:
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        raise PydanticCustomError(
            "ui_timestamp_shape", "not a YYYY-MM-DDTHH:MM:SSZ timestamp"
        )
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    if not 1 <= month <= 12 or not 1 <= day <= days[month - 1]:
        raise PydanticCustomError("ui_timestamp_calendar", "not a real calendar date")
    if hour > 23 or minute > 59 or second > 59:
        raise PydanticCustomError("ui_timestamp_time", "not a real time of day")
    return value


#: What JSON Schema can say about the text classes (the BMP part: C0/C1 controls, LF
#: in S and D, and the BMP Default_Ignorable set in S). Unassigned, surrogate and
#: astral code points are checked by the validator only.
_PATTERN_S: Final = (
    r"^[^\u0000-\u001F\u007F-\u009F"
    r"\u00AD\u034F\u061C\u115F\u1160\u17B4\u17B5\u180B-\u180F\u200B-\u200F"
    r"\u202A-\u202E\u2060-\u206F\u3164\uFE00-\uFE0F\uFEFF\uFFA0\uFFF0-\uFFF8]*$"
)
_PATTERN_D: Final = r"^[^\u0000-\u001F\u007F-\u009F]*$"
_PATTERN_L: Final = r"^[^\u0000-\u0009\u000B-\u001F\u007F-\u009F]*$"

#: Text of class S (short label), D (data text) and L (long text).
ShortText = Annotated[
    str, Field(max_length=CLASS_S, pattern=_PATTERN_S), AfterValidator(_class_s)
]
DataText = Annotated[
    str, Field(max_length=CLASS_D, pattern=_PATTERN_D), AfterValidator(_class_d)
]
LongText = Annotated[
    str, Field(max_length=CLASS_L, pattern=_PATTERN_L), AfterValidator(_class_l)
]
FallbackText = Annotated[
    str,
    Field(min_length=1, max_length=CLASS_L, pattern=_PATTERN_L),
    AfterValidator(_class_l),
]
ComponentId = Annotated[str, Field(pattern=_COMPONENT_ID)]
OperationName = Annotated[str, Field(pattern=_OPERATION)]
OwnerRef = Annotated[str, Field(pattern=_OWNER_REF)]
Timestamp = Annotated[
    str,
    Field(
        min_length=20,
        max_length=20,
        pattern=_TIMESTAMP_SCHEMA_PATTERN,
    ),
    AfterValidator(_timestamp),
]

SourceKind = Literal["operation", "receipt", "document", "source"]
ValueType = Literal[
    "text", "integer", "decimal_string", "boolean", "timestamp", "url", "null"
]
StatusState = Literal["healthy", "degraded", "error", "unknown", "stale", "unavailable"]


def _optional_means_absent(schema: dict[str, Any]) -> None:
    """Export an optional key as absent-or-typed: an explicit null is refused."""
    for prop in schema.get("properties", {}).values():
        options = prop.get("anyOf")
        if options is not None:
            kept = [option for option in options if option != {"type": "null"}]
            if len(kept) == 1:
                del prop["anyOf"]
                prop.update(kept[0])
            else:
                prop["anyOf"] = kept
        prop.pop("default", None)


class _Closed(BaseModel):
    """Closed, strict, immutable: an unknown key is an error, nothing is coerced."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        json_schema_extra=_optional_means_absent,
    )


def _refuse_null(value: object) -> object:
    if value is None:
        raise PydanticCustomError(
            "ui_optional_null", "an optional key is absent, never null"
        )
    return value


class Source(_Closed):
    kind: SourceKind
    ref: OwnerRef


class Snapshot(_Closed):
    source: Source
    observed_at: Timestamp
    expires_at: Timestamp | None = None

    _no_null = field_validator("expires_at", mode="before")(_refuse_null)

    @model_validator(mode="after")
    def _expiry_follows_observation(self) -> Self:
        if self.expires_at is not None and self.expires_at <= self.observed_at:
            raise PydanticCustomError(
                "ui_expiry_order", "expires_at is not later than observed_at"
            )
        return self


class Evidence(_Closed):
    kind: SourceKind
    ref: OwnerRef


class Status(_Closed):
    state: StatusState
    text: ShortText


def _finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return value == value and abs(value) <= MAX_SAFE  # noqa: PLR0124 -- NaN check


class Progress(_Closed):
    current: int | float
    total: int | float
    unit: ShortText

    @field_validator("current", "total", mode="before")
    @classmethod
    def _number(cls, value: object) -> object:
        if not _finite(value):
            raise PydanticCustomError(
                "ui_number_range", "not a finite JSON number within 2^53 - 1"
            )
        return value


def _fact_schema(schema: dict[str, Any]) -> None:
    """Export ``value`` as typed by ``value_type``, as the validator does."""
    _optional_means_absent(schema)
    plain = {"not": {"anyOf": [{"required": ["currency"]}, {"required": ["scale"]}]}}
    typed: dict[str, dict[str, Any]] = {
        "text": {"type": "string", "maxLength": CLASS_D, "pattern": _PATTERN_D},
        "integer": {"type": "integer", "minimum": -MAX_SAFE, "maximum": MAX_SAFE},
        "boolean": {"type": "boolean"},
        "null": {"type": "null"},
        "timestamp": {"type": "string", "pattern": _TIMESTAMP_SCHEMA_PATTERN},
        "url": {"type": "string", "maxLength": CLASS_D, "pattern": _URL_SCHEMA_PATTERN},
    }
    branches: list[dict[str, Any]] = [
        {
            "if": {
                "properties": {"value_type": {"const": kind}},
                "required": ["value_type"],
            },
            "then": {"properties": {"value": shape}, **plain},
        }
        for kind, shape in typed.items()
    ]
    branches.append(
        {
            "if": {
                "properties": {"value_type": {"const": "decimal_string"}},
                "required": ["value_type"],
            },
            "then": {
                "properties": {
                    "value": {"type": "string", "pattern": _DECIMAL_SCHEMA_PATTERN}
                },
                "dependentRequired": {"currency": ["scale"]},
            },
        }
    )
    schema["allOf"] = branches


class Fact(_Closed):
    model_config = ConfigDict(json_schema_extra=_fact_schema)

    label: ShortText
    value_type: ValueType
    value: Any  # noqa: ANN401 -- typed by value_type in the validator below
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    scale: int | None = Field(default=None, ge=0, le=18)

    _no_null = field_validator("currency", "scale", mode="before")(_refuse_null)

    @model_validator(mode="after")
    def _typed_value(self) -> Self:  # noqa: C901 -- one branch per value type
        kind, value = self.value_type, self.value
        if kind != "decimal_string" and (
            self.currency is not None or self.scale is not None
        ):
            raise PydanticCustomError(
                "ui_currency_scale_kind",
                "currency and scale are for decimal_string only",
            )
        if kind == "null":
            if value is not None:
                raise PydanticCustomError("ui_fact_null", "a null fact carries null")
            return self
        if kind == "boolean":
            if not isinstance(value, bool):
                raise PydanticCustomError("ui_fact_boolean", "not a boolean")
            return self
        if kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise PydanticCustomError("ui_fact_integer", "not a JSON integer")
            if abs(value) > MAX_SAFE:
                raise PydanticCustomError(
                    "ui_fact_integer_range", "integer beyond 2^53 - 1"
                )
            return self
        if not isinstance(value, str):
            raise PydanticCustomError(
                "ui_fact_string", f"a {kind} fact carries a string"
            )
        if kind == "text":
            _class_d(value)
        elif kind == "timestamp":
            _timestamp(value)
        elif kind == "url":
            self._check_url(value)
        else:
            self._check_decimal(value)
        return self

    @staticmethod
    def _check_url(value: str) -> None:
        match = _URL.fullmatch(value)
        if match is None or len(value) > CLASS_D:
            raise PydanticCustomError(
                "ui_url_profile", "not an https URL of the profile"
            )
        port = match.group("port")
        if port is not None and not 0 < int(port) <= 65535:
            raise PydanticCustomError("ui_url_port", "port out of range")
        if "#" in value:
            raise PydanticCustomError("ui_url_fragment", "no fragment in v1")

    def _check_decimal(self, value: str) -> None:
        if _DECIMAL.fullmatch(value) is None:
            raise PydanticCustomError("ui_decimal_shape", "not a decimal_string")
        if value.startswith("-") and set(value) <= set("-0."):
            raise PydanticCustomError("ui_decimal_negative_zero", "negative zero")
        if self.currency is not None:
            if _CURRENCY.fullmatch(self.currency) is None:
                raise PydanticCustomError(
                    "ui_currency_shape", "currency is three upper-case ASCII letters"
                )
            if self.scale is None:
                raise PydanticCustomError(
                    "ui_currency_needs_scale", "currency needs scale"
                )
        if self.scale is not None:
            fraction = value.partition(".")[2]
            if len(fraction) != self.scale:
                raise PydanticCustomError(
                    "ui_scale_digits", "fraction digits differ from scale"
                )


class CardProps(_Closed):
    summary: LongText
    facts: Annotated[list[Fact], Field(max_length=12)]
    status: Status | None = None
    progress: Progress | None = None
    evidence: Annotated[list[Evidence], Field(max_length=8)]
    action_refs: Annotated[list[ComponentId], Field(max_length=0)]

    _no_null = field_validator("status", "progress", mode="before")(_refuse_null)


class CardComponent(_Closed):
    id: ComponentId
    kind: Literal["card"]
    title: ShortText
    accessibility_label: ShortText
    fallback_text: FallbackText
    snapshot: Snapshot | None = None
    props: CardProps

    _no_null = field_validator("snapshot", mode="before")(_refuse_null)


class InReplyTo(_Closed):
    operation: OperationName
    invocation: OwnerRef


class UiCardResult(_Closed):
    """
    ``zeo.ui-result/v1`` with one read-only card (the zeocore-SOW-42 subset).

    This JSON Schema is NECESSARY BUT NOT SUFFICIENT: it refuses the structural errors
    and never refuses a document the validator accepts, but UTF-8 byte lengths, the
    Unicode 16.0.0 text classes beyond the BMP, calendar validity, snapshot ordering,
    decimal scale digits, duplicate keys and the byte limits are checked only by
    ``zeocore validate``. Conformance means ``zeocore validate`` plus the shared
    vectors.
    """

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, populate_by_name=False
    )

    schema_: Literal["zeo.ui-result/v1"] = Field(alias="schema")
    result_id: OwnerRef
    in_reply_to: InReplyTo
    produced_at: Timestamp
    complete: StrictBool
    plain_text: LongText
    components: Annotated[list[CardComponent], Field(min_length=1, max_length=1)]

    @model_validator(mode="after")
    def _observation_not_after_production(self) -> Self:
        snapshot = self.components[0].snapshot
        if snapshot is not None and snapshot.observed_at > self.produced_at:
            raise PydanticCustomError(
                "ui_observed_after_produced", "observed_at is later than produced_at"
            )
        return self
