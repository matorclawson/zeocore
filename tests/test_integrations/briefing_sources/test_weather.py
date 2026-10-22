"""MET recorded response and absent probability, without network access."""

import json
from datetime import datetime
from pathlib import Path

import httpx

from tests.test_integrations.briefing_sources.test_http import NOW, client
from zeo_core.integrations.briefing_sources.weather import weather

FIXTURES = Path(__file__).parents[2] / "fixtures" / "briefing_sources"


def test_recorded_met_hourly_forecast_keeps_actual_time_and_probability_absence() -> (
    None
):
    data = json.loads((FIXTURES / "met-locationforecast.json").read_text())
    requested = data["properties"]["timeseries"][0]["time"]
    http, _ = client(lambda req: httpx.Response(200, json=data))
    try:
        ref = weather(
            http,
            51.4775,
            -0.4614,
            location="EGLL",
            arrival=datetime.fromisoformat(requested),
            fetched_at=NOW,
        )
        assert ref.error is None
        assert ref.as_of == data["properties"]["meta"]["updated_at"]
        assert ref.payload["arrival_hour"] == requested
        assert ref.payload["hourly"][0]["precip_prob"] is None
        assert "CC BY 4.0" in ref.source
    finally:
        http.close()


def test_weather_down_or_malformed_is_empty_unavailable() -> None:
    for response in (httpx.Response(500), httpx.Response(200, json={})):
        http, _ = client(lambda req, response=response: response)
        try:
            ref = weather(
                http, 51.4, -0.4, location="EGLL", arrival=None, fetched_at=NOW
            )
            assert ref.error is not None and ref.payload == {}
        finally:
            http.close()
