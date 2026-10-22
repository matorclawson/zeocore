"""Explicit synthetic ECB vectors; live ECB smoke was transport-unavailable."""

import httpx

from tests.test_integrations.briefing_sources.test_http import (
    NOW,
    client,
    response_handler,
)
from zeo_core.integrations.briefing_sources.market import market

HEADER = "FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n"
SAMPLE = (
    HEADER
    + "D,USD,EUR,SP00,A,2026-10-16,1.08001\nD,USD,EUR,SP00,A,2026-10-19,1.09002\n"
)


def test_daily_date_decimal_change_no_live_clock_or_unknown_row() -> None:
    http, _ = client(lambda req: httpx.Response(200, text=SAMPLE))
    try:
        ref = market(http, ["USD"], fetched_at=NOW)
        assert ref.error is None and ref.delayed is False
        assert ref.payload["quote_date"] == "2026-10-19"
        assert ref.payload["quote_time"] is None
        assert ref.payload["rate_basis"] == "daily_reference"
        assert [item.label for item in ref.assumptions] == [
            "change_computed_from_two_reference_days"
        ]
        assert ref.payload["symbols"][0]["change"] == 0.01001
        assert ref.payload["symbols"][0]["change_pct"] == 0.926843
        assert list(ref.payload["series"]) == ["EURUSD"]
    finally:
        http.close()


def test_missing_previous_value_means_change_unavailable_not_zero() -> None:
    http, _ = client(
        lambda req: httpx.Response(
            200, text=HEADER + "D,USD,EUR,SP00,A,2026-10-19,1.09002\n"
        )
    )
    try:
        ref = market(http, ["USD"], fetched_at=NOW)
        assert ref.error is None
        assert not ref.assumptions
        assert ref.payload["symbols"][0]["change"] is None
        assert ref.payload["symbols"][0]["change_pct"] is None
    finally:
        http.close()


def test_unknown_code_partial_empty_or_bad_rates_do_not_invent_rows() -> None:
    for currencies, text in (
        (["ZZZ"], SAMPLE),
        (["USD", "GBP"], SAMPLE),
        (["USD"], HEADER),
        (["USD"], SAMPLE.replace("1.09002", "NaN")),
    ):
        http, _ = client(response_handler(httpx.Response(200, text=text)))
        try:
            ref = market(http, currencies, fetched_at=NOW)
            assert ref.error is not None and ref.payload == {}
        finally:
            http.close()


def test_half_even_decimal_change_does_not_drift_through_float() -> None:
    sample = (
        HEADER
        + "D,USD,EUR,SP00,A,2026-10-16,1.0\nD,USD,EUR,SP00,A,2026-10-19,1.000000015\n"
    )
    http, _ = client(response_handler(httpx.Response(200, text=sample)))
    try:
        ref = market(http, ["USD"], fetched_at=NOW)
        assert ref.payload["symbols"][0]["change_pct"] == 0.000002
    finally:
        http.close()


def test_tiny_positive_and_negative_changes_never_become_invented_zero() -> None:
    for current, expected in (
        ("1.00000000000015", 0.00000000000015),
        ("0.99999999999985", -0.00000000000015),
    ):
        sample = (
            HEADER
            + "D,USD,EUR,SP00,A,2026-10-16,1.0\nD,USD,EUR,SP00,A,2026-10-19,"
            + current
            + "\n"
        )
        http, _ = client(response_handler(httpx.Response(200, text=sample)))
        try:
            ref = market(http, ["USD"], fetched_at=NOW)
            quote = ref.payload["symbols"][0]
            assert quote["change"] == expected
            assert quote["change_pct"] != 0
            assert (quote["change_pct"] > 0) == (expected > 0)
        finally:
            http.close()


def test_market_computation_declaration_matches_the_owning_renderer_contract() -> None:
    from zeo_core.adapters.widget_html import MARKET_CHANGE_ASSUMPTION as SHARED
    from zeo_core.integrations.briefing_sources.market import MARKET_CHANGE_ASSUMPTION

    assert MARKET_CHANGE_ASSUMPTION == SHARED
