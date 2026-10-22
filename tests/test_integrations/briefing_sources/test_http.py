"""Offline public-source transport controls; no provider calls in this suite."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from zeo_core.integrations.briefing_sources.http import (
    PublicSourceHTTP,
    SourceUnavailableError,
)

NOW = datetime(2026, 10, 22, 1, tzinfo=UTC)
POSITION_URL = "https://api.adsb.lol/v2/callsign/TEST123"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def mono(self) -> float:
        return self.t

    def now(self) -> datetime:
        return NOW + timedelta(seconds=self.t)

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[PublicSourceHTTP, Clock]:
    clock = Clock()
    return PublicSourceHTTP(
        transport=httpx.MockTransport(handler),
        clock=clock.now,
        monotonic=clock.mono,
        sleep=clock.sleep,
    ), clock


def response_handler(
    response: httpx.Response, calls: list[httpx.Request] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    def reply(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return response

    return reply


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.invalid/v2/callsign/TEST123",
        "https://api.adsb.lol.evil.invalid/v2/callsign/TEST123",
        "https://api.adsb.lol@evil.invalid/v2/callsign/TEST123",
        "https://user:password@api.adsb.lol/v2/callsign/TEST123",
        "https://api.adsb.lol:444/v2/callsign/TEST123",
        "https://127.0.0.1/v2/callsign/TEST123",
        "https://[::1]/v2/callsign/TEST123",
        "http://api.adsb.lol/v2/callsign/TEST123",
        "https://api.adsb.lol/v2/callsign/TEST123#private",
        "https://api.adsb.lol/v2/callsign/TEST123?key=sk-proj-canary",
        "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=nan&lon=0",
    ],
)
def test_guard_precedes_connection(url: str) -> None:
    calls: list[httpx.Request] = []
    http, _ = client(response_handler(httpx.Response(200, json={"ac": []}), calls))
    try:
        with pytest.raises(SourceUnavailableError):
            http.fetch("GET", url)
        assert not calls
        assert http.refused == 1
    finally:
        http.close()


def test_redirect_never_contacts_target() -> None:
    calls: list[httpx.Request] = []
    http, _ = client(
        response_handler(
            httpx.Response(302, headers={"Location": "https://evil.invalid/"}), calls
        )
    )
    try:
        with pytest.raises(SourceUnavailableError):
            http.fetch("GET", POSITION_URL)
        assert [r.url.host for r in calls] == ["api.adsb.lol"]
        assert http.refused == 1
    finally:
        http.close()


def test_cache_user_agent_and_no_personal_fields() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "now": 1,
                "ac": [
                    {
                        "flight": "TEST123",
                        "lat": 1,
                        "registered_owner": "PRIVATE PERSON",
                        "passengers": ["CANARY"],
                        "note": "sk-proj-private",
                    }
                ],
            },
        )

    http, clock = client(handler)
    try:
        a = http.fetch("GET", POSITION_URL)
        clock.t = 59
        b = http.fetch("GET", POSITION_URL)
        assert a == b and len(calls) == 1
        assert calls[0].headers["User-Agent"].startswith("RasaCommunityFlightBriefing/")
        assert "PRIVATE" not in repr(a.data) and "sk-proj-" not in repr(a.data)
        assert a.data["ac"] == [{"flight": "TEST123", "lat": 1}]
    finally:
        http.close()


def test_expired_cache_failure_never_returns_stale() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"now": 1, "ac": []})
        raise httpx.ConnectError("synthetic failure")

    http, clock = client(handler)
    try:
        http.fetch("GET", POSITION_URL)
        clock.t = 61
        with pytest.raises(SourceUnavailableError):
            http.fetch("GET", POSITION_URL)
        assert len(calls) == 2
        assert not http._cache
    finally:
        http.close()


def test_retry_after_respected_without_retry() -> None:
    calls: list[httpx.Request] = []
    http, clock = client(
        response_handler(httpx.Response(429, headers={"Retry-After": "120"}), calls)
    )
    try:
        for t in (0, 61, 119):
            clock.t = t
            with pytest.raises(SourceUnavailableError):
                http.fetch("GET", POSITION_URL)
        assert len(calls) == 1
        clock.t = 121
        with pytest.raises(SourceUnavailableError):
            http.fetch("GET", POSITION_URL)
        assert len(calls) == 2
    finally:
        http.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, text="not JSON"),
        httpx.Response(200, content=b"x" * (1048576 + 1)),
    ],
)
def test_bad_response_is_value_free(response: httpx.Response) -> None:
    http, _ = client(response_handler(response))
    try:
        with pytest.raises(SourceUnavailableError) as caught:
            http.fetch("GET", POSITION_URL)
        assert " " in str(caught.value) and "://" not in str(caught.value)
        assert "not JSON" not in str(caught.value)
    finally:
        http.close()


def test_cache_data_cannot_be_poisoned_by_a_caller() -> None:
    http, _ = client(lambda req: httpx.Response(200, json={"now": 1, "ac": []}))
    try:
        first = http.fetch("GET", POSITION_URL)
        first.data["now"] = "untrusted prose"
        second = http.fetch("GET", POSITION_URL)
        assert second.data["now"] == 1
        second.data["now"] = "other prose"
        assert http.fetch("GET", POSITION_URL).data["now"] == 1
    finally:
        http.close()


def test_budget_and_provider_expiry_are_enforced() -> None:
    calls: list[httpx.Request] = []
    met = {"properties": {"meta": {"updated_at": NOW.isoformat()}, "timeseries": []}}
    http, clock = client(
        response_handler(
            httpx.Response(
                200, json=met, headers={"Expires": "Thu, 22 Oct 2026 03:00:00 GMT"}
            ),
            calls,
        )
    )
    try:
        url = "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=0&lon=0"
        http.fetch("GET", url)
        clock.t = 100
        http.fetch("GET", url)
        assert len(calls) == 1
        clock.t = 8000
        http.requests["api.met.no"] = 20
        with pytest.raises(SourceUnavailableError):
            http.fetch("GET", url)
        assert len(calls) == 1
    finally:
        http.close()
