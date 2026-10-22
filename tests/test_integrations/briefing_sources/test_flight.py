"""Synthetic success vectors plus the actual empty-position smoke recording."""

import json
from pathlib import Path

import httpx
import pytest

from tests.test_integrations.briefing_sources.test_http import NOW, client
from zeo_core.integrations.briefing_sources.flight import flight

FIXTURES = Path(__file__).parents[2] / "fixtures" / "briefing_sources"


def handler(request: httpx.Request) -> httpx.Response:
    if request.method == "GET":
        return httpx.Response(
            200,
            json={
                "now": NOW.timestamp() * 1000,
                "ac": [
                    {
                        "flight": "TEST123 ",
                        "lat": 50.0,
                        "lon": 0.0,
                        "gs": 400.0,
                        "seen_pos": 5,
                        "alt_baro": 30000,
                        "passenger": "PRIVATE CANARY",
                    }
                ],
            },
        )
    return httpx.Response(
        200,
        json=[
            {
                "callsign": "TEST123",
                "plausible": True,
                "_airports": [
                    {"icao": "KJFK", "lat": 40.6, "lon": -73.7},
                    {"icao": "EGLL", "lat": 51.4775, "lon": -0.4614},
                ],
            }
        ],
    )


def test_position_computes_estimate_with_no_schedule_or_personal_data() -> None:
    http, _ = client(handler)
    try:
        result = flight(http, "TEST123", fetched_at=NOW)
        ref = result.reference
        assert ref.error is None and ref.replay is False
        assert ref.payload["scheduled_arrival"] is None
        assert ref.payload["scheduled_departure"] is None
        assert ref.payload["estimated_arrival"] is not None
        assert "PRIVATE" not in repr(ref)
        assert "delayed" not in ref.payload["reported_status"].lower()
        assert "on time" not in ref.payload["reported_status"].lower()
        assert result.destination == "EGLL"
        assert ref.assumptions[0].label == "estimated_arrival_from_live_position"
    finally:
        http.close()


@pytest.mark.parametrize(
    "field,value", [("seen_pos", 121), ("gs", 0), ("lat", 91), ("alt_baro", "ground")]
)
def test_missing_or_stale_position_is_unavailable(field: str, value: object) -> None:
    def bad(request: httpx.Request) -> httpx.Response:
        response = handler(request)
        data = response.json()
        data["ac"][0][field] = value
        return httpx.Response(200, json=data)

    http, _ = client(bad)
    try:
        ref = flight(http, "TEST123", fetched_at=NOW).reference
        assert ref.error is not None and ref.payload == {}
        assert " " in ref.error.reason
    finally:
        http.close()


def test_actual_smoke_empty_aircraft_response_is_unavailable() -> None:
    recorded = json.loads((FIXTURES / "adsb-position.json").read_text())
    http, _ = client(lambda req: httpx.Response(200, json=recorded))
    try:
        ref = flight(http, "UAE4", fetched_at=NOW).reference
        assert ref.error is not None and ref.payload == {}
    finally:
        http.close()


