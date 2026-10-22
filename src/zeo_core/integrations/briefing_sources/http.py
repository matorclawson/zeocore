"""Bounded public reads for the four briefing sources, without credentials."""

from __future__ import annotations

import csv
import io
import json
import re
import time
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from threading import RLock
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

HOSTS = frozenset({"api.met.no", "api.adsb.lol", "data-api.ecb.europa.eu"})
USER_AGENT = "RasaCommunityFlightBriefing/1.0 (+https://rasa.community)"
MAX_BYTES = 1024 * 1024


class SourceUnavailableError(ValueError):
    """A fixed, value-free reason; never contains a URL or provider exception."""

    def __init__(self, code: str) -> None:
        reasons = {
            "source_request_refused": "The public source request was refused",
            "source_rate_limited": "The public source asked us to wait",
            "source_request_budget_exhausted": (
                "The source request limit was reached, wait for the ten minute window"
            ),
            "source_response_too_large": "The public source response was too large",
            "source_unavailable": "The public source did not answer with usable data",
            "arrival_inputs_unavailable": "The arrival inputs are unavailable",
            "weather_data_unavailable": "The hourly forecast is unavailable",
            "reference_rates_unavailable": "The reference rates are unavailable",
            "flight_position_or_route_unavailable": "The flight data are unavailable",
            "destination_unavailable": "The destination is unavailable",
            "route_not_provided": "Route not provided by the source",
        }
        self.code = code if code in reasons else "source_unavailable"
        super().__init__(reasons[self.code])


@dataclass(frozen=True)
class Fetched:
    data: Any
    fetched_at: datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def _project_json(
    host: str, path: str, value: object
) -> dict[str, Any] | list[dict[str, Any]]:
    """Discard unneeded fields before cache custody, notably aircraft owners."""
    if host == "api.adsb.lol" and path.startswith("/v2/"):
        if not isinstance(value, dict) or not isinstance(value.get("ac"), list):
            raise ValueError
        return {
            "now": value.get("now"),
            "partial": path.startswith("/v2/point/")
            and (
                value.get("truncated") is True
                or value.get("partial") is True
                or (
                    isinstance(value.get("total"), int)
                    and value["total"] > len(value["ac"])
                )
            ),
            "ac": [
                {
                    k: a[k]
                    for k in ("flight", "lat", "lon", "gs", "seen_pos", "alt_baro")
                    if k in a
                }
                for a in (
                    value["ac"] if path.startswith("/v2/point/") else value["ac"][:100]
                )
                if isinstance(a, dict)
            ],
        }
    if host == "api.adsb.lol":
        if not isinstance(value, list):
            raise ValueError
        return [
            {
                "callsign": row.get("callsign"),
                "plausible": row.get("plausible"),
                "_airports": [
                    {k: a[k] for k in ("icao", "iata", "lat", "lon") if k in a}
                    for a in row.get("_airports", [])[:8]
                    if isinstance(a, dict)
                ],
            }
            for row in value[:100]
            if isinstance(row, dict)
        ]
    if not isinstance(value, dict):
        raise ValueError
    properties = value["properties"]
    hours = []
    for item in properties["timeseries"][:72]:
        data = item["data"]
        instant = data["instant"]["details"]
        next_hour = data.get("next_1_hours", {}).get("details", {})
        hours.append(
            {
                "time": item["time"],
                "temp_c": instant.get("air_temperature"),
                "precip_mm": next_hour.get("precipitation_amount"),
                "precip_prob": next_hour.get("probability_of_precipitation"),
            }
        )
    return {"updated_at": properties["meta"].get("updated_at"), "hours": hours}


def _point_request(method: str, path: str, body: object, query: str) -> bool:
    point = re.fullmatch(
        r"/v2/point/(-?(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,6})?)/"
        r"(-?(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,6})?)/([1-9][0-9]{0,2})",
        path,
    )
    return bool(
        point is not None
        and method == "GET"
        and body is None
        and not query
        and -90 <= float(point[1]) <= 90
        and -180 <= float(point[2]) <= 180
        and 1 <= int(point[3]) <= 250
    )


