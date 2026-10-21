"""The widget-routing contract, built to the rasa-ops specification (SOW-02)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]  # no stubs; a dev dependency
import pytest
from pydantic import ValidationError

from zeo_core.cli.schemas import render_schema
from zeo_core.contracts.ui.widgets import (
    COMPONENTS,
    REGISTRY,
    DataRef,
    WidgetBoard,
    WidgetRefused,
    WidgetUpsert,
)

FIXTURES = Path(__file__).parent / "fixtures"
T = "2026-10-22T13:40:00Z"

PAYLOADS: dict[str, dict[str, Any]] = {
    "flight_status": {
        "flight_number": "BA117",
        "route": {"from": "LHR", "to": "JFK"},
        "scheduled_departure": "2026-10-22T14:25:00Z",
        "estimated_departure": None,
        "scheduled_arrival": "2026-10-22T21:10:00Z",
        "estimated_arrival": "2026-10-22T21:35:00Z",
        "terminal": None,
        "reported_status": "Delayed",
        "source_timestamp": T,
    },
    "weather_hourly": {
        "location": "New York",
        "timezone": "America/New_York",
        "valid_from": "2026-10-22T20:00:00Z",
        "valid_to": "2026-10-23T02:00:00Z",
        "hourly": [
            {
                "time": "2026-10-22T21:00:00Z",
                "temp_c": 14.5,
                "precip_prob": 30,
                "precip_mm": 0.2,
            }
        ],
        "arrival_hour": "2026-10-22T21:00:00Z",
    },
    "arrival_timeline": {
        "steps": [{"label": "Land", "time": "2026-10-22T21:35:00Z"}],
        "transfer_minutes": {"value": 75, "kind": "assumption"},
        "meeting_time": "2026-10-22T22:00:00Z",
        "margin_minutes": -50,
    },
    "market_watchlist": {
        "symbols": [
            {"symbol": "AAPL", "price": 231.4, "change": -1.9, "change_pct": -0.8}
        ],
        "market_status": "open",
        "quote_time": T,
        "delayed": True,
        "series": {"AAPL": [{"time": T, "value": 231.4}]},
    },
}


def ref(
    component: str = "weather_hourly", ref_id: str = "r1", **over: object
) -> DataRef:  # noqa: ANN003 -- field overrides
    body: dict[str, Any] = {
        "id": ref_id,
        "source": "provider.x",
        "fetched_at": T,
        "as_of": T,
        "replay": False,
        "delayed": True if component == "market_watchlist" else None,
        "assumptions": (
            [
                {
                    "label": "transfer to Manhattan (minutes)",
                    "value": 75,
                    "kind": "assumption",
                }
            ]
            if component == "arrival_timeline"
            else []
        ),
        "payload": copy.deepcopy(PAYLOADS[component]),
        "error": None,
    }
    return DataRef(**(body | over))


def upsert(
    component: str = "weather_hourly",
    *,
    request_id: str = "req-1",
    widget_id: str = "arrival-weather",
    data_ref: str = "r1",
    revision: int = 1,
) -> WidgetUpsert:
    return WidgetUpsert(
        type="widget.upsert",
        request_id=request_id,
        widget_id=widget_id,
        component=component,  # type: ignore[arg-type]
        data_ref=data_ref,
        revision=revision,
    )


def test_the_registry_has_the_four_components() -> None:
    assert set(REGISTRY) == set(COMPONENTS) == set(PAYLOADS)


@pytest.mark.parametrize("component", COMPONENTS)
def test_each_component_accepts_its_sample(component: str) -> None:
    ref(component).check(component)


@pytest.mark.parametrize("component", COMPONENTS)
def test_a_payload_for_another_component_is_refused(component: str) -> None:
    other = next(name for name in COMPONENTS if name != component)
    with pytest.raises(WidgetRefused) as refused:
        ref(other).check(component)
    assert refused.value.code in {
        "payload_does_not_match_component",
        "delayed_flag_missing_or_inconsistent",
    }


def test_unknown_keys_coercion_and_markup_are_refused() -> None:
    bad = copy.deepcopy(PAYLOADS["flight_status"]) | {"extra": 1}
    with pytest.raises(WidgetRefused):
        ref("flight_status", payload=bad).check("flight_status")
    bad = copy.deepcopy(PAYLOADS["weather_hourly"])
    bad["hourly"][0]["temp_c"] = "14.5"
    with pytest.raises(WidgetRefused):
        ref(payload=bad).check("weather_hourly")
    bad = copy.deepcopy(PAYLOADS["flight_status"]) | {
        "reported_status": "<b>On time</b>"
    }
    with pytest.raises(WidgetRefused):
        ref("flight_status", payload=bad).check("flight_status")
    bad = copy.deepcopy(PAYLOADS["weather_hourly"])
    bad["hourly"][0]["temp_c"] = float("nan")
    with pytest.raises(WidgetRefused):
        ref(payload=bad).check("weather_hourly")


def test_a_missing_provider_update_time_stays_unknown() -> None:
    assert ref(as_of=None).as_of is None
    with pytest.raises(ValidationError):
        DataRef(**(ref().model_dump() | {"as_of": "yesterday"}))


def test_delayed_market_data_is_labelled_delayed_and_is_not_replay() -> None:
    delayed = ref("market_watchlist")
    assert (delayed.delayed, delayed.replay) == (True, False)
    delayed.check("market_watchlist")
    replayed = ref("market_watchlist", replay=True)
    assert (replayed.delayed, replayed.replay) == (True, True)


@pytest.mark.parametrize("flag", [None, False])
def test_delayed_cannot_be_dropped_or_contradicted(flag: bool | None) -> None:
    with pytest.raises(WidgetRefused) as refused:
        ref("market_watchlist", delayed=flag).check("market_watchlist")
    assert refused.value.code == "delayed_flag_missing_or_inconsistent"


def test_a_transfer_time_is_an_assumption() -> None:
    bad = copy.deepcopy(PAYLOADS["arrival_timeline"])
    bad["transfer_minutes"]["kind"] = "traffic"
    with pytest.raises(WidgetRefused):
        ref("arrival_timeline", payload=bad).check("arrival_timeline")
    with pytest.raises(WidgetRefused) as refused:
        ref("arrival_timeline", assumptions=[]).check("arrival_timeline")
    assert refused.value.code == "transfer_time_not_labelled_as_assumption"


def test_a_failed_source_carries_a_reason_and_no_value() -> None:
    failed = ref(payload={}, error={"reason": "provider timed out"})
    assert failed.check("flight_status").error is not None
    with pytest.raises(WidgetRefused) as refused:
        ref(error={"reason": "x"}).check("weather_hourly")
    assert refused.value.code == "error_with_payload"


def test_an_upsert_updates_the_same_card_in_place() -> None:
    board = WidgetBoard()
    board.begin_request("req-1")
    assert board.apply(upsert(revision=1), ref())
    assert board.apply(upsert(revision=2, data_ref="r2"), ref(ref_id="r2"))
    assert list(board.cards()) == ["arrival-weather"]
    assert board.cards()["arrival-weather"].id == "r2"


def test_a_lower_or_equal_revision_is_ignored() -> None:
    board = WidgetBoard()
    board.begin_request("req-1")
    board.apply(upsert(revision=2), ref())
    for revision in (1, 2):
        assert (
            board.apply(upsert(revision=revision, data_ref="r9"), ref(ref_id="r9"))
            is False
        )
    assert board.cards()["arrival-weather"].id == "r1"


def test_the_old_destination_cannot_overwrite_the_new_one() -> None:
    board = WidgetBoard()
    board.begin_request("req-1")
    board.begin_request("req-2")  # the user changed the destination
    board.apply(upsert(request_id="req-2", data_ref="new"), ref(ref_id="new"))
    with pytest.raises(WidgetRefused) as refused:
        board.apply(upsert(request_id="req-1", revision=9), ref())
    assert refused.value.code == "superseded_request"
    assert board.cards()["arrival-weather"].id == "new"


def test_a_request_can_be_marked_superseded_by_itself() -> None:
    board = WidgetBoard()
    board.begin_request("req-1")
    board.supersede("req-1")
    with pytest.raises(WidgetRefused) as refused:
        board.apply(upsert(), ref())
    assert refused.value.code == "superseded_request"
    with pytest.raises(WidgetRefused) as restarted:
        board.begin_request("req-1")
    assert restarted.value.code == "request_already_superseded"


def test_an_event_before_any_request_is_refused() -> None:
    with pytest.raises(WidgetRefused) as refused:
        WidgetBoard().apply(upsert(), ref())
    assert refused.value.code == "superseded_request"


def test_the_event_and_its_data_must_agree() -> None:
    board = WidgetBoard()
    board.begin_request("req-1")
    with pytest.raises(WidgetRefused) as refused:
        board.apply(upsert(data_ref="other"), ref())
    assert refused.value.code == "data_ref_mismatch"
    with pytest.raises(WidgetRefused):
        board.apply(
            upsert("flight_status"), ref()
        )  # a weather payload on a flight card


def test_a_card_cannot_change_its_component() -> None:
    board = WidgetBoard()
    board.begin_request("req-1")
    board.apply(upsert(), ref())
    with pytest.raises(WidgetRefused) as refused:
        board.apply(
            upsert("flight_status", revision=2, data_ref="r2"),
            ref("flight_status", "r2"),
        )
    assert refused.value.code == "component_changed"


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "widget.delete"},
        {"revision": 0},
        {"widget_id": "Bad_Id"},
        {"widget_id": "-lead"},
        {"component": "iframe"},
        {"request_id": ""},
        {"extra": 1},
    ],
)
def test_malformed_events_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        WidgetUpsert(**(upsert().model_dump() | bad))


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"rasa-ops-{name}.schema.json").read_text())  # type: ignore[no-any-return]


@pytest.mark.parametrize("component", COMPONENTS)
def test_what_we_accept_conforms_to_the_rasa_ops_schemas(component: str) -> None:
    jsonschema.validate(ref(component).model_dump(by_alias=True), load("data-ref"))
    jsonschema.validate(upsert(component).model_dump(), load("widget-upsert"))


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "widget.delete"},
        {"revision": 0},
        {"widget_id": "Bad_Id"},
        {"component": "iframe"},
        {"extra": 1},
    ],
)
def test_what_the_rasa_ops_schema_rejects_we_reject(bad: dict[str, Any]) -> None:
    event = upsert().model_dump() | bad
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(event, load("widget-upsert"))
    with pytest.raises(ValidationError):
        WidgetUpsert(**event)


def test_the_schemas_are_exported() -> None:
    for name in ("ui.widget-upsert", "ui.widget-data-ref"):
        assert render_schema(name)["additionalProperties"] is False
