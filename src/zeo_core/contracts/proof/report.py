"""Internal immutable ``zeo.proof-report/v1`` (sow: zeocore-SOW-45).

Normative: SOW-39 E/A.5/F.3/G.6. This is NOT the library-owned outward
proof-view. Structural validity is never verified supporting evidence or live
clearance. References are format-checked only, with no foreign-contract imports.

``proof_id`` is prf_ + SHA256(RFC8785(all report fields except proof_id)). The
F.3 transport digest instead hashes exact returned bytes INCLUDING proof_id.
``draft_issue_digest`` hashes RFC8785({from_name, subject, plain_text, html}),
without Unicode normalization. Draft artifact hashes name exact representation
bytes. ``markdown_sha256``/``expected_digest`` name the cited LF UTF-8 Markdown
revision; ``retained_sha256`` names exact retained RAW bytes. rr_/li_/sel_ ids
are opaque identifiers; their syntax cannot establish referent existence.
Locations name exact LF UTF-8 view hashes, one-based codepoint lines/columns,
end exclusive. Excerpt and claim hashes name exact unnormalized UTF-8 text.

No I/O, resolver, provider, scorer or current lifecycle is implemented here.
An unresolved reference is not_checked/not_checkable, never supported because
its format passed. Runtime F.3/F.7 must refuse unavailable/stale/non-valid reads;
current freshness thresholds are NOT immutable report fields. A stopped or
unperformed check remains not_checked. A deleted formerly
passing report remains structurally valid while its current use is refused.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from zeo_core.contracts.ui._unicode16 import (
    is_control,
    is_default_ignorable,
    is_surrogate,
    is_unassigned,
)


def sha256(data: bytes) -> str:
    """Lowercase SHA256 of exact bytes; never a source-verification statement."""
    return hashlib.sha256(data).hexdigest()


def _canonical_value(value: object) -> object:
    """The report's JCS subset: scalar strings, safe integers, bool/null, arrays.

    Object keys sort by UTF-16 code units, NOT Python Unicode code points.
    Floats and non-JSON objects are refused, never rounded or stringified.
    """
    if value is None or type(value) is bool:
        return value
    if isinstance(value, int) and type(value) is int:
        if not -(2**53 - 1) <= value <= 2**53 - 1:
            raise ValueError("canonical integer outside exact JSON range")
        return value
    if isinstance(value, str) and type(value) is str:
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeError as error:
            raise ValueError("canonical string is not Unicode scalar text") from error
        return value
    if isinstance(value, (list, tuple)) and type(value) in (list, tuple):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict) and type(value) is dict:
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or type(key) is not str:
                raise ValueError("canonical object keys must be strings")
            _canonical_value(key)
            result[key] = _canonical_value(item)
        return dict(
            sorted(result.items(), key=lambda pair: pair[0].encode("utf-16-be"))
        )
    raise ValueError("unsupported canonical JSON value; floats are forbidden")


def canonical_bytes(value: object) -> bytes:
    """RFC8785 bytes for the contract's restricted, float-free JSON domain."""
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_digest(value: object) -> str:
    """RFC8785 identity; distinct from the transport's exact-byte digest."""
    return sha256(canonical_bytes(value))


def draft_issue_digest(
    *, from_name: str, subject: str, plain_text: str, html: str
) -> str:
    """Hash the exact four strings; do not normalize draft/evidence text."""
    values = {
        "from_name": from_name,
        "subject": subject,
        "plain_text": plain_text,
        "html": html,
    }
    if any(type(value) is not str for value in values.values()):
        raise ValueError("draft fields must be strings")
    return canonical_digest(values)


def _array(value: object) -> tuple[object, ...]:
    if not isinstance(value, (tuple, list)) or type(value) not in (tuple, list):
        raise ValueError("expected an array")
    return tuple(value)


def _metadata(value: str, bound: int) -> str:
    if not value or len(value.encode("utf-8", errors="surrogatepass")) > bound:
        raise ValueError("metadata UTF-8 byte bound")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("metadata must be NFC")
    for char in value:
        cp = ord(char)
        if (
            is_surrogate(cp)
            or is_unassigned(cp)
            or is_control(cp)
            or is_default_ignorable(cp)
            # On older hosts, unknown Unicode16 metadata refuses rather than
            # silently using an incomplete normalization table. Evidence does
            # not go through this metadata validator.
            or unicodedata.category(char) == "Cn"
        ):
            raise ValueError("metadata codepoint refused")
    return value


def _short(value: str) -> str:
    return _metadata(value, 256)


def _reason(value: str) -> str:
    return _metadata(value, 512)


def _message(value: str) -> str:
    return _metadata(value, 1024)


