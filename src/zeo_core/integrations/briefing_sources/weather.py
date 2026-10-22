"""MET Norway's documented hourly forecast, with absent probability preserved."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from urllib.parse import urlencode
from uuid import uuid4

from zeo_core.contracts.ui.widgets import DataRef
from zeo_core.integrations.briefing_sources.http import (
    PublicSourceHTTP,
    SourceUnavailableError,
)
from zeo_core.integrations.briefing_sources.timeline import unavailable

SOURCE = "MET Norway (api.met.no), CC BY 4.0; hourly subset normalised"


def weather(
    http: PublicSourceHTTP,
    latitude: float,
    longitude: float,
    *,
    location: str,
    arrival: datetime | None,
    fetched_at: datetime,
) -> DataRef:
    try:
        if (
            isinstance(latitude, bool)
            or isinstance(longitude, bool)
            or not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            raise ValueError
        if not isinstance(location, str) or not re.fullmatch(r"[A-Z]{3,4}", location):
            raise ValueError
        query = urlencode({"lat": f"{latitude:.4f}", "lon": f"{longitude:.4f}"})
        response = http.fetch(
            "GET", "https://api.met.no/weatherapi/locationforecast/2.0/compact?" + query
        )
        data = response.data
        hours = data["hours"]
        if not hours:
            raise ValueError
        # Missing precipitation amount is missing data, never zero rain.
        # Restrict to hours for which next_1_hours actually supplies the amount.
        hours = [h for h in hours if h["precip_mm"] is not None][:48]
        if not hours:
            raise ValueError
        timestamp = data["updated_at"]
        arrival_hour = None
        if arrival is not None:
            if arrival.tzinfo is None:
                raise ValueError
            hour = arrival.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
            if any(datetime.fromisoformat(h["time"]) == hour for h in hours):
                arrival_hour = next(
                    h["time"]
                    for h in hours
                    if datetime.fromisoformat(h["time"]) == hour
                )
        return DataRef(
            id="weather-" + uuid4().hex,
            source=SOURCE,
            fetched_at=response.fetched_at.isoformat(),
            as_of=timestamp,
            replay=False,
            payload={
                "location": location,
                "timezone": "UTC",
                "valid_from": hours[0]["time"],
                "valid_to": hours[-1]["time"],
                "hourly": hours,
                "arrival_hour": arrival_hour,
            },
        ).check("weather_hourly")
    except SourceUnavailableError as exc:
        return unavailable("weather_hourly", SOURCE, fetched_at, exc.code)
    except (ValueError, TypeError, KeyError, OverflowError):
        return unavailable(
            "weather_hourly", SOURCE, fetched_at, "weather_data_unavailable"
        )
