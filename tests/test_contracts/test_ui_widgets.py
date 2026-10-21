"""The widget-routing contract (software-factories-masterclass-SOW-02)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from zeo_core.cli.schemas import render_schema
from zeo_core.contracts.ui.widgets import (
    COMPONENTS,
    REGISTRY,
    WidgetBoard,
    WidgetData,
    WidgetRefused,
    WidgetUpsert,
)

PROV: dict[str, Any] = {
    "source": "provider.flights",
    "observed_at": "2026-10-22T13:40:00Z",
    "basis": "live",
}


def sample(component: str) -> dict[str, Any]:
    return copy.deepcopy(
        {
            "flight_status": {
                "flight": "BA117",
                "origin": "LHR",
                "destination": "JFK",
                "scheduled_at": "2026-10-22T14:25:00Z",
                "estimated_at": "2026-10-22T14:50:00Z",
                "status": "Delayed",
                "provenance": PROV,
            },
            "weather_hourly": {
                "location": "New York",
                "hours": [
                    {
                        "at": "2026-10-22T21:00:00Z",
                        "temperature_c": "14.5",
                        "precipitation_percent": 30,
                    }
                ],
                "provenance": {**PROV, "timezone": "America/New_York"},
            },
            "arrival_timeline": {
                "steps": [
                    {
                        "kind": "flight",
                        "label": "Land",
                        "at": "2026-10-22T21:10:00Z",
                        "basis": "live",
                    },
                    {
                        "kind": "transfer",
                        "label": "Transfer to Manhattan",
                        "at": "2026-10-22T22:30:00Z",
                        "basis": "assumption",
                    },
                ],
                "provenance": {**PROV, "basis": "assumption"},
            },
            "market_watchlist": {
                "rows": [
                    {"symbol": "AAPL", "price": "231.40", "change_percent": "-0.8"}
                ],
                "market_status": "open",
                "provenance": {**PROV, "basis": "delayed", "delay_minutes": 15},
            },
        }[component]
    )


def envelope(component: str, ref: str = "r1") -> WidgetData:
    return WidgetData(data_ref=ref, component=component, data=sample(component))  # type: ignore[arg-type]


def upsert(
    component: str = "weather_hourly",
    *,
    request_id: str = "req-1",
    widget_id: str = "arrival-weather",
    ref: str = "r1",
    revision: int = 1,
) -> WidgetUpsert:
    return WidgetUpsert(
        type="widget.upsert",
        request_id=request_id,
        widget_id=widget_id,
        component=component,  # type: ignore[arg-type]
        data_ref=ref,
        revision=revision,
    )


def test_the_registry_has_the_four_components() -> None:
    assert (
        set(REGISTRY)
        == set(COMPONENTS)
        == {
            "flight_status",
            "weather_hourly",
            "arrival_timeline",
            "market_watchlist",
        }
    )


@pytest.mark.parametrize("component", COMPONENTS)
def test_each_component_accepts_its_sample(component: str) -> None:
    assert envelope(component).component == component


def test_data_must_match_its_component() -> None:
    with pytest.raises(ValidationError):
        WidgetData(
            data_ref="r1", component="flight_status", data=sample("weather_hourly")
        )


def test_unknown_keys_and_coercion_are_refused() -> None:
    data = sample("flight_status")
    data["extra"] = 1
    with pytest.raises(ValidationError):
        WidgetData(data_ref="r1", component="flight_status", data=data)
    data = sample("weather_hourly")
    data["hours"][0]["precipitation_percent"] = "30"
    with pytest.raises(ValidationError):
        WidgetData(data_ref="r1", component="weather_hourly", data=data)


def test_delay_is_required_for_delayed_data_and_only_for_it() -> None:
    data = sample("market_watchlist")
    del data["provenance"]["delay_minutes"]
    with pytest.raises(ValidationError, match="delay_minutes"):
        WidgetData(data_ref="r1", component="market_watchlist", data=data)
    data = sample("market_watchlist")
    data["provenance"]["basis"] = "live"
    with pytest.raises(ValidationError, match="delay_minutes"):
        WidgetData(data_ref="r1", component="market_watchlist", data=data)


def test_a_transfer_time_can_only_be_an_assumption() -> None:
    data = sample("arrival_timeline")
    data["steps"][1]["basis"] = "live"
    with pytest.raises(ValidationError, match="assumption"):
        WidgetData(data_ref="r1", component="arrival_timeline", data=data)


def test_market_data_is_never_an_assumption() -> None:
    data = sample("market_watchlist")
    data["provenance"] = {**PROV, "basis": "assumption"}
    with pytest.raises(ValidationError, match="never an assumption"):
        WidgetData(data_ref="r1", component="market_watchlist", data=data)


def test_a_replayed_fixture_carries_the_replay_label() -> None:
    data = sample("flight_status")
    data["provenance"]["basis"] = "replay"
    assert envelope_from(data).data["provenance"]["basis"] == "replay"


def envelope_from(data: dict[str, Any]) -> WidgetData:
    return WidgetData(data_ref="r1", component="flight_status", data=data)


@pytest.mark.parametrize("bad", ["1.5.5", "1e3", " 1", "", "NaN", "+1"])
def test_decimals_are_plain_strings(bad: str) -> None:
    data = sample("market_watchlist")
    data["rows"][0]["price"] = bad
    with pytest.raises(ValidationError):
        WidgetData(data_ref="r1", component="market_watchlist", data=data)


def test_an_upsert_updates_the_same_card_in_place() -> None:
    board = WidgetBoard()
    board.begin_request("req-1")
    board.apply(upsert(revision=1), envelope("weather_hourly"))
    board.apply(upsert(revision=2, ref="r2"), envelope("weather_hourly", "r2"))
    assert list(board.cards()) == ["arrival-weather"]
    assert board.cards()["arrival-weather"].data_ref == "r2"


def test_a_lower_or_equal_revision_never_overwrites() -> None:
    board = WidgetBoard()
    board.begin_request("req-1")
    board.apply(upsert(revision=2), envelope("weather_hourly"))
    for revision in (1, 2):
        with pytest.raises(WidgetRefused) as refused:
            board.apply(
                upsert(revision=revision, ref="r9"), envelope("weather_hourly", "r9")
            )
        assert refused.value.code == "stale_revision"
    assert board.cards()["arrival-weather"].data_ref == "r1"


def test_the_old_destination_cannot_overwrite_the_new_one() -> None:
    board = WidgetBoard()
    board.begin_request("req-1")
    board.begin_request("req-2")  # the user changed the destination
    board.apply(
        upsert(request_id="req-2", ref="new"), envelope("weather_hourly", "new")
    )
    with pytest.raises(WidgetRefused) as refused:
        board.apply(upsert(request_id="req-1", revision=9), envelope("weather_hourly"))
    assert refused.value.code == "superseded_request"
    assert board.cards()["arrival-weather"].data_ref == "new"


def test_a_superseded_request_cannot_be_restarted() -> None:
    board = WidgetBoard()
    board.begin_request("req-1")
    board.begin_request("req-2")
    with pytest.raises(WidgetRefused) as refused:
        board.begin_request("req-1")
    assert refused.value.code == "request_already_superseded"


def test_an_event_before_any_request_is_refused() -> None:
    with pytest.raises(WidgetRefused) as refused:
        WidgetBoard().apply(upsert(), envelope("weather_hourly"))
    assert refused.value.code == "superseded_request"


def test_the_event_and_its_data_must_agree() -> None:
    board = WidgetBoard()
    board.begin_request("req-1")
    with pytest.raises(WidgetRefused) as refused:
        board.apply(upsert(ref="other"), envelope("weather_hourly"))
    assert refused.value.code == "data_ref_mismatch"
    with pytest.raises(WidgetRefused) as refused:
        board.apply(upsert("flight_status"), envelope("weather_hourly"))
    assert refused.value.code == "component_mismatch"


def test_a_card_cannot_change_its_component() -> None:
    board = WidgetBoard()
    board.begin_request("req-1")
    board.apply(upsert(), envelope("weather_hourly"))
    with pytest.raises(WidgetRefused) as refused:
        board.apply(
            upsert("flight_status", revision=2, ref="r2"),
            envelope("flight_status", "r2"),
        )
    assert refused.value.code == "component_changed"


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "widget.delete"},
        {"revision": 0},
        {"widget_id": "Bad Id"},
        {"component": "iframe"},
    ],
)
def test_malformed_events_are_refused(bad: dict[str, Any]) -> None:
    event = upsert().model_dump() | bad
    with pytest.raises(ValidationError):
        WidgetUpsert(**event)


def test_the_schemas_are_exported() -> None:
    for name in ("ui.widget-upsert", "ui.widget-data"):
        assert render_schema(name)["additionalProperties"] is False
