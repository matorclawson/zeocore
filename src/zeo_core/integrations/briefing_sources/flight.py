"""Observed ADS-B position and an explicitly computed arrival estimate."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

from zeo_core.contracts.ui.widgets import Assumption, DataRef
from zeo_core.integrations.briefing_sources.http import (
    PublicSourceHTTP,
    SourceUnavailableError,
)
from zeo_core.integrations.briefing_sources.timeline import unavailable

SOURCE = "ADSB.lol contributors, ODbL 1.0, Virtual Radar Server standing-data, CC0"


@dataclass(frozen=True)
class StatedDestination:
    """Traveller-selected airport resolved by the caller's versioned local table."""

    code: str
    latitude: float
    longitude: float

    def validated(self) -> StatedDestination:
        if not isinstance(self.code, str) or not re.fullmatch(r"[A-Z]{3,4}", self.code):
            raise ValueError("invalid_stated_destination")
        number(self.latitude, -90, 90)
        number(self.longitude, -180, 180)
        return self


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
    http: PublicSourceHTTP,
    callsign: str,
    *,
    fetched_at: datetime,
    stated_destination: StatedDestination | None = None,
) -> FlightRead:
    try:
        if stated_destination is not None:
            stated_destination.validated()
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
        return _observed_flight(
            http,
            callsign,
            position.fetched_at,
            aircraft,
            lat,
            lon,
            speed,
            age,
            observed,
            stated_destination,
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


def _status(
    aircraft: dict[str, Any],
    speed: float,
    age: float,
    route_missing: bool,
    stated_destination: StatedDestination | None,
    conflict: bool,
) -> str:
    altitude = aircraft.get("alt_baro")
    altitude_text = "altitude not provided"
    if altitude is not None:
        altitude_text = f"{number(altitude, -2000, 100000):g} ft"
    status = "Route not provided by the source. " if route_missing else ""
    status += f"Live position: {altitude_text}, {speed:g} kt, position {age:g} s old"
    if stated_destination is not None:
        status += "; destination stated by traveller " + stated_destination.code
        if conflict:
            status += "; source route differs"
    return status


def _observed_flight(
    http: PublicSourceHTTP,
    callsign: str,
    fetched_at: datetime,
    aircraft: dict[str, Any],
    lat: float,
    lon: float,
    speed: float,
    age: float,
    observed: datetime,
    stated_destination: StatedDestination | None,
) -> FlightRead:
    origin = destination = None
    destination_lat = destination_lon = None
    try:
        routes = http.fetch(
            "POST",
            "https://api.adsb.lol/api/0/routeset",
            body={"planes": [{"callsign": callsign, "lat": lat, "lng": lon}]},
        )
        origin, destination, destination_lat, destination_lon = _destination(
            routes.data, callsign
        )
    except (
        SourceUnavailableError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        pass  # Position remains a provider observation when no route is supplied.
    route_missing = destination is None
    conflict = (
        stated_destination is not None
        and destination is not None
        and stated_destination.code != destination
    )
    if stated_destination is not None:
        destination = stated_destination.code
        destination_lat, destination_lon = (
            stated_destination.latitude,
            stated_destination.longitude,
        )
    estimate = None
    assumptions = []
    if destination_lat is not None and destination_lon is not None:
        distance = great_circle_nm(lat, lon, destination_lat, destination_lon)
        estimate = observed + timedelta(hours=distance / speed)
        basis = "position + speed + great-circle distance; not an airline time"
        if stated_destination is not None:
            basis += "; destination stated by traveller " + stated_destination.code
        assumptions.append(
            Assumption(
                label="estimated_arrival_from_live_position",
                value=basis,
                kind="assumption",
            )
        )
    status = _status(aircraft, speed, age, route_missing, stated_destination, conflict)
    if stated_destination is not None:
        assumptions.append(
            Assumption(
                label="destination_stated_by_traveller",
                value=stated_destination.code,
                kind="assumption",
            )
        )
    reference = DataRef(
        id="flight-" + uuid4().hex,
        source=SOURCE,
        fetched_at=fetched_at.isoformat(),
        as_of=observed.isoformat(),
        replay=False,
        assumptions=assumptions,
        payload={
            "flight_number": callsign,
            "route": {
                "from": origin or "not provided",
                "to": destination or "not provided",
            },
            "scheduled_departure": None,
            "estimated_departure": None,
            "scheduled_arrival": None,
            "estimated_arrival": estimate.isoformat() if estimate is not None else None,
            "terminal": None,
            "reported_status": status,
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


@dataclass(frozen=True)
class PublishedRoute:
    """One caller-owned, attributed public standing-data row; never model prose."""

    airline: str
    origin: str
    destination: str


@dataclass(frozen=True)
class FlightCandidate:
    callsign: str
    airline: str
    origin: str
    destination: str
    altitude_feet: float
    position_age_seconds: float
    latitude: float
    longitude: float
    ground_speed_knots: float


@dataclass(frozen=True)
class FlightSearch:
    """Discovery is not a fifth widget component or a claimed airline schedule."""

    candidates: tuple[FlightCandidate, ...]
    fetched_at: datetime
    route_date: str
    label: str
    error: str | None = None
    source: str = SOURCE
    partial: bool = False

    @property
    def message(self) -> str:
        if self.error is not None:
            return self.error
        if self.partial:
            return (
                "Partial flight search; some aircraft could not be read. "
                "Search again with the flight number"
            )
        return self.label if self.candidates else "No flights found"


def _route_callsign(value: str) -> str:
    if len(value) > 10:
        raise ValueError
    match = re.fullmatch(r"([A-Z]{3})([0-9][A-Z0-9]*)", value)
    if match is None:
        raise ValueError
    suffix = match[2].lstrip("0")
    if not suffix or not suffix[0].isdigit():
        suffix = "0" + suffix
    if (
        re.fullmatch(r"(?:[0-9]{1,4}|[0-9]{1,3}[A-Z]|[0-9]{1,2}[A-Z]{2})", suffix)
        is None
    ):
        raise ValueError
    return match[1] + suffix


def _search_inputs(
    destination: str,
    latitude: float,
    longitude: float,
    airport_codes: tuple[str, ...],
    routes: dict[str, PublishedRoute],
    route_date: str,
    fetched_at: datetime,
) -> tuple[float, float, dict[str, PublishedRoute]]:
    if (
        not isinstance(destination, str)
        or re.fullmatch(r"[A-Za-z][A-Za-z -]{0,59}", destination) is None
        or fetched_at.tzinfo is None
        or fetched_at.utcoffset() is None
        or not isinstance(route_date, str)
        or date.fromisoformat(route_date).isoformat() != route_date
        or date.fromisoformat(route_date) > fetched_at.date()
        or not isinstance(airport_codes, tuple)
        or not 1 <= len(airport_codes) <= 8
        or any(
            not isinstance(code, str) or re.fullmatch(r"[A-Z]{4}", code) is None
            for code in airport_codes
        )
        or not isinstance(routes, dict)
        or len(routes) > 5000
    ):
        raise ValueError
    lat, lon = number(latitude, -90, 90), number(longitude, -180, 180)
    for callsign, route in routes.items():
        if (
            not isinstance(callsign, str)
            or _route_callsign(callsign) != callsign
            or not isinstance(route, PublishedRoute)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 &.,'()/+-]{0,79}", route.airline)
            is None
            or re.fullmatch(r"[A-Z]{4}", route.origin) is None
            or re.fullmatch(r"[A-Z]{4}", route.destination) is None
            or route.origin == route.destination
        ):
            raise ValueError
    return lat, lon, dict(routes)


def _candidate(
    row: dict[str, Any],
    *,
    epoch: float,
    now: datetime,
    lat: float,
    lon: float,
    routes: dict[str, PublishedRoute],
    airport_codes: tuple[str, ...],
) -> tuple[str, FlightCandidate] | None:
    observed_callsign = row["flight"].strip()
    normalized = _route_callsign(observed_callsign)
    route = routes.get(normalized)
    if route is None or route.destination not in airport_codes:
        return None
    age = number(row["seen_pos"], 0, 120)
    observed_at = datetime.fromtimestamp(epoch, UTC) - timedelta(seconds=age)
    actual_age = max(0.0, number((now - observed_at).total_seconds(), -10, 120))
    altitude = number(row["alt_baro"], 1, 100000)
    speed = number(row["gs"], 30, 1000)
    current_lat = number(row["lat"], -90, 90)
    current_lon = number(row["lon"], -180, 180)
    if great_circle_nm(current_lat, current_lon, lat, lon) > 250:
        return None
    return normalized, FlightCandidate(
        observed_callsign,
        route.airline,
        route.origin,
        route.destination,
        altitude,
        actual_age,
        current_lat,
        current_lon,
        speed,
    )


def _flight_candidates(
    data: object,
    *,
    now: datetime,
    lat: float,
    lon: float,
    routes: dict[str, PublishedRoute],
    airport_codes: tuple[str, ...],
) -> tuple[FlightCandidate, ...]:
    if not isinstance(data, dict) or not isinstance(data.get("ac"), list):
        raise ValueError
    aircraft = data["ac"]
    if not aircraft:
        return ()
    epoch = number(data["now"], 0, 10**15)
    if epoch > 10**11:
        epoch /= 1000
    candidates: dict[str, FlightCandidate] = {}
    duplicates: set[str] = set()
    for row in aircraft:
        try:
            result = _candidate(
                row,
                epoch=epoch,
                now=now,
                lat=lat,
                lon=lon,
                routes=routes,
                airport_codes=airport_codes,
            )
            if result is None:
                continue
            key, value = result
            if key in candidates:
                duplicates.add(key)
            candidates[key] = value
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            OverflowError,
            OSError,
        ):
            continue
    return tuple(
        candidates[key] for key in sorted(candidates) if key not in duplicates
    )[:5]


def find_flights(
    http: PublicSourceHTTP,
    destination: str,
    *,
    latitude: float,
    longitude: float,
    airport_codes: tuple[str, ...],
    routes: dict[str, PublishedRoute],
    route_date: str,
    fetched_at: datetime,
) -> FlightSearch:
    """One cached point GET; match caller-owned dated routes, never infer them."""
    label = "Published route data; position live"
    try:
        lat, lon, checked = _search_inputs(
            destination,
            latitude,
            longitude,
            airport_codes,
            routes,
            route_date,
            fetched_at,
        )
        label = f"headed to {destination} per published route data; position live"
    except (ValueError, TypeError, AttributeError, OverflowError):
        return FlightSearch(
            (),
            fetched_at,
            "not provided",
            label,
            "The flight search inputs are invalid",
        )
    try:
        position = http.fetch(
            "GET", f"https://api.adsb.lol/v2/point/{lat:.6f}/{lon:.6f}/250"
        )
        found = _flight_candidates(
            position.data,
            now=fetched_at,
            lat=lat,
            lon=lon,
            routes=checked,
            airport_codes=airport_codes,
        )
        return FlightSearch(
            found,
            position.fetched_at,
            route_date,
            label,
            partial=position.data.get("partial", False) is True,
        )
    except SourceUnavailableError as exc:
        return FlightSearch(
            (),
            fetched_at,
            route_date,
            label,
            str(exc),
            partial=exc.code == "source_response_too_large",
        )
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError):
        return FlightSearch(
            (),
            fetched_at,
            route_date,
            label,
            "The flight search source data are unavailable",
        )
