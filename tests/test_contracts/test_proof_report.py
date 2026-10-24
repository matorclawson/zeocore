"""Internal contract and unresolved-reference vectors (sow: zeocore-SOW-45).

Synthetic declarations only. No outward producer/receiver or live read is
claimed. Runtime scenario expectations do not manufacture fresh receipts.
"""

from __future__ import annotations

import ast
import copy
import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from zeo_core.cli.schemas import render_schema
from zeo_core.contracts.proof import ProofReport
from zeo_core.contracts.proof import report as module
from zeo_core.contracts.proof.report import (
    Excerpt,
    Location,
    canonical_digest,
    draft_issue_digest,
    sha256,
)

FIXTURES = Path(__file__).parent / "fixtures" / "proof_report"


def document(name: str = "valid-minimal.json") -> dict[str, Any]:
    value: dict[str, Any] = json.loads((FIXTURES / name).read_bytes())
    return value


def seal(value: dict[str, Any]) -> dict[str, Any]:
    value["proof_id"] = "prf_" + canonical_digest(
        {k: v for k, v in value.items() if k != "proof_id"}
    )
    return value


def supported() -> dict[str, Any]:
    value = document("refuse-pass-quote-over-budget.json")
    value["per_item_quote_totals"][0]["quote_budget"] = 2
    return seal(value)


def refuse(value: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ProofReport.model_validate(seal(value))


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("valid-*.json")))
def test_acceptance_and_lossless_immutable_roundtrip(path: Path) -> None:
    value = json.loads(path.read_bytes())
    parsed = ProofReport.model_validate(value)
    assert parsed.model_dump(mode="json", by_alias=True) == value
    with pytest.raises(ValidationError):
        parsed.claim_total = 2  # type: ignore[misc]  # deliberate frozen assignment
    assert isinstance(parsed.checks, tuple)
    with pytest.raises(ValidationError):
        parsed.checks[0].status = "not_checked"  # type: ignore[misc]


@pytest.mark.parametrize(
    "path", sorted(FIXTURES.glob("refuse-*.json")), ids=lambda p: p.name
)
def test_refusal_fixtures_through_strict_cli(path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "zeo_core.cli", "validate", "proof.report"],
        input=path.read_bytes(),
        capture_output=True,
        check=False,
    )
    assert result.returncode == (
        2 if path.name == "refuse-duplicate-key.json" else 20
    ), result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["ok"] is False
    # Strict input diagnostics may identify a field, never echo private content.
    assert b"synthetic offline fixture" not in result.stdout


@pytest.mark.parametrize("name", ["proof.report", "zeo.proof-report/v1"])
def test_cli_alias_accepts_exact_model_and_schema(name: str) -> None:
    result = subprocess.run(  # noqa: S603 -- fixed interpreter/module, closed names
        [sys.executable, "-m", "zeo_core.cli", "validate", name],
        input=(FIXTURES / "valid-minimal.json").read_bytes(),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["ok"] is True
    schema = render_schema("proof.report")
    assert schema["additionalProperties"] is False
    assert "lifecycle" not in schema["properties"]
    assert "receiving" not in schema["properties"]


def test_identity_and_exact_transport_digest_are_distinct() -> None:
    value = document()
    assert value["proof_id"] == "prf_" + canonical_digest(
        {k: v for k, v in value.items() if k != "proof_id"}
    )
    assert (
        sha256((FIXTURES / "valid-minimal.json").read_bytes()) != value["proof_id"][4:]
    )
    changed = copy.deepcopy(value)
    changed["created_at"] = "2026-10-25T20:00:00Z"
    with pytest.raises(ValidationError, match="proof_id"):
        ProofReport.model_validate(changed)
    assert ProofReport.model_validate(seal(changed)).proof_id != value["proof_id"]


def test_draft_recipe_preserves_all_four_strings_and_decomposition() -> None:
    fields = {"from_name": "Cafe\u0301", "subject": "S", "plain_text": "P", "html": "H"}
    assert draft_issue_digest(**fields) == canonical_digest(fields)
    assert draft_issue_digest(**fields) != draft_issue_digest(
        **{**fields, "from_name": "Café"}
    )
    for key in fields:
        assert draft_issue_digest(**fields) != draft_issue_digest(
            **{**fields, key: "changed"}
        )
    with pytest.raises(ValueError, match="strings"):
        draft_issue_digest(from_name=1, subject="S", plain_text="P", html="H")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [True, "1", 1.0, -1, 2**53])