class PublicSourceHTTP:
    """A fixed-endpoint client; not an arbitrary URL proxy.

    Cache and rolling ten minute host budgets live for this instance's run.
    Injected transports
    remain behind the same URL guard. No environment proxy or credential read.
    """

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._clock, self._monotonic, self._sleep = clock, monotonic, sleep
        self._client = httpx.Client(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=10,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
        )
        self._lock = RLock()
        self._cache: dict[str, tuple[float, Fetched]] = {}
        self._cooldown: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self.requests: dict[str, int] = {}
        self._request_windows: dict[str, deque[float]] = {}
        self.refused = 0

    def close(self) -> None:
        self._client.close()

    def _guard(
        self, method: str, url: str, body: dict[str, Any] | None
    ) -> tuple[str, str]:
        try:
            u = urlsplit(url)
            host = u.hostname
            if (
                u.scheme != "https"
                or host is None
                or host not in HOSTS
                or u.username is not None
                or u.password is not None
                or u.port not in (None, 443)
                or u.fragment
                or "%" in u.netloc
            ):
                raise ValueError
            query = parse_qs(u.query, keep_blank_values=True)
            allowed = False
            if u.hostname == "api.met.no":
                allowed = (
                    method == "GET"
                    and body is None
                    and u.path == "/weatherapi/locationforecast/2.0/compact"
                    and set(query) == {"lat", "lon"}
                    and all(len(v) == 1 for v in query.values())
                )
                if allowed:
                    lat, lon = float(query["lat"][0]), float(query["lon"][0])
                    allowed = -90 <= lat <= 90 and -180 <= lon <= 180
            elif u.hostname == "api.adsb.lol":
                allowed = (
                    method == "GET"
                    and body is None
                    and not query
                    and bool(re.fullmatch(r"/v2/callsign/[A-Z0-9]{2,10}", u.path))
                )
                allowed = allowed or _point_request(method, u.path, body, u.query)
                if (
                    method == "POST"
                    and u.path == "/api/0/routeset"
                    and not query
                    and body
                ):
                    planes = body.get("planes")
                    if (
                        isinstance(planes, list)
                        and len(planes) == 1
                        and set(body) == {"planes"}
                    ):
                        p = planes[0]
                        allowed = (
                            isinstance(p, dict)
                            and set(p) == {"callsign", "lat", "lng"}
                            and bool(re.fullmatch(r"[A-Z0-9]{2,10}", p["callsign"]))
                            and -90 <= p["lat"] <= 90
                            and -180 <= p["lng"] <= 180
                        )
            elif u.hostname == "data-api.ecb.europa.eu":
                allowed = (
                    method == "GET"
                    and body is None
                    and bool(
                        re.fullmatch(
                            r"/service/data/EXR/D\.[A-Z]{3}(?:\+[A-Z]{3}){0,9}\.EUR\.SP00\.A",
                            u.path,
                        )
                    )
                    and query == {"lastNObservations": ["2"], "format": ["csvdata"]}
                )
            if not allowed:
                raise ValueError
            return host, u.path
        except (ValueError, TypeError, KeyError):
            self.refused += 1
            raise SourceUnavailableError("source_request_refused") from None

    def _rate_limit(
        self, host: str, response: httpx.Response, fetched_at: datetime
    ) -> None:
        retry = response.headers.get("Retry-After", "60")
        try:
            wait = float(retry)
        except ValueError:
            try:
                wait = (parsedate_to_datetime(retry) - fetched_at).total_seconds()
            except (ValueError, TypeError):
                wait = 60
        self._cooldown[host] = self._monotonic() + max(60, wait)
        raise SourceUnavailableError("source_rate_limited")

    @staticmethod
    def _decode(host: str, path: str, chunks: bytearray) -> object:
        if host == "data-api.ecb.europa.eu":
            fields = (
                "FREQ",
                "CURRENCY",
                "CURRENCY_DENOM",
                "EXR_TYPE",
                "EXR_SUFFIX",
                "TIME_PERIOD",
                "OBS_VALUE",
            )
            return [
                {k: row.get(k) for k in fields}
                for row in csv.DictReader(io.StringIO(chunks.decode("utf-8-sig")))
            ][:100]
        return _project_json(host, path, json.loads(chunks))

    def fetch(
        self, method: str, url: str, *, body: dict[str, Any] | None = None
    ) -> Fetched:
        with self._lock:
            return self._fetch(method, url, body=body)

    def _fetch(
        self, method: str, url: str, *, body: dict[str, Any] | None = None
    ) -> Fetched:
        host, path = self._guard(method, url, body)
        key = method + url + json.dumps(body, sort_keys=True, separators=(",", ":"))
        now = self._monotonic()
        if self._cooldown.get(host, 0) > now:
            raise SourceUnavailableError("source_rate_limited")
        cached = self._cache.get(key)
        if cached and cached[0] > now:
            return deepcopy(cached[1])
        self._cache.pop(key, None)  # an expired value is never a failure fallback
        window = self._request_windows.setdefault(host, deque())
        while window and window[0] <= now - 600:
            window.popleft()
        if len(window) >= 20:
            raise SourceUnavailableError("source_request_budget_exhausted")
        self._sleep(max(0, 1 - (now - self._last.get(host, now - 1))))
        self._last[host] = self._monotonic()
        window.append(self._monotonic())
        self.requests[host] = self.requests.get(host, 0) + 1
        return self._remote(host, path, key, method, url, body)

    def _remote(
        self,
        host: str,
        path: str,
        key: str,
        method: str,
        url: str,
        body: dict[str, Any] | None,
    ) -> Fetched:
        deadline = self._monotonic() + 10
        try:
            with self._client.stream(method, url, json=body) as response:
                fetched_at = self._clock()
                if response.status_code == 429:
                    self._rate_limit(host, response, fetched_at)
                created_routeset = (
                    response.status_code == 201
                    and method == "POST"
                    and host == "api.adsb.lol"
                    and path == "/api/0/routeset"
                )
                if response.status_code != 200 and not created_routeset:
                    if 300 <= response.status_code < 400:
                        self.refused += 1
                    raise SourceUnavailableError("source_unavailable")
                chunks = self._bounded_body(response, deadline)
                data = self._decode(host, path, chunks)
                result = Fetched(data, fetched_at)
                ttl = self._ttl(host, response, fetched_at)
                self._cache[key] = (self._monotonic() + ttl, deepcopy(result))
                return result
        except SourceUnavailableError:
            raise
        except (
            httpx.HTTPError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            csv.Error,
            RecursionError,
        ):
            raise SourceUnavailableError("source_unavailable") from None

    def _bounded_body(self, response: httpx.Response, deadline: float) -> bytearray:
        if response.headers.get("Content-Encoding", "identity") != "identity":
            raise SourceUnavailableError("source_unavailable")
        chunks = bytearray()
        for chunk in response.iter_bytes():
            if self._monotonic() >= deadline:
                raise SourceUnavailableError("source_unavailable")
            chunks.extend(chunk)
            if len(chunks) > MAX_BYTES:
                raise SourceUnavailableError("source_response_too_large")
        if self._monotonic() >= deadline:
            raise SourceUnavailableError("source_unavailable")
        return chunks

    @staticmethod
    def _ttl(host: str, response: httpx.Response, fetched_at: datetime) -> float:
        ttl = 60.0
        if host == "api.met.no" and "Expires" in response.headers:
            try:
                expiry = parsedate_to_datetime(response.headers["Expires"])
                ttl = max(ttl, (expiry - fetched_at).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
        return ttl
