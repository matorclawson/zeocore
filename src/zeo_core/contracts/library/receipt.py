"""Separate acyclic access-receipt shape, without an actual receipt issuer.

sow: zeocore-SOW-39 amendment G9-v2-citation-metadata.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, ConfigDict, Field, field_validator, model_validator

from zeo_core.contracts.library.view import (
    SHA256,
    Closed,
    ItemRef,
    Metadata,
    RightsClass,
)
from zeo_core.contracts.proof.report import canonical_bytes

REFUSAL_STATES = {
    "not_found": "not_found",
    "not_retained": "not_found",
    "tombstoned": "tombstoned",
    "withdrawn": "withdrawn",
    "not_ready": "not_ready",
    "unavailable": "unavailable",
    "digest_mismatch": "inconsistent",
    "kind_refused": "inconsistent",
    "use_mismatch": "inconsistent",
    "receipt_mismatch": "inconsistent",
    "citation_mismatch": "inconsistent",
}


def timestamp(value: str) -> str:
    """A.5 UTC second precision with real calendar validation."""
    if (
        re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value)
        is None
    ):
        raise ValueError("timestamp")
    datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return value


Timestamp = Annotated[
    str,
    Field(
        min_length=20, max_length=20, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
    ),
    AfterValidator(timestamp),
]


class AccessReceipt(Closed):
    """Shape and digest integrity only; never proof a live F.8 read occurred."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"outcome": {"const": "served"}}},
                    "then": {
                        "required": ["view_sha256", "view_length", "rights_class"],
                        "properties": {
                            "state": {"const": "ready"},
                            "view_sha256": {"type": "string"},
                            "view_length": {"type": "integer"},
                            "rights_class": {"type": "string"},
                        },
                    },
                    "else": {
                        "not": {
                            "anyOf": [
                                {"required": ["view_sha256"]},
                                {"required": ["view_length"]},
                                {"required": ["rights_class"]},
                            ]
                        },
                    },
                },
                *[
                    {
                        "if": {"properties": {"outcome": {"const": outcome}}},
                        "then": {"properties": {"state": {"const": state}}},
                    }
                    for outcome, state in REFUSAL_STATES.items()
                ],
            ],
        },
    )

    schema_tag: Literal["zeo.access-receipt/v2"] = Field(alias="schema")
    id: Annotated[
        str, Field(pattern=r"^rcp_[0-9a-f]{64}$", min_length=68, max_length=68)
    ]
    reservation: SHA256
    reader: Metadata
    purpose: Literal["citation_link"]
    item_ref: ItemRef
    markdown_sha256: SHA256
    view_sha256: SHA256 | None = None
    view_length: Annotated[int, Field(ge=1, le=4096)] | None = None
    rights_class: RightsClass | None = None
    state: Literal[
        "ready",
        "withdrawn",
        "tombstoned",
        "unavailable",
        "not_ready",
        "not_found",
        "inconsistent",
    ]
    as_of: Timestamp
    evaluated_at: Timestamp
    outcome: Literal[
        "served",
        "not_found",
        "not_retained",
        "tombstoned",
        "withdrawn",
        "not_ready",
        "unavailable",
        "digest_mismatch",
        "kind_refused",
        "use_mismatch",
        "receipt_mismatch",
        "citation_mismatch",
    ]
    code_points_charged: Literal[0]

    @field_validator("code_points_charged", mode="before")
    @classmethod
    def integer_zero(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("integer_required")
        return value

    @model_validator(mode="after")
    def binding(self) -> Self:
        view_fields = {"view_sha256", "view_length", "rights_class"}
        if self.outcome == "served":
            if self.state != "ready" or any(
                getattr(self, k) is None for k in view_fields
            ):
                raise ValueError("served_binding")
        elif (
            self.state != REFUSAL_STATES[self.outcome]
            or view_fields & self.model_fields_set
        ):
            raise ValueError("refusal_binding")
        if self.id != receipt_id(
            self.model_dump(mode="json", by_alias=True, exclude_none=True)
        ):
            raise ValueError("receipt_digest")
        return self


def receipt_id(fields: dict[str, object]) -> str:
    """Hash every receipt field except its own id, including reservation."""
    preimage = {key: value for key, value in fields.items() if key != "id"}
    return "rcp_" + hashlib.sha256(canonical_bytes(preimage)).hexdigest()