def test_counts_are_strict_and_bounded(bad: object) -> None:
    value = document()
    value["claim_total"] = bad
    with pytest.raises(ValidationError, match="claim_total"):
        ProofReport.model_validate(value)


@pytest.mark.parametrize(
    "bad",
    [
        "2026-02-30T00:00:00Z",
        "2026-10-24T20:00:00+00:00",
        "2026-10-24T20:00:00.0Z",
        "0000-01-01T00:00:00Z",
    ],
)
def test_timestamp_profile(bad: str) -> None:
    value = document()
    value["created_at"] = bad
    refuse(value, "created_at")


def test_location_codepoints_are_shape_only_and_pinned() -> None:
    # Emoji and decomposed text stay exact; no conversion to UTF16/byte offsets.
    text = "😀e\u0301\n"
    loc = Location.model_validate(
        {
            "view_sha256": sha256(text.encode()),
            "start": {"line": 1, "column": 1},
            "end": {"line": 1, "column": 4},
        }
    )
    assert loc.end.column == 4
    for start, end in [((1, 0), (1, 1)), ((2, 2), (2, 1)), ((2, 1), (1, 9))]:
        with pytest.raises(ValidationError):
            Location.model_validate(
                {
                    "view_sha256": loc.view_sha256,
                    "start": dict(zip(("line", "column"), start, strict=True)),
                    "end": dict(zip(("line", "column"), end, strict=True)),
                }
            )
    value = document()
    value["checks"][0]["location"]["view_sha256"] = "0" * 64
    refuse(value, "draft location")


def test_reference_bindings_and_visible_support() -> None:
    value = supported()
    assert ProofReport.model_validate(value).editorial_decision == "pass"
    value["claims"][0]["evidence_refs"][0]["visible"] = False
    refuse(value, "hidden-only")
    value = supported()
    value["claims"][0]["evidence_refs"][0]["expected_digest"] = "0" * 64
    refuse(value, "revision digest")
    value = supported()
    value["subject"]["item_revisions"] = []
    refuse(value, "revision not pinned")
    value = supported()
    value["evidence_digests"].reverse()
    refuse(value, "sorted unique")
    value = supported()
    value["subject"]["selection_id"] = None
    refuse(value, "pinned selection")


@pytest.mark.parametrize(
    "kind", ["attribution", "quote_budget", "language", "house_style"]
)
def test_mandatory_checks_and_unperformed_cannot_pass(kind: str) -> None:
    value = document()
    check = next(c for c in value["checks"] if c["kind"] == kind)
    check["required"] = False
    refuse(value, "mandatory")
    value = document()
    check = next(c for c in value["checks"] if c["kind"] == kind)
    check["status"] = "not_checked"
    refuse(value, "editorial decision")
    value["editorial_decision"] = "blocked"
    assert ProofReport.model_validate(seal(value)).editorial_decision == "blocked"


def test_quote_budget_attribution_and_totals_cannot_lie() -> None:
    value = supported()
    value["per_item_quote_totals"][0]["quoted_code_points"] = 1
    refuse(value, "quoted count mismatch")
    value = supported()
    value["per_item_quote_totals"] = []
    refuse(value, "cover pinned")
    value = supported()
    value["claims"][0]["evidence_refs"][0]["publisher"] = None
    refuse(value, "missing attribution")
    value["checks"][0]["status"] = "failed"
    value["editorial_decision"] = "changes_required"
    assert (
        ProofReport.model_validate(seal(value)).editorial_decision == "changes_required"
    )


def test_findings_resolution_ids_and_provenance() -> None:
    value = document()
    value["checks"].append(copy.deepcopy(value["checks"][0]))
    refuse(value, "duplicate record")
    value = document()
    value["subject"]["artifacts"].append(
        copy.deepcopy(value["subject"]["artifacts"][0])
    )
    refuse(value, "duplicate artifact")
    value = supported()
    value["subject"]["item_revisions"].append(
        copy.deepcopy(value["subject"]["item_revisions"][0])
    )
    refuse(value, "duplicate pinned")
    value = document()
    value["checks"][-1]["check_id"] = "style"
    refuse(value, "start hs")
    value = document()
    value["reviewer"]["unavailable_reason"] = None
    refuse(value, "provenance")


