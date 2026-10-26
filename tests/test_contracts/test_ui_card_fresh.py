"""zeocore-SOW-46: the ``ui.result-card-fresh`` profile, vector by vector.

The vectors are files in ``fixtures/ui_result_card/fresh/`` so a consumer can run the
same ones: every ``valid-*`` file is accepted and every ``refused-*`` file is refused,
by the model, the byte entry and the CLI alike. The default profile must keep
accepting what it accepted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zeo_core.cli.__main__ import _validate_bytes_contract
from zeo_core.cli.schemas import BYTE_CONTRACTS, render_schema, resolve
from zeo_core.contracts.ui import UiRefused, errors_of, validate_bytes

FIXTURES = Path(__file__).parent / "fixtures" / "ui_result_card"
VECTORS = sorted((FIXTURES / "fresh").glob("*.json"))
VALID = [path for path in VECTORS if path.name.startswith("valid-")]
REFUSED = [path for path in VECTORS if path.name.startswith("refused-")]
#: What the DEFAULT profile did before this one existed: these stay refused there too.
REFUSED_BY_BOTH = {
    "refused-expiry-equals-observation.json",
    "refused-expiry-before-observation.json",
    "refused-observed-after-produced.json",
    "refused-calendar-invalid-expiry.json",
    "refused-expiry-with-offset.json",
    "refused-bad-source-kind.json",
    "refused-source-ref-with-at-sign.json",
    "refused-source-ref-non-ascii.json",
    "refused-title-default-ignorable.json",
    "refused-null-expires-at.json",
    "refused-null-snapshot.json",
    "refused-no-source.json",
    "refused-no-observed-at.json",
}


def test_the_vectors_are_all_named_and_nothing_is_skipped() -> None:
    assert len(VALID) == 3
    assert len(REFUSED) == 15
    assert len(VALID) + len(REFUSED) == len(VECTORS)


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.name)
def test_a_valid_vector_is_accepted_by_both_profiles(path: Path) -> None:
    raw = path.read_bytes()
    assert errors_of(raw, "fresh") == []
    assert errors_of(raw) == []
    result = validate_bytes(raw, "fresh")
    assert result.components[0].snapshot is not None
    assert result.model_dump(
        mode="json", by_alias=True, exclude_none=True
    ) == json.loads(raw)


@pytest.mark.parametrize("path", REFUSED, ids=lambda p: p.name)
def test_a_refused_vector_is_refused_by_the_fresh_profile(path: Path) -> None:
    raw = path.read_bytes()
    assert errors_of(raw, "fresh") != []
    with pytest.raises(UiRefused):
        validate_bytes(raw, "fresh")


@pytest.mark.parametrize("path", REFUSED, ids=lambda p: p.name)
def test_only_the_missing_snapshot_vectors_differ_between_profiles(path: Path) -> None:
    default_refused = errors_of(path.read_bytes()) != []
    assert default_refused == (path.name in REFUSED_BY_BOTH), path.name


def test_the_default_profile_still_accepts_a_card_with_no_snapshot() -> None:
    raw = (FIXTURES / "fresh" / "refused-no-snapshot.json").read_bytes()
    assert errors_of(raw) == []
    assert errors_of(raw, "fresh") != []
    legacy = (FIXTURES / "valid-labels-card.json").read_bytes()
    assert errors_of(legacy) == []
    assert errors_of(legacy, "fresh") != []  # a snapshot without expires_at


def test_the_cli_agrees_with_the_model_on_every_vector() -> None:
    assert "ui.result-card-fresh" in BYTE_CONTRACTS
    assert resolve("ui.result-card-fresh") == "ui.result-card-fresh"
    assert resolve("zeo.ui-result/v1") == "ui.result-card"  # the wire tag is unchanged
    for path in VECTORS:
        status, answer = _validate_bytes_contract(
            "ui.result-card-fresh", path.read_bytes()
        )
        assert (status == 0) == path.name.startswith("valid-"), path.name
        assert answer["ok"] is (status == 0)


def test_a_refusal_never_echoes_input() -> None:
    raw = (FIXTURES / "fresh" / "refused-source-ref-with-at-sign.json").read_bytes()
    _, answer = _validate_bytes_contract("ui.result-card-fresh", raw)
    assert "registry@abc" not in json.dumps(answer)


def test_the_exported_schema_requires_the_snapshot_and_keeps_the_wire_tag() -> None:
    schema = render_schema("ui.result-card-fresh")
    text = json.dumps(schema)
    assert '"const": "zeo.ui-result/v1"' in text
    snapshot = schema["$defs"]["FreshSnapshot"]
    assert sorted(snapshot["required"]) == ["expires_at", "observed_at", "source"]
    component = schema["$defs"]["FreshCardComponent"]
    assert "snapshot" in component["required"]
    assert (
        "snapshot"
        not in render_schema("ui.result-card")["$defs"]["CardComponent"]["required"]
    )
