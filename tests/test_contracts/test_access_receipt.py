"""Separate receipt vectors; no receipt issuer/current-read side effects.

sow: zeocore-SOW-39 amendment G9-v2-citation-metadata.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from zeo_core.contracts.library.receipt import REFUSAL_STATES, AccessReceipt, receipt_id
from zeo_core.contracts.proof.report import canonical_bytes

FIXTURES = Path(__file__).parent / "fixtures" / "access_receipt"
NAMES = (
    "refuse-auth-receipt.json",
    "refuse-bad-reservation.json",
    "refuse-charge-bool.json",
    "refuse-charge-nonzero.json",
    "refuse-id-mismatch.json",
    "refuse-item-id-present.json",
    "refuse-outcome-unknown.json",
    "refuse-purpose.json",
    "refuse-receipt-bad-timestamp.json",
    "refuse-refused-receipt-with-view-fields.json",
    "refuse-refused-outcome-wrong-state.json",
    "refuse-schema-v1.json",
    "refuse-served-null-view-hash.json",
    "refuse-served-without-view-hash.json",
    "refuse-state-outcome-mismatch.json",
    "refuse-state-unknown.json",
    "refuse-timestamp-offset.json",
    "refuse-unknown-key.json",
    "refuse-view-length-bool.json",
    "refuse-view-length-over.json",
    "valid-refused-inconsistent.json",
    "valid-refused-not-found.json",
    "valid-refused-tombstoned.json",
    "valid-served.json",
)


def test_receipt_manifest_is_closed() -> None:
    assert {p.name for p in FIXTURES.iterdir()} == set(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_receipt_fixture(name: str) -> None:
    data = json.loads((FIXTURES / name).read_bytes())
    if name.startswith("valid-"):
        receipt = AccessReceipt.model_validate(data)
        assert receipt.code_points_charged == 0
        assert receipt.id == data["id"]
    else:
        with pytest.raises(ValidationError):
            AccessReceipt.model_validate(data)


def test_receipt_id_omits_only_id_and_includes_reservation() -> None:
    data = json.loads((FIXTURES / "valid-served.json").read_bytes())
    before = receipt_id(data)
    data["id"] = "ignored"
    assert receipt_id(data) == before
    preimage = {k: v for k, v in data.items() if k != "id"}
    expected = json.dumps(preimage, sort_keys=True, separators=(",", ":")).encode()
    assert canonical_bytes(preimage) == expected
    assert before == "rcp_" + hashlib.sha256(expected).hexdigest()
    data["reservation"] = "7" * 64
    assert receipt_id(data) != before


def test_every_refusal_state() -> None:
    assert set(REFUSAL_STATES) == {
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
    }
    original = json.loads((FIXTURES / "valid-refused-tombstoned.json").read_bytes())
    for outcome, state in REFUSAL_STATES.items():
        data = {**original, "outcome": outcome, "state": state}
        data["id"] = receipt_id(data)
        assert AccessReceipt.model_validate(data).state == state


def test_no_null_or_partial_refusal_view_fields() -> None:
    original = json.loads((FIXTURES / "valid-refused-tombstoned.json").read_bytes())
    for field in ("view_sha256", "view_length", "rights_class"):
        data = {**original, field: None}
        data["id"] = receipt_id(data)
        with pytest.raises(ValidationError):
            AccessReceipt.model_validate(data)


def test_alias_schema_has_all_bounds() -> None:
    from zeo_core.cli.schemas import render_schema

    schema = render_schema("access.receipt-v2")
    assert schema["additionalProperties"] is False
    assert schema["properties"]["reservation"]["minLength"] == 64
    assert schema["properties"]["view_length"]["anyOf"][0]["maximum"] == 4096


def test_cli_refusal_receipt_roundtrip_has_no_absent_view_fields() -> None:
    from zeo_core.cli.__main__ import _validate

    raw = (FIXTURES / "valid-refused-tombstoned.json").read_bytes()
    status, answer = _validate(["access.receipt-v2"], raw)
    assert status == 0
    value = answer["value"]
    assert isinstance(value, dict)
    assert not {"view_sha256", "view_length", "rights_class"} & value.keys()
    echoed = json.dumps(value).encode()
    status, repeated = _validate(["access.receipt-v2"], echoed)
    assert status == 0 and repeated["value"] == value