@pytest.mark.parametrize(
    "severity,decision",
    [
        ("blocker", "blocked"),
        ("major", "changes_required"),
        ("minor", "changes_required"),
        ("info", "pass"),
    ],
)
def test_findings_are_located_owned_and_decision_relevant(
    severity: str, decision: str
) -> None:
    value = document()
    value["findings"] = [
        {
            "finding_id": "f1",
            "check_id": "language",
            "severity": severity,
            "responsible_owner": "author",
            "location": value["checks"][0]["location"],
            "message": "Synthetic finding",
            "proposed_edit": "e\u0301",
            "resolved": False,
        }
    ]
    value["editorial_decision"] = decision
    assert ProofReport.model_validate(seal(value)).editorial_decision == decision
    value["findings"][0]["check_id"] = "missing"
    refuse(value, "finding check reference")


def test_blocked_dominates_changes_and_over500_never_truncates() -> None:
    value = document("refuse-pass-unsupported-claim.json")
    value["checks"][0]["status"] = "not_checked"
    value["editorial_decision"] = "blocked"
    assert ProofReport.model_validate(seal(value)).editorial_decision == "blocked"
    value = document("valid-claim-total-501-blocked.json")
    value["inventory_complete"] = True
    refuse(value, "over500")
    value = document()
    value["claim_total"] = 1
    refuse(value, "inventory count")


@pytest.mark.parametrize("text", ["e\u0301", "😀", "a\n\tb"])
def test_exact_evidence_never_normalizes(text: str) -> None:
    parsed = Excerpt.model_validate({"text": text, "sha256": sha256(text.encode())})
    assert parsed.text == text
    with pytest.raises(ValidationError, match="digest mismatch"):
        Excerpt.model_validate({"text": text, "sha256": "0" * 64})


@pytest.mark.parametrize("text", ["a\r\nb", "a\x00b", "é" * 2049])
def test_excerpt_byte_bound_and_lf_profile(text: str) -> None:
    with pytest.raises(ValidationError):
        Excerpt.model_validate({"text": text, "sha256": sha256(text.encode())})


def test_unresolved_producer_case_is_structurally_valid_but_unchecked() -> None:
    value = supported()
    value["claims"][0]["verdict"] = "not_checkable"
    for check in value["checks"][:2]:
        check.update(status="not_checked", reason="Synthetic unresolved reference")
    value["editorial_decision"] = "blocked"
    parsed = ProofReport.model_validate(seal(value))
    assert parsed.editorial_decision == "blocked"
    assert parsed.claims[0].verdict == "not_checkable"
    assert all(c.status == "not_checked" for c in parsed.checks[:2])
    assert "verified" not in parsed.model_dump()
    assert "lifecycle" not in parsed.model_dump()


@pytest.mark.parametrize(
    "field", ["as_of", "evaluated_at", "lifecycle", "receiving", "stop", "verified"]
)
def test_runtime_freshness_stop_and_verification_are_not_report_fields(
    field: str,
) -> None:
    value = document()
    value[field] = "synthetic read context"
    refuse(value, field)


