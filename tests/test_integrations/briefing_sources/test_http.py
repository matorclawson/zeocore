"""Offline public-source transport controls; no provider calls in this suite."""

from collections.abc import Callable, Iterator
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
        http.fetch("GET", url)
        assert len(calls) == 2
    finally:
        http.close()


@pytest.mark.parametrize(
    "body,host,path",
    [
        ("[" * 50000 + "]" * 50000, "api.adsb.lol", "/v2/callsign/TEST123"),
        (
            "CURRENCY,OBS_VALUE\nUSD," + "9" * 140000 + "\n",
            "data-api.ecb.europa.eu",
            "/service/data/EXR/D.USD.EUR.SP00.A?lastNObservations=2&format=csvdata",
        ),
    ],
)
def test_decoder_limit_errors_are_value_free_source_unavailable(
    body: str, host: str, path: str
) -> None:
    http, _ = client(lambda request: httpx.Response(200, text=body))
    try:
        try:
            http.fetch("GET", "https://" + host + path)
        except Exception as error:
            assert isinstance(error, SourceUnavailableError)
            assert error.code == "source_unavailable"
            assert body not in str(error)
        else:
            pytest.fail("Malformed source must be unavailable")
    finally:
        http.close()


def test_dripping_stream_stops_at_monotonic_deadline_without_buffering_64k() -> None:
    clock = Clock()
    observed: list[float] = []

    class Drip(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            for _ in range(100):
                clock.t += 6
                observed.append(clock.t)
                yield b" "

    http = PublicSourceHTTP(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=Drip())),
        clock=clock.now,
        monotonic=clock.mono,
        sleep=clock.sleep,
    )
    try:
        with pytest.raises(SourceUnavailableError) as error:
            http.fetch("GET", POSITION_URL)
        assert error.value.code == "source_unavailable"
        assert observed == [6, 12]
        assert not http._cache
    finally:
        http.close()


def test_twenty_five_spaced_briefings_keep_working_with_shared_budget() -> None:
    calls: list[httpx.Request] = []
    http, clock = client(response_handler(httpx.Response(200, json={"ac": []}), calls))
    try:
        for number in range(25):
            clock.t = number * 61.0
            # Two ADS-B reads per briefing, all unique so cache cannot hide exhaustion.
            for suffix in ("A", "B"):
                try:
                    http.fetch(
                        "GET",
                        "https://api.adsb.lol/v2/callsign/TEST" + str(number) + suffix,
                    )
                except SourceUnavailableError as error:
                    raise AssertionError(
                        "Spaced briefings exhausted a lifetime budget"
                    ) from error
        assert len(calls) == 50
    finally:
        http.close()


def test_rolling_window_burst_refused_then_recovers_without_a_new_client() -> None:
    calls: list[httpx.Request] = []
    http, clock = client(response_handler(httpx.Response(200, json={"ac": []}), calls))
    try:
        for number in range(20):
            http.fetch("GET", "https://api.adsb.lol/v2/callsign/TEST" + str(number))
        with pytest.raises(SourceUnavailableError) as caught:
            http.fetch("GET", "https://api.adsb.lol/v2/callsign/TEST20")
        assert caught.value.code == "source_request_budget_exhausted"
        assert "ten minute window" in str(caught.value)
        assert len(calls) == 20
        clock.t = 601
        http.fetch("GET", "https://api.adsb.lol/v2/callsign/TEST20")
        assert len(calls) == 21
    finally:
        http.close()


@pytest.mark.parametrize(
    "method,status",
    [
        ("GET", 201),
        ("GET", 202),
        ("GET", 204),
        ("POST", 202),
        ("POST", 204),
        ("POST", 302),
    ],
)
def test_created_is_only_allowed_for_routeset_post(method: str, status: int) -> None:
    calls: list[httpx.Request] = []
    payload = {"now": NOW.timestamp() * 1000, "ac": []} if method == "GET" else []
    http, _ = client(response_handler(httpx.Response(status, json=payload), calls))
    try:
        url = POSITION_URL if method == "GET" else "https://api.adsb.lol/api/0/routeset"
        body = (
            None
            if method == "GET"
            else {"planes": [{"callsign": "TEST123", "lat": 50.0, "lng": 0.0}]}
        )
        with pytest.raises(SourceUnavailableError):
            http.fetch(method, url, body=body)
        assert len(calls) == 1
        assert not http._cache
    finally:
        http.close()


@pytest.mark.parametrize(
    "suffix",
    [
        "91/0/250",
        "0/-181/250",
        "nan/0/10",
        "0/inf/10",
        "0/0/251",
        "0/0/0",
        "0/0/001",
        "0/0/2.5",
        "0/0/250?key=CANARY",
        "0/0/250?ignored",
        "0/0/250/extra",
        "0/%2e%2e/250",
        "0/0/250#private",
    ],
)
def test_point_guard_refuses_before_connection(suffix: str) -> None:
    calls: list[httpx.Request] = []
    http, _ = client(response_handler(httpx.Response(200, json={"ac": []}), calls))
    try:
        with pytest.raises(SourceUnavailableError):
            http.fetch("GET", "https://api.adsb.lol/v2/point/" + suffix)
        assert calls == []
    finally:
        http.close()


def test_point_get_is_cached_projected_and_bounded() -> None:
    calls: list[httpx.Request] = []
    row = {
        "flight": "TEST123",
        "lat": 40.7,
        "lon": -73.8,
        "gs": 350,
        "seen_pos": 2,
        "alt_baro": 25000,
        "r": "PRIVATE REGISTRATION",
        "registered_owner": "PRIVATE OWNER",
    }
    http, clock = client(
        response_handler(
            httpx.Response(200, json={"now": NOW.timestamp(), "ac": [row] * 101}), calls
        )
    )
    try:
        url = "https://api.adsb.lol/v2/point/40.7/-73.8/250"
        first = http.fetch("GET", url)
        clock.t = 59
        assert http.fetch("GET", url) == first
        assert len(calls) == 1
        assert len(first.data["ac"]) == 101
        assert set(first.data["ac"][0]) == {
            "flight",
            "lat",
            "lon",
            "gs",
            "seen_pos",
            "alt_baro",
        }
        assert "PRIVATE" not in repr(first)
        with pytest.raises(SourceUnavailableError):
            http.fetch("POST", url, body={"planes": []})
        assert len(calls) == 1
    finally:
        http.close()


def test_point_redirect_and_malformed_aircraft_never_become_empty_success() -> None:
    for response in (
        httpx.Response(302, headers={"Location": "https://evil.invalid/"}),
        httpx.Response(200, json={"ac": "not an aircraft array"}),
    ):
        calls: list[httpx.Request] = []
        http, _ = client(response_handler(response, calls))
        try:
            with pytest.raises(SourceUnavailableError):
                http.fetch("GET", "https://api.adsb.lol/v2/point/40.7/-73.8/250")
            assert len(calls) == 1
            assert not http._cache
        finally:
            http.close()
