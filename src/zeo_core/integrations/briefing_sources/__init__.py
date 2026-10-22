"""Keyless public briefing sources; Core contracts own every displayed value."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

import httpx

from zeo_core.contracts.ui.widgets import DataRef
from zeo_core.integrations.briefing_sources.flight import flight as read_flight
from zeo_core.integrations.briefing_sources.http import (
    PublicSourceHTTP,
    SourceUnavailableError,
    utc_now,
)
from zeo_core.integrations.briefing_sources.market import market as read_market
from zeo_core.integrations.briefing_sources.timeline import (
    arrival_timeline,
    unavailable,
)
from zeo_core.integrations.briefing_sources.weather import weather as read_weather

__all__ = [
    "PublicBriefingSources",
    "PublicSourceHTTP",
    "SourceUnavailableError",
    "arrival_timeline",
]


class PublicBriefingSources:
    """One bounded run, with shared per-source cache and explicit clock/transport."""

    def __init__(
        self,
        http: PublicSourceHTTP | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.http = http or PublicSourceHTTP(transport=transport, clock=clock)
        self._clock = clock

    def flight(self, callsign: str) -> DataRef:
        return read_flight(self.http, callsign, fetched_at=self._clock()).reference

    def weather(
        self,
        latitude: float,
        longitude: float,
        *,
        location: str,
        arrival: datetime | None = None,
    ) -> DataRef:
        return read_weather(
            self.http,
            latitude,
            longitude,
            location=location,
            arrival=arrival,
            fetched_at=self._clock(),
        )

    def market(self, currencies: Sequence[str]) -> DataRef:
        return read_market(self.http, currencies, fetched_at=self._clock())

    def arrival_timeline(
        self, flight: DataRef, *, transfer_minutes: float, meeting_time: datetime
    ) -> DataRef:
        return arrival_timeline(
            flight,
            transfer_minutes=transfer_minutes,
            meeting_time=meeting_time,
            fetched_at=self._clock(),
        )

    def read_briefing(
        self,
        callsign: str,
        *,
        transfer_minutes: float,
        meeting_time: datetime,
        currencies: Sequence[str] = ("USD", "GBP"),
    ) -> dict[str, DataRef]:
        """Independent source failures remain visible; never substitute a replay."""
        now = self._clock()
        observed = read_flight(self.http, callsign, fetched_at=now)
        ref = observed.reference
        if (
            observed.destination_latitude is not None
            and observed.destination_longitude is not None
            and observed.destination is not None
        ):
            estimate = ref.payload.get("estimated_arrival")
            arrival = (
                datetime.fromisoformat(estimate) if isinstance(estimate, str) else None
            )
            forecast = self.weather(
                observed.destination_latitude,
                observed.destination_longitude,
                location=observed.destination,
                arrival=arrival,
            )
        else:
            forecast = unavailable(
                "weather_hourly",
                "MET Norway (api.met.no), CC BY 4.0",
                now,
                "destination_unavailable",
            )
        return {
            "flight_status": ref,
            "weather_hourly": forecast,
            "arrival_timeline": arrival_timeline(
                ref,
                transfer_minutes=transfer_minutes,
                meeting_time=meeting_time,
                fetched_at=now,
            ),
            "market_watchlist": self.market(currencies),
        }

    def close(self) -> None:
        self.http.close()
