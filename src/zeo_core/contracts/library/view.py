"""Closed citation metadata, never source content or current-read authority.

sow: zeocore-SOW-39 amendment G9-v2-citation-metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from zeo_core.contracts.proof.report import canonical_bytes
from zeo_core.contracts.ui._unicode16 import (
    is_control,
    is_default_ignorable,
    is_surrogate,
    is_unassigned,
)

SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]
ItemRef = Annotated[
    str, Field(pattern=r"^ir_[0-9a-f]{64}$", min_length=67, max_length=67)
]
CitationId = Annotated[
    str, Field(pattern=r"^cit_[0-9a-f]{64}$", min_length=68, max_length=68)
]
RightsClass = Literal["citation_link_only", "operator_owned"]
MAX_VIEW_BYTES = 4096


def metadata(value: str) -> str:
    """Refuse non-scalar/non-NFC metadata; never rewrite evidence."""
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("metadata_not_nfc")
    if any(
        is_surrogate(ord(c))
        or is_control(ord(c))
        or is_unassigned(ord(c))
        or is_default_ignorable(ord(c))
        or unicodedata.category(c) == "Cn"
        for c in value
    ):
        raise ValueError("metadata_codepoint")
    if len(value.encode("utf-8")) > 256:
        raise ValueError("metadata_bytes")
    return value


Metadata = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(metadata)]


def citation_url(value: str) -> str:
    """A.2 public URL data, narrowed by G.11: no port or trailing dot."""
    if not value.isascii() or any(not 33 <= ord(c) <= 126 for c in value):
        raise ValueError("citation_url")
    if any(c in value for c in ("@", "%", "?", "#", "\\")):
        raise ValueError("citation_url")
    host = value[8:].split("/", 1)[0]
    if (
        not value.startswith("https://")
        or not host
        or len(host) > 253
        or host.endswith(".")
        or re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", host) is None
        or any(
            not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
            for label in host.split(".")
        )
    ):
        raise ValueError("citation_url")
    return value


CitationUrl = Annotated[
    str, Field(min_length=9, max_length=512), AfterValidator(citation_url)
]


class Closed(BaseModel):
    """Strict, immutable and closed at every nested boundary."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Revision(Closed):
    markdown_sha256: SHA256


class Rights(Closed):
    class_: RightsClass = Field(alias="class")


class Citation(Closed):
    url: CitationUrl
    id: CitationId


class Attribution(Closed):
    publisher: Metadata
    publisher_map_sha256: SHA256
    citation: Citation


class LibraryView(Closed):
    """A valid shape does not establish rights, attribution or currentness."""

    schema_tag: Literal["zeo.library-view/v2"] = Field(alias="schema")
    producer: Literal["dt-library"]
    item_ref: ItemRef
    revision: Revision
    rights: Rights
    attribution: Attribution


def item_ref(item_id: str) -> str:
    """One-way stable item reference, domain separated from every other id."""
    if re.fullmatch(r"li_[0-9a-f]{64}", item_id) is None:
        raise ValueError("item_id")
    preimage = {"domain": "zeo.library-view/item_ref/v2", "item_id": item_id}
    return "ir_" + hashlib.sha256(canonical_bytes(preimage)).hexdigest()


class ViewRefused(ValueError):  # noqa: N818 -- stable refusal, no input echoed
    """A fixed code only; no values, unknown keys or parser diagnostics."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ViewRefused("duplicate_key")
        result[key] = value
    return result


def _constant(_value: str) -> Any:  # noqa: ANN401 -- json parse hook
    raise ViewRefused("nonfinite")


def validate_bytes(raw: bytes) -> LibraryView:
    """Validate exact transport bytes without I/O or an eligibility assertion."""
    if len(raw) > MAX_VIEW_BYTES:
        raise ViewRefused("view_bytes")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
        return LibraryView.model_validate(value)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ViewRefused("invalid_view") from error


def errors_of(raw: bytes) -> list[dict[str, Any]]:
    """Match the existing CLI's redacted error interface."""
    try:
        validate_bytes(raw)
    except ViewRefused:
        return [{"loc": [], "type": "library_view_refused"}]
    return []


@dataclass(frozen=True)
class CitationBinding:
    """Explicit inert contract inputs, NOT authenticated current state."""

    stored_url: str
    record_url: str
    record_id: str
    map_sha256: str
    publisher: str
    web_host: str
    host_kind: str
    recorded_by: str


def validate_binding(view: LibraryView, binding: CitationBinding) -> None:
    """C4/G.11 exact comparisons; callers must separately authenticate inputs."""
    citation = view.attribution.citation
    if (
        citation.url != binding.stored_url
        or citation.url != binding.record_url
        or citation.id != binding.record_id
        or view.attribution.publisher_map_sha256 != binding.map_sha256
        or view.attribution.publisher != binding.publisher
        or citation.url[8:].split("/", 1)[0] != binding.web_host
        or binding.host_kind != "web"
        or not binding.recorded_by.strip()
    ):
        raise ViewRefused("citation_mismatch")
