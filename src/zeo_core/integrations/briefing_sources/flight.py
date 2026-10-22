"""Observed ADS-B position and an explicitly computed arrival estimate."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from zeo_core.contracts.ui.widgets import Assumption, DataRef
from zeo_core.integrations.briefing_sources.http import (
    PublicSourceHTTP,
    SourceUnavailableError,
)
from zeo_core.integrations.briefing_sources.timeline import unavailable

SOURCE = "ADSB.lol contributors, ODbL 1.0; Virtual Radar Server standing-data, CC0"


@dataclass(frozen=True)
class FlightRead:
    reference: DataRef
    destination_latitude: float | None = None
    destination_longitude: float | None = None
    destination: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    ground_speed_knots: float | None = None
    observed_at: datetime | None = None


def number(value: object, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError
    return result


def great_circle_nm(lat: float, lon: float, dest_lat: float, dest_lon: float) -> float:
    a, b, c, d = map(math.radians, (lat, lon, dest_lat, dest_lon))
    hav = (
        math.sin((c - a) / 2) ** 2
        + math.cos(a) * math.cos(c) * math.sin((d - b) / 2) ** 2
    )
    return 3440.065 * 2 * math.asin(math.sqrt(min(1, max(0, hav))))


def _destination(rows: object, callsign: str) -> tuple[str, str, float, float]:
    if not isinstance(rows, list):
        raise ValueError
    matches = [r for r in rows if isinstance(r, dict) and r.get("callsign") == callsign]
    if len(matches) != 1 or matches[0].get("plausible") is not True:
        raise ValueError
    airports = matches[0]["_airports"]
    if len(airports) != 2:
        raise ValueError
    codes = []
    for airport in airports:
        code = airport.get("icao") or airport.get("iata")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z]{3,4}", code):
            raise ValueError
        codes.append(code)
    return (
        codes[0],
        codes[1],
        number(airports[1]["lat"], -90, 90),
        number(airports[1]["lon"], -180, 180),
    )


def flight(
    http: PublicSourceHTTP, callsign: str, *, fetched_at: datetime
) -> FlightRead:
    try:
        if not isinstance(callsign, str) or not re.fullmatch(
            r"[A-Z0-9]{2,10}", callsign
        ):
            raise ValueError
        position = http.fetch("GET", "https://api.adsb.lol/v2/callsign/" + callsign)
        matches = [
            a for a in position.data["ac"] if a.get("flight", "").strip() == callsign
        ]
        if len(matches) != 1:
            raise ValueError
        aircraft = matches[0]
        lat, lon = number(aircraft["lat"], -90, 90), number(aircraft["lon"], -180, 180)
        speed = number(aircraft["gs"], 30, 1000)
        age = number(aircraft["seen_pos"], 0, 120)
        if aircraft.get("alt_baro") == "ground":
            raise ValueError
        epoch = number(position.data["now"], 0, 10**15)
        if epoch > 10**11:
            epoch /= 1000
        observed = datetime.fromtimestamp(epoch, UTC) - timedelta(seconds=age)
        if not -10 <= (position.fetched_at - observed).total_seconds() <= 120:
            raise ValueError
        routes = http.fetch(
            "POST",
            "https://api.adsb.lol/api/0/routeset",
            body={"planes": [{"callsign": callsign, "lat": lat, "lng": lon}]},
        )
        origin, destination, destination_lat, destination_lon = _destination(
            routes.data, callsign
        )
        distance = great_circle_nm(lat, lon, destination_lat, destination_lon)
        estimate = observed + timedelta(hours=distance / speed)
        reference = DataRef(
            id="flight-" + uuid4().hex,
            source=SOURCE,
            fetched_at=position.fetched_at.isoformat(),
            as_of=observed.isoformat(),
            replay=False,
            assumptions=[
                Assumption(
                    label="estimated_arrival_from_live_position",
                    value=(
                        "position + speed + great-circle distance; not an airline time"
                    ),
                    kind="assumption",
                )
            ],
            payload={
                "flight_number": callsign,
                "route": {"from": origin, "to": destination},
                "scheduled_departure": None,
                "estimated_departure": None,
                "scheduled_arrival": None,
                "estimated_arrival": estimate.isoformat(),
                "terminal": None,
                "reported_status": "Not provided by ADS-B source",
                "source_timestamp": observed.isoformat(),
            },
        ).check("flight_status")
        return FlightRead(
            reference,
            destination_lat,
            destination_lon,
            destination,
            lat,
            lon,
            speed,
            observed,
        )
    except SourceUnavailableError as exc:
        return FlightRead(unavailable("flight_status", SOURCE, fetched_at, exc.code))
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError):
        return FlightRead(
            unavailable(
                "flight_status",
                SOURCE,
                fetched_at,
                "flight_position_or_route_unavailable",
            )
        )