def test_all_adapter_attributions_survive_the_actual_shared_renderer() -> None:
    from datetime import timedelta

    from tests.test_integrations.briefing_sources.test_market import SAMPLE
    from zeo_core.adapters.widget_html import render_card_html
    from zeo_core.contracts.ui.widgets import WidgetBoard, WidgetUpsert
    from zeo_core.integrations.briefing_sources import PublicBriefingSources

    def sources(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.met.no":
            return httpx.Response(
                200,
                json=json.loads((FIXTURES / "met-locationforecast.json").read_text()),
            )
        if request.url.host == "data-api.ecb.europa.eu":
            return httpx.Response(200, text=SAMPLE)
        return handler(request)

    http, _ = client(sources)
    source = PublicBriefingSources(http, clock=lambda: NOW)
    try:
        refs = source.read_briefing(
            "TEST123",
            transfer_minutes=45,
            meeting_time=NOW + timedelta(hours=5),
            currencies=["USD"],
        )
        assert len(refs) == 4
        board = WidgetBoard()
        board.begin_request("attribution-test")
        for component, reference in refs.items():
            assert reference.error is None
            event = WidgetUpsert.model_validate(
                {
                    "widget_id": component.replace("_", "-"),
                    "type": "widget.upsert",
                    "component": component,
                    "revision": 1,
                    "request_id": "attribution-test",
                    "data_ref": reference.id,
                }
            )
            assert board.apply(event, reference)
        for card in board.snapshot():
            rendered = render_card_html(card)
            assert card.data_ref.source in rendered
            assert "[withheld]" not in rendered
            if card.component == "market_watchlist":
                assert (
                    "change and change % COMPUTED from two reference days" in rendered
                )
                assert "not provider data" in rendered
        assert "ODbL 1.0" in refs["flight_status"].source
        assert "CC BY 4.0" in refs["weather_hourly"].source
    finally:
        source.close()


@pytest.mark.parametrize("route_status", [200, 201])
def test_routeset_created_status_keeps_valid_flight(route_status: int) -> None:
    """Ops observed HTTP 201; payload remains a synthetic controlled vector."""

    def created(request: httpx.Request) -> httpx.Response:
        response = handler(request)
        status = route_status if request.method == "POST" else 200
        return httpx.Response(status, json=response.json())

    http, _ = client(created)
    try:
        result = flight(http, "TEST123", fetched_at=NOW)
        assert result.reference.error is None
        assert result.destination == "EGLL"
        assert result.reference.payload["estimated_arrival"] is not None
    finally:
        http.close()


@pytest.mark.parametrize("stated", [False, True])
def test_empty_route_keeps_position_and_only_stated_destination_computes(
    stated: bool,
) -> None:
    from zeo_core.adapters.widget_html import render_card_html
    from zeo_core.contracts.ui.widgets import WidgetBoard, WidgetUpsert
    from zeo_core.integrations.briefing_sources.flight import StatedDestination

    def empty(request: httpx.Request) -> httpx.Response:
        return handler(request) if request.method == "GET" else httpx.Response(201)

    http, _ = client(empty)
    try:
        destination = StatedDestination("EGLL", 51.4775, -0.4614) if stated else None
        result = flight(http, "TEST123", fetched_at=NOW, stated_destination=destination)
        ref = result.reference
        assert ref.error is None and not ref.replay
        assert result.latitude == 50 and result.ground_speed_knots == 400
        assert "Route not provided by the source" in ref.payload["reported_status"]
        assert "30000 ft, 400 kt, position 5 s old" in ref.payload["reported_status"]
        assert (ref.payload["estimated_arrival"] is not None) is stated
        assert ref.payload["route"]["to"] == ("EGLL" if stated else "not provided")
        board = WidgetBoard()
        board.begin_request("empty-route")
        event = WidgetUpsert.model_validate(
            {
                "type": "widget.upsert",
                "widget_id": "flight",
                "component": "flight_status",
                "revision": 1,
                "request_id": "empty-route",
                "data_ref": ref.id,
            }
        )
        assert board.apply(event, ref)
        rendered = render_card_html(board.snapshot()[0])
        assert "Route not provided by the source" in rendered
        assert "unavailable" not in rendered
        if stated:
            assert "destination stated by traveller" in rendered
            assert "ASSUMPTION" in rendered
            assert "COMPUTED FROM LIVE POSITION" in rendered
            assert {item.label for item in ref.assumptions} == {
                "estimated_arrival_from_live_position",
                "destination_stated_by_traveller",
            }
        else:
            assert ref.assumptions == []
    finally:
        http.close()


def test_stated_destination_overrides_conflicting_source_route_with_label() -> None:
    from zeo_core.integrations.briefing_sources.flight import StatedDestination

    http, _ = client(handler)
    try:
        ref = flight(
            http,
            "TEST123",
            fetched_at=NOW,
            stated_destination=StatedDestination("EDDF", 50.0333, 8.57056),
        ).reference
        assert ref.error is None
        assert ref.payload["route"]["to"] == "EDDF"
        assert "source route differs" in ref.payload["reported_status"]
        assert "destination stated by traveller EDDF" in ref.payload["reported_status"]
        assert any(
            "destination stated by traveller EDDF" in str(item.value)
            for item in ref.assumptions
        )
    finally:
        http.close()


@pytest.mark.parametrize(
    "code,lat,lon",
    [
        ("../EGLL", 50, 0),
        ("EGLL", float("nan"), 0),
        ("EGLL", 91, 0),
        ("EGLL", 50, 181),
        ("EGLL", True, 0),
    ],
)
def test_invalid_stated_destination_is_refused_before_connection(
    code: str, lat: float, lon: float
) -> None:
    from zeo_core.integrations.briefing_sources.flight import StatedDestination

    calls = []

    def count(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    http, _ = client(count)
    try:
        ref = flight(
            http,
            "TEST123",
            fetched_at=NOW,
            stated_destination=StatedDestination(code, lat, lon),
        ).reference
        assert ref.error is not None and ref.payload == {}
        assert calls == []
    finally:
        http.close()


def test_empty_route_with_stated_airport_makes_four_real_reader_references() -> None:
    from datetime import timedelta

    from tests.test_integrations.briefing_sources.test_market import SAMPLE
    from zeo_core.integrations.briefing_sources import (
        PublicBriefingSources,
        StatedDestination,
    )

    def sources(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.adsb.lol":
            return handler(request) if request.method == "GET" else httpx.Response(201)
        if request.url.host == "api.met.no":
            return httpx.Response(
                200,
                json=json.loads((FIXTURES / "met-locationforecast.json").read_text()),
            )
        assert request.url.host == "data-api.ecb.europa.eu"
        return httpx.Response(200, text=SAMPLE)

    http, _ = client(sources)
    source = PublicBriefingSources(http, clock=lambda: NOW)
    try:
        refs = source.read_briefing(
            "TEST123",
            transfer_minutes=45,
            meeting_time=NOW + timedelta(hours=5),
            currencies=("USD",),
            stated_destination=StatedDestination("EGLL", 51.4775, -0.4614),
        )
        assert len(refs) == 4
        assert all(ref.error is None and not ref.replay for ref in refs.values())
        assert "destination stated by traveller EGLL" in refs["arrival_timeline"].source
        assert refs["weather_hourly"].payload["location"] == "EGLL"
    finally:
        source.close()
