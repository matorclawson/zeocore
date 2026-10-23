"""One request at a time PER HOST; different hosts run together (a latency fix)."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable

import httpx
import pytest

from zeo_core.integrations.briefing_sources.http import (
    PublicSourceHTTP,
    SourceUnavailableError,
)

ADSB = "https://api.adsb.lol/v2/callsign/TEST123"
ECB = (
    "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
    "?lastNObservations=2&format=csvdata"
)
CSV = "FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n"


def _reply(request: httpx.Request) -> httpx.Response:
    if "ecb" in request.url.host:
        return httpx.Response(200, text=CSV)
    return httpx.Response(200, json={"now": 0, "ac": []})


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    sleep: Callable[[float], None] = lambda seconds: None,
) -> PublicSourceHTTP:
    return PublicSourceHTTP(transport=httpx.MockTransport(handler), sleep=sleep)


def _run(*calls: Callable[[], object]) -> list[BaseException | None]:
    errors: list[BaseException | None] = [None] * len(calls)

    def worker(index: int, call: Callable[[], object]) -> None:
        try:
            call()
        except BaseException as error:  # noqa: BLE001 -- reported to the test
            errors[index] = error

    threads = [
        threading.Thread(target=worker, args=(i, c), daemon=True)
        for i, c in enumerate(calls)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads), "a request hung"
    return errors


def test_two_different_hosts_are_in_flight_together() -> None:
    barrier = threading.Barrier(2, timeout=3)  # both must be inside at once

    def handler(request: httpx.Request) -> httpx.Response:
        barrier.wait()
        return _reply(request)

    http = _client(handler)
    errors = _run(lambda: http.fetch("GET", ADSB), lambda: http.fetch("GET", ECB))
    http.close()
    assert errors == [None, None]  # under one global lock the barrier breaks


def test_one_host_never_has_two_requests_in_flight() -> None:
    state = {"now": 0, "most": 0}
    guard = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with guard:
            state["now"] += 1
            state["most"] = max(state["most"], state["now"])
        time.sleep(0.05)
        with guard:
            state["now"] -= 1
        return _reply(request)

    http = _client(handler)
    urls = [f"https://api.adsb.lol/v2/callsign/TEST{n}" for n in range(1, 6)]
    errors = _run(*[lambda u=u: http.fetch("GET", u) for u in urls])
    http.close()
    assert errors == [None] * 5 and state["most"] == 1  # politeness is per host


def test_a_pacing_sleep_on_one_host_does_not_hold_up_another() -> None:
    release = threading.Event()
    entered = threading.Event()

    def sleep(seconds: float) -> None:
        if seconds > 0:
            entered.set()
            release.wait(timeout=5)  # the first host is pacing itself

    def handler(request: httpx.Request) -> httpx.Response:
        return _reply(request)

    http = _client(handler, sleep)
    http.fetch("GET", "https://api.adsb.lol/v2/callsign/FIRST1")  # first call: no wait
    second_done = threading.Event()

    def second() -> None:
        http.fetch("GET", "https://api.adsb.lol/v2/callsign/SECOND")  # must pace
        second_done.set()

    waiter = threading.Thread(target=second, daemon=True)
    waiter.start()
    assert entered.wait(timeout=3), "the second same-host call never paced"
    other: list[object] = []
    ecb = threading.Thread(
        target=lambda: other.append(http.fetch("GET", ECB)), daemon=True
    )
    ecb.start()
    ecb.join(timeout=3)
    assert other and not ecb.is_alive()  # the ECB read finished while ADSB paced
    assert not second_done.is_set()
    release.set()
    waiter.join(timeout=5)
    http.close()
    assert second_done.is_set()


def test_the_refused_counter_is_exact_under_many_threads() -> None:
    http = _client(_reply)
    bad = "https://evil.example/v2/callsign/TEST123"

    def refuse() -> None:
        for _ in range(40):
            with pytest.raises(SourceUnavailableError):
                http.fetch("GET", bad)

    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # make a lost update likely if the counter is unguarded
    try:
        errors = _run(*[refuse for _ in range(24)])
    finally:
        sys.setswitchinterval(previous)
    http.close()
    assert errors == [None] * 24 and http.refused == 24 * 40


def test_budget_and_cache_still_hold_per_host_under_threads() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return _reply(request)

    http = _client(handler)
    errors = _run(*[lambda: http.fetch("GET", ADSB) for _ in range(8)])
    http.close()
    assert errors == [None] * 8
    assert calls.count("api.adsb.lol") == 1  # one network call, seven cache hits
    assert http.requests == {"api.adsb.lol": 1}


class _Watched(PublicSourceHTTP):
    """Records any write to ``refused`` made without holding the state lock."""

    unguarded = 0

    @property
    def refused(self) -> int:
        return int(self.__dict__.get("_refused_n", 0))

    @refused.setter
    def refused(self, value: int) -> None:
        lock = self.__dict__.get("_lock")
        if value and lock is not None and not lock._is_owned():  # noqa: SLF001
            type(self).unguarded += 1
        self.__dict__["_refused_n"] = value


def test_the_refused_counter_is_only_written_under_the_state_lock() -> None:
    _Watched.unguarded = 0
    http = _Watched(transport=httpx.MockTransport(_reply))
    for _ in range(3):
        with pytest.raises(SourceUnavailableError):
            http.fetch("GET", "https://evil.example/v2/callsign/TEST123")
    http.close()
    assert http.refused == 3 and _Watched.unguarded == 0
