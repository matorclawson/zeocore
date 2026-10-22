"""Computed timeline is bound to the flight and explicitly stated inputs."""

from datetime import datetime, timedelta

from tests.test_integrations.briefing_sources.test_flight import handler
from tests.test_integrations.briefing_sources.test_http import NOW, client
from zeo_core.integrations.briefing_sources.flight import flight
from zeo_core.integrations.briefing_sources.timeline import arrival_timeline


def test_timeline_margin_and_bound_assumption_not_model_prose() -> None:
    http, _ = client(handler)
    try:
        ref = flight(http, "TEST123", fetched_at=NOW).reference
        output = arrival_timeline(
            ref,
            transfer_minutes=90.0,
            meeting_time=NOW + timedelta(hours=3),
            fetched_at=NOW,
        )
        assert output.error is None
        assert output.payload["transfer_minutes"] == {
            "value": 90.0,
            "kind": "assumption",
        }
        assert output.assumptions[0].value == 90.0
        landing = datetime.fromisoformat(ref.payload["estimated_arrival"])
        expected = ((NOW + timedelta(hours=3)) - landing).total_seconds() / 60 - 90
        assert output.payload["margin_minutes"] == round(expected)
        for transfer in (-1.0, float("nan")):
            bad = arrival_timeline(
                ref, transfer_minutes=transfer, meeting_time=NOW, fetched_at=NOW
            )
            assert bad.error is not None and bad.payload == {}
    finally:
        http.close()


def test_public_facade_keeps_independent_sources_and_validated_models() -> None:
    import json

    import httpx

    from tests.test_integrations.briefing_sources.test_market import SAMPLE
    from tests.test_integrations.briefing_sources.test_weather import FIXTURES
    from zeo_core.contracts.ui.widgets import DataRef
    from zeo_core.integrations.briefing_sources import PublicBriefingSources

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.met.no":
            return httpx.Response(
                200,
                json=json.loads((FIXTURES / "met-locationforecast.json").read_text()),
            )
        if request.url.host == "data-api.ecb.europa.eu":
            return httpx.Response(200, text=SAMPLE)
        return handler(request)

    http, clock = client(transport)
    sources = PublicBriefingSources(http, clock=clock.now)
    try:
        refs = sources.read_briefing(
            "TEST123",
            transfer_minutes=90.0,
            meeting_time=NOW + timedelta(hours=3),
            currencies=["USD"],
        )
        assert set(refs) == {
            "flight_status",
            "weather_hourly",
            "arrival_timeline",
            "market_watchlist",
        }
        assert all(isinstance(r, DataRef) and r.error is None for r in refs.values())
        assert sources.flight("TEST123").error is None
        assert (
            sources.arrival_timeline(
                refs["flight_status"], transfer_minutes=0.0, meeting_time=NOW
            ).error
            is None
        )
    finally:
        sources.close()


def test_stated_destination_is_named_in_timeline_source_and_assumption() -> None:
    from tests.test_integrations.briefing_sources.test_flight import handler
    from tests.test_integrations.briefing_sources.test_http import NOW, client
    from zeo_core.integrations.briefing_sources.flight import StatedDestination, flight

    http, _ = client(handler)
    try:
        ref = flight(
            http,
            "TEST123",
            fetched_at=NOW,
            stated_destination=StatedDestination("EGLL", 51.4775, -0.4614),
        ).reference
        result = arrival_timeline(
            ref,
            transfer_minutes=45,
            meeting_time=NOW + timedelta(hours=5),
            fetched_at=NOW,
        )
        assert result.error is None
        assert "destination stated by traveller EGLL" in result.source
        assert any(
            item.label == "destination_stated_by_traveller"
            for item in result.assumptions
        )
    finally:
        http.close()