def _edit(value: str) -> str:
    # Suggested replacement is draft/evidence text, not normalized metadata.
    value.encode("utf-8", errors="strict")
    if len(value.encode()) > 2048 or "\r" in value or "\x00" in value:
        raise ValueError("invalid proposed edit")
    return value


def _time(value: str) -> str:
    if len(value) != 20:
        raise ValueError("timestamp must be YYYY-MM-DDTHH:MM:SSZ")
    datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return value


Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]
Identifier = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
ItemId = Annotated[str, Field(pattern=r"^li_[0-9a-f]{64}$", max_length=67)]
RawId = Annotated[str, Field(pattern=r"^rr_[0-9a-f]{64}$", max_length=67)]
SelectionId = Annotated[str, Field(pattern=r"^sel_[0-9a-f]{64}$", max_length=68)]
ProofId = Annotated[str, Field(pattern=r"^prf_[0-9a-f]{64}$", max_length=68)]
Count = Annotated[int, Field(ge=0, le=2**53 - 1)]
Short = Annotated[str, Field(max_length=256), AfterValidator(_short)]
Reason = Annotated[str, Field(max_length=512), AfterValidator(_reason)]
Message = Annotated[str, Field(max_length=1024), AfterValidator(_message)]
Edit = Annotated[str, Field(max_length=2048), AfterValidator(_edit)]
Timestamp = Annotated[
    str,
    Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"),
    AfterValidator(_time),
]
Decision = Literal["pass", "changes_required", "blocked"]