@pytest.mark.parametrize(
    "function,fixture",
    [
        ("_inventory", "refuse-pass-incomplete-inventory.json"),
        ("_decision", "refuse-pass-unsupported-claim.json"),
        ("_record_bindings", "refuse-pass-missing-attribution-check.json"),
        ("_quote_checks", "refuse-pass-quote-over-budget.json"),
    ],
)
def test_handmade_validator_mutants_are_killed(
    function: str, fixture: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Replace exactly one validator body in an isolated synthetic module. The
    # intended refusal must disappear, not merely fail with an unrelated error.
    source = Path(module.__file__).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            node.body = [ast.Pass()]
    ast.fix_missing_locations(tree)
    mutant = types.ModuleType("zeo_core.contracts.proof._mutant")
    monkeypatch.setitem(sys.modules, mutant.__name__, mutant)
    exec(compile(tree, "<handmade-proof-mutant>", "exec"), mutant.__dict__)  # noqa: S102 -- fixed own source, deliberate AST mutant
    value = document(fixture)
    if function == "_inventory":
        value = document()
        value["claim_total"] = 1
        seal(value)
    if function == "_quote_checks":
        value["editorial_decision"] = "changes_required"
        seal(value)
    # The original must refuse. The hand-made mutant accepting proves that the
    # named validator is load-bearing for this exact refusal vector.
    with pytest.raises(ValidationError):
        ProofReport.model_validate(value)
    parsed = mutant.__dict__["ProofReport"].model_validate(value)
    assert parsed.proof_id == value["proof_id"]


@pytest.mark.parametrize(
    "function",
    [
        "ordered",
        "references",
        "binding",
        "support",
        "house_style_id",
        "exact",
        "explicit_unavailable",
        "_evidence_bindings",
        "_short",
        "_reason",
        "_message",
        "_edit",
        "_time",
        "_array",
        "consistent",
    ],
)
def test_other_handmade_validators_are_load_bearing(  # noqa: C901 -- explicit mutant matrix
    function: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = (
        supported()
        if function in {"binding", "support", "_evidence_bindings"}
        else document()
    )
    if function == "ordered":
        value["checks"][0]["location"]["end"]["column"] = 0 + 1
        value["checks"][0]["location"]["start"]["column"] = 2
    elif function == "references":
        value["subject"]["artifacts"].append(
            copy.deepcopy(value["subject"]["artifacts"][0])
        )
    elif function == "binding":
        value["claims"][0]["evidence_refs"][0]["expected_digest"] = "0" * 64
    elif function == "support":
        value["claims"][0]["evidence_refs"][0]["visible"] = False
    elif function == "house_style_id":
        value["checks"][-1]["check_id"] = "other-style"
    elif function == "exact":
        value["excerpt_cache"] = [{"text": "synthetic", "sha256": "0" * 64}]
    elif function == "explicit_unavailable":
        value["reviewer"]["unavailable_reason"] = None
    elif function == "_evidence_bindings":
        value["evidence_digests"] = []
    elif function == "_short":
        value["reviewer"]["reviewer"] = "Cafe\u0301"
    elif function == "_reason":
        value["checks"][0]["reason"] = "é" * 300
    elif function in {"_message", "_edit"}:
        value["findings"] = [
            {
                "finding_id": "f",
                "check_id": "language",
                "severity": "info",
                "responsible_owner": "author",
                "location": value["checks"][0]["location"],
                "message": "é" * 600 if function == "_message" else "Synthetic",
                "proposed_edit": "é" * 1500 if function == "_edit" else None,
                "resolved": False,
            }
        ]
    elif function == "_time":
        value["created_at"] = "2026-02-30T20:00:00Z"
    elif function == "_array":
        value["scorer_runs"] = ""
    elif function == "consistent":
        value["created_at"] = "2026-10-25T20:00:00Z"
    if function == "_array":
        normalized = {**value, "scorer_runs": []}
        value["proof_id"] = seal(normalized)["proof_id"]
    elif function != "consistent":
        seal(value)
    with pytest.raises(ValidationError):
        ProofReport.model_validate(value)
    tree = ast.parse(Path(module.__file__).read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != function:
            continue
        if function == "_array":
            for child in ast.walk(node):
                if isinstance(child, ast.If):
                    child.body = [ast.Pass()]
        elif function == "_evidence_bindings":
            node.body = [
                ast.Return(
                    ast.Tuple([ast.Constant(True), ast.Constant(True)], ast.Load())
                )
            ]
        elif function.startswith("_"):
            node.body = [ast.Return(ast.Name("value", ast.Load()))]
        else:
            node.body = [ast.Return(ast.Name("self", ast.Load()))]
    ast.fix_missing_locations(tree)
    mutant = types.ModuleType("zeo_core.contracts.proof._mutant")
    monkeypatch.setitem(sys.modules, mutant.__name__, mutant)
    exec(compile(tree, "<handmade-proof-mutant>", "exec"), mutant.__dict__)  # noqa: S102 -- fixed source AST, deliberate validator mutant
    assert (
        mutant.__dict__["ProofReport"].model_validate(value).proof_id
        == value["proof_id"]
    )
