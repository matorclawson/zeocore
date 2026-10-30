"""The ``intervention_card`` payload contract (zeocore-SOW-54, slice S1).

Every fixture is fake data. A ``valid`` fixture is a ``DataRef`` plus the
``widget.upsert`` that points at it; a ``negative`` fixture carries the payload and
the verdict (``accepted`` or ``refused``) it must get, and why.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from zeo_core.contracts.ui import intervention_cards as cards
from zeo_core.contracts.ui import widgets
from zeo_core.contracts.ui.intervention_cards import InterventionCardPayload

FIXTURES = Path(__file__).parent / "fixtures" / "intervention_cards"
VALID = sorted((FIXTURES / "valid").glob("*.dataref.json"))
NEGATIVE = sorted((FIXTURES / "negative").glob("*.json"))


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def test_the_fixture_sets_are_not_empty() -> None:
    assert len(VALID) == 15
    assert len(NEGATIVE) == 184


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.name.split(".")[0])
def test_a_valid_card_is_accepted_and_round_trips(path: Path) -> None:
    payload = _load(path)["payload"]
    card = InterventionCardPayload.model_validate(payload)
    again = json.loads(card.model_dump_json(exclude_none=True))
    assert InterventionCardPayload.model_validate(again) == card
    assert all(v is not None for v in again.values())  # absent, never null


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.name.split(".")[0])
def test_each_valid_upsert_names_the_component_and_its_data(path: Path) -> None:
    upsert = _load(path.with_name(path.name.replace(".dataref", ".upsert")))
    assert upsert["component"] == "intervention_card"
    assert upsert["data_ref"] == _load(path)["id"]


@pytest.mark.parametrize("path", NEGATIVE, ids=lambda p: p.stem)
def test_a_fixture_gets_the_verdict_it_states(path: Path) -> None:
    fixture = _load(path)
    assert fixture["why"]
    if fixture["expect"] == "accepted":
        InterventionCardPayload.model_validate(fixture["payload"])
        return
    assert fixture["expect"] == "refused"
    with pytest.raises(ValidationError):
        InterventionCardPayload.model_validate(fixture["payload"])


def _decision() -> dict[str, Any]:
    return copy.deepcopy(_load(FIXTURES / "valid" / "decision.dataref.json")["payload"])


def test_an_unknown_key_is_refused() -> None:
    payload = _decision()
    payload["note"] = "extra"
    with pytest.raises(ValidationError):
        InterventionCardPayload.model_validate(payload)


def test_null_is_not_absent() -> None:
    payload = _decision()
    payload["intervention_ref"] = None
    with pytest.raises(ValidationError):
        InterventionCardPayload.model_validate(payload)


def test_an_unknown_situation_is_refused() -> None:
    payload = _decision()
    payload["situation"] = "calendar_view"
    with pytest.raises(ValidationError):
        InterventionCardPayload.model_validate(payload)


def test_the_payload_is_immutable() -> None:
    card = InterventionCardPayload.model_validate(_decision())
    with pytest.raises(ValidationError):
        card.root = card.root  # type: ignore[misc]


@pytest.mark.parametrize(
    "text",
    ["fine", "a" * 200, "", "a" * 201, "<b>x</b>", "x‮y", "x\ny", "x​z"],
)
def test_the_text_rule_agrees_with_widgets(text: str) -> None:
    ours, theirs = TypeAdapter(cards.Text), TypeAdapter(widgets.Text)

    def accepted(adapter: TypeAdapter[str]) -> bool:
        try:
            adapter.validate_python(text)
        except ValidationError:
            return False
        return True

    assert accepted(ours) == accepted(theirs)


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-30T09:56:00Z",
        "2026-10-30T09:56:00.123456+01:00",
        "2026-10-30T24:00:00Z",
        "2026-13-30T09:56:00Z",
        "2026-02-30T09:56:00Z",
        "2026-10-30 09:56:00Z",
        "2026-10-30T09:56:00",
    ],
)
def test_the_date_rule_agrees_with_widgets(value: str) -> None:
    ours, theirs = TypeAdapter(cards.DateTime), TypeAdapter(widgets.DateTime)

    def accepted(adapter: TypeAdapter[str]) -> bool:
        try:
            adapter.validate_python(value)
        except ValidationError:
            return False
        return True

    assert accepted(ours) == accepted(theirs)


def test_a_project_card_offers_only_the_three_interventions() -> None:
    assert cards.CHECKPOINT_ACTIONS == (
        "request_checkpoint",
        "request_unblock",
        "explain_blocker",
    )