class Closed(BaseModel):
    """Closed, strict and deeply immutable (array fields use tuples)."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Position(Closed):
    line: Annotated[int, Field(ge=1, le=2**53 - 1)]
    column: Annotated[int, Field(ge=1, le=2**53 - 1)]


class Location(Closed):
    """Codepoint range in an exact LF UTF-8 view, not byte/UTF16 offsets."""

    view_sha256: Digest
    start: Position
    end: Position

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if (self.end.line, self.end.column) < (self.start.line, self.start.column):
            raise ValueError("location end precedes start")
        return self


class DraftArtifact(Closed):
    artifact_id: Identifier
    media_type: Annotated[
        str, Field(pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$", max_length=128)
    ]
    sha256: Digest = Field(description="SHA256 of exact draft representation bytes")


class ItemRevision(Closed):
    """Opaque format-only reference, not proof that an item/revision exists."""

    item_id: ItemId
    markdown_sha256: Digest = Field(description="SHA256 of exact LF UTF-8 Markdown")


class Subject(Closed):
    draft_issue_digest: Digest = Field(
        description="SHA256 RFC8785 exact {from_name,subject,plain_text,html} strings"
    )
    artifacts: Annotated[
        tuple[DraftArtifact, ...],
        Field(min_length=1, max_length=64),
        BeforeValidator(_array),
    ]
    selection_id: SelectionId | None = Field(
        description="Opaque sel_ identifier, format only; unresolved is not verified"
    )
    item_revisions: Annotated[
        tuple[ItemRevision, ...], Field(max_length=500), BeforeValidator(_array)
    ]

    @model_validator(mode="after")
    def references(self) -> Self:
        if len({a.artifact_id for a in self.artifacts}) != len(self.artifacts):
            raise ValueError("duplicate artifact id")
        keys = [(item.item_id, item.markdown_sha256) for item in self.item_revisions]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate pinned item revision")
        if self.item_revisions and self.selection_id is None:
            raise ValueError("evidence requires pinned selection")
        return self


class EvidenceReference(Closed):
    """Declared support in visible Markdown; reference syntax is not resolution."""

    item_id: ItemId
    markdown_sha256: Digest = Field(
        description="SHA256 of exact LF UTF-8 item revision"
    )
    expected_digest: Digest = Field(description="Must equal cited markdown_sha256")
    raw_ref_id: RawId = Field(description="Opaque rr_ id, format only; never outward")
    retained_sha256: Digest = Field(description="SHA256 of exact retained RAW bytes")
    location: Location
    visible: bool
    quoted_code_points: Count
    attribution: Short | None
    publisher: Short | None

    @model_validator(mode="after")
    def binding(self) -> Self:
        if not (
            self.expected_digest == self.markdown_sha256 == self.location.view_sha256
        ):
            raise ValueError("evidence revision digest mismatch")
        return self


class Claim(Closed):
    claim_id: Identifier
    text_sha256: Digest = Field(
        description="SHA256 of exact claim text in draft LF view"
    )
    location: Location
    evidence_refs: Annotated[
        tuple[EvidenceReference, ...], Field(max_length=16), BeforeValidator(_array)
    ]
    verdict: Literal[
        "supported", "unsupported", "contradicted", "needs_human", "not_checkable"
    ]

    @model_validator(mode="after")
    def support(self) -> Self:
        if not self.evidence_refs and self.verdict != "unsupported":
            raise ValueError("a claim without evidence is unsupported")
        if self.verdict == "supported" and all(
            not ref.visible for ref in self.evidence_refs
        ):
            raise ValueError("hidden-only support cannot pass")
        return self


class Check(Closed):
    check_id: Identifier
    kind: Literal["attribution", "quote_budget", "language", "house_style", "other"]
    required: bool
    status: Literal["passed", "failed", "not_checked"]
    location: Location
    reason: Reason

    @model_validator(mode="after")
    def house_style_id(self) -> Self:
        if self.kind == "house_style" and not self.check_id.startswith("hs."):
            raise ValueError("house-style check id must start hs.")
        return self


class Finding(Closed):
    finding_id: Identifier
    check_id: Identifier
    severity: Literal["blocker", "major", "minor", "info"]
    responsible_owner: Literal["author", "editor", "source", "proofreader"]
    location: Location
    message: Message
    proposed_edit: Edit | None
    resolved: bool


class QuoteTotal(Closed):
    item_id: ItemId
    markdown_sha256: Digest
    quote_budget: Count
    quoted_code_points: Count


class Excerpt(Closed):
    """Digest-keyed exact text cache, not independent source/span verification.

    A cache entry is self-addressed; no source binding is inferred from it.
    Evidence refs bind whole item revisions separately. Resolving an excerpt
    against those bytes and its location remains a producer responsibility.
    """

    sha256: Digest
    text: Annotated[str, Field(max_length=4096)]

    @model_validator(mode="after")
    def exact(self) -> Self:
        data = self.text.encode("utf-8", errors="strict")
        if len(data) > 4096 or "\r" in self.text or "\x00" in self.text:
            raise ValueError("invalid exact LF excerpt")
        if sha256(data) != self.sha256:
            raise ValueError("excerpt digest mismatch")
        return self


class Provenance(Closed):
    reviewer: Short
    model: Short | None
    session: Short | None
    unavailable_reason: Reason | None

    @model_validator(mode="after")
    def explicit_unavailable(self) -> Self:
        if (
            self.model is None or self.session is None
        ) and self.unavailable_reason is None:
            raise ValueError("unavailable provenance needs a reason")
        return self


class ScorerRun(Closed):
    """Deterministic run declarations, separate from human/model editorial checks."""

    input_sha256: Digest
    tool_version_sha256: Digest
    policy_sha256: Digest


class ProofReport(Closed):
    """Internal shape only. No current read receipt, lifecycle or verified flag.

    Every field is immutable and included in the RFC8785 identity except
    proof_id itself. No missing/null field is silently inferred to be checked.
    """

    schema_tag: Literal["zeo.proof-report/v1"] = Field(alias="schema")
    proof_id: ProofId
    producer: Literal["dt-proofreader"]
    created_at: Timestamp
    sow: Short
    reviewer: Provenance
    subject: Subject
    claim_total: Count
    inventory_complete: bool
    claims: Annotated[tuple[Claim, ...], Field(max_length=500), BeforeValidator(_array)]
    checks: Annotated[tuple[Check, ...], Field(max_length=200), BeforeValidator(_array)]
    findings: Annotated[
        tuple[Finding, ...], Field(max_length=500), BeforeValidator(_array)
    ]
    evidence_digests: Annotated[
        tuple[Digest, ...], Field(max_length=16000), BeforeValidator(_array)
    ]
    per_item_quote_totals: Annotated[
        tuple[QuoteTotal, ...], Field(max_length=500), BeforeValidator(_array)
    ]
    excerpt_cache: Annotated[
        tuple[Excerpt, ...], Field(max_length=500), BeforeValidator(_array)
    ]
    house_style_sha256: Digest
    scorer_runs: Annotated[
        tuple[ScorerRun, ...], Field(max_length=32), BeforeValidator(_array)
    ]
    editorial_decision: Decision
    supersedes: ProofId | None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        self._record_bindings()
        quote_ok, attribution_ok = self._evidence_bindings()
        self._quote_checks(quote_ok, attribution_ok)
        self._inventory()
        self._decision(quote_ok, attribution_ok)
        payload = self.model_dump(mode="json", by_alias=True, exclude={"proof_id"})
        if self.proof_id != "prf_" + canonical_digest(payload):
            raise ValueError("proof_id does not match immutable payload")
        if self.supersedes == self.proof_id:
            raise ValueError("report cannot supersede itself")
        return self

    def _record_bindings(self) -> None:
        for records, attribute in (
            (self.claims, "claim_id"),
            (self.checks, "check_id"),
            (self.findings, "finding_id"),
        ):
            ids = [getattr(record, attribute) for record in records]
            if len(set(ids)) != len(ids):
                raise ValueError("duplicate record id")
        checks = {check.check_id: check for check in self.checks}
        for finding in self.findings:
            if finding.check_id not in checks:
                raise ValueError("unresolved finding check reference")
        kinds = {check.kind for check in self.checks if check.required}
        if not {"attribution", "quote_budget", "language", "house_style"} <= kinds:
            raise ValueError("mandatory checks absent or optional")
        artifact_hashes = {a.sha256 for a in self.subject.artifacts}
        locations = [c.location for c in self.claims] + [
            c.location for c in self.checks
        ]
        locations += [f.location for f in self.findings]
        if any(location.view_sha256 not in artifact_hashes for location in locations):
            raise ValueError("draft location not bound to artifact")

    def _evidence_bindings(self) -> tuple[bool, bool]:
        pins = {(i.item_id, i.markdown_sha256) for i in self.subject.item_revisions}
        refs = [ref for claim in self.claims for ref in claim.evidence_refs]
        if any((ref.item_id, ref.markdown_sha256) not in pins for ref in refs):
            raise ValueError("evidence revision not pinned in subject")
        expected = sorted(
            {d for ref in refs for d in (ref.markdown_sha256, ref.retained_sha256)}
        )
        if list(self.evidence_digests) != expected:
            raise ValueError("evidence digests must be complete sorted unique hashes")
        totals = {(t.item_id, t.markdown_sha256): t for t in self.per_item_quote_totals}
        if len(totals) != len(self.per_item_quote_totals) or set(totals) != pins:
            raise ValueError(
                "per-item quote totals must cover pinned revisions exactly"
            )
        for key, total in totals.items():
            declared = sum(
                ref.quoted_code_points
                for ref in refs
                if (ref.item_id, ref.markdown_sha256) == key
            )
            if total.quoted_code_points != declared:
                raise ValueError("per-item quoted count mismatch")
        quote_ok = all(t.quoted_code_points <= t.quote_budget for t in totals.values())
        attribution_ok = all(
            not ref.quoted_code_points
            or (ref.attribution is not None and ref.publisher is not None)
            for ref in refs
        )
        return quote_ok, attribution_ok

    def _quote_checks(self, quote_ok: bool, attribution_ok: bool) -> None:
        if (
            any(c.kind == "quote_budget" and c.status == "passed" for c in self.checks)
            and not quote_ok
        ):
            raise ValueError("quote-budget check cannot pass over budget")
        if (
            any(c.kind == "attribution" and c.status == "passed" for c in self.checks)
            and not attribution_ok
        ):
            raise ValueError("attribution check cannot pass missing attribution")

    def _inventory(self) -> None:
        if self.claim_total > 500:
            if (
                self.claims
                or self.inventory_complete
                or self.editorial_decision != "blocked"
            ):
                raise ValueError(
                    "over500 requires true count, empty claims, incomplete, blocked"
                )
        elif self.inventory_complete and self.claim_total != len(self.claims):
            raise ValueError("complete inventory count mismatch")
        elif not self.inventory_complete and self.claim_total < len(self.claims):
            raise ValueError("partial inventory exceeds declared total")

    def _decision(self, quote_ok: bool, attribution_ok: bool) -> None:
        blocked = (
            not self.inventory_complete
            or any(c.verdict in {"needs_human", "not_checkable"} for c in self.claims)
            or any(c.required and c.status == "not_checked" for c in self.checks)
            or any(not f.resolved and f.severity == "blocker" for f in self.findings)
        )
        changes = (
            any(c.verdict in {"unsupported", "contradicted"} for c in self.claims)
            or any(c.required and c.status == "failed" for c in self.checks)
            or any(
                not f.resolved and f.severity in {"major", "minor"}
                for f in self.findings
            )
            or not quote_ok
            or not attribution_ok
        )
        decision = "blocked" if blocked else "changes_required" if changes else "pass"
        if self.editorial_decision != decision:
            raise ValueError(
                "editorial decision inconsistent with checks/claims/findings"
            )


def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _refuse_constant(value: str) -> object:
    raise ValueError("non-JSON constant")


def parse_proof_report(data: bytes) -> ProofReport:
    """Strict UTF-8 byte entry point: duplicate keys/non-JSON constants refuse.

    Consumers must use this rather than Pydantic's last-key-wins JSON loader.
    The caller bounds transport input before parsing; the CLI separately caps
    input at 1 MiB, so some model-valid reports cannot use that CLI transport.
    This parses immutable declarations only, never verifies their evidence.
    """
    if type(data) is not bytes:
        raise ValueError("expected exact UTF-8 bytes")
    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_pairs,
            parse_constant=_refuse_constant,
        )
        return ProofReport.model_validate(value)
    except RecursionError:
        raise ValueError("report nesting exceeds parser limit") from None
