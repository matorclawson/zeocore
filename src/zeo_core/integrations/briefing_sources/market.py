"""ECB daily EUR reference rates; Decimal differences, no invented prior day."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from uuid import uuid4

from zeo_core.contracts.ui.widgets import Assumption, DataRef
from zeo_core.integrations.briefing_sources.http import (
    PublicSourceHTTP,
    SourceUnavailableError,
)
from zeo_core.integrations.briefing_sources.timeline import unavailable

SOURCE = "ECB statistics, freely available, daily EUR reference rates, changes computed"
# Explicit supported currency list; a missing provider row is still unavailable.
CURRENCIES = frozenset(
    {"USD", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "NOK", "SEK", "DKK"}
)
MARKET_CHANGE_ASSUMPTION = "change_computed_from_two_reference_days"
ROUNDING = Decimal("0.000001")


def _reported_delta(value: Decimal) -> float:
    rounded = value.quantize(ROUNDING, rounding=ROUND_HALF_EVEN)
    return float(value if rounded == 0 and value != 0 else rounded)


def _observations(
    rows: object, selected: list[str], fetched_at: datetime
) -> dict[str, dict[date, Decimal]]:
    if not isinstance(rows, list):
        raise ValueError
    series: dict[str, dict[date, Decimal]] = {c: {} for c in selected}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError
        code = row.get("CURRENCY")
        if code not in series:
            continue
        if (
            row.get("FREQ"),
            row.get("CURRENCY_DENOM"),
            row.get("EXR_TYPE"),
            row.get("EXR_SUFFIX"),
        ) != ("D", "EUR", "SP00", "A"):
            raise ValueError
        day = date.fromisoformat(row["TIME_PERIOD"])
        rate = Decimal(row["OBS_VALUE"])
        if (
            not rate.is_finite()
            or rate <= 0
            or day > fetched_at.date()
            or day in series[code]
        ):
            raise ValueError
        series[code][day] = rate
    return series


def market(
    http: PublicSourceHTTP, currencies: Sequence[str], *, fetched_at: datetime
) -> DataRef:
    try:
        selected = sorted(set(currencies))
        if not selected or len(selected) > 10 or not set(selected) <= CURRENCIES:
            raise ValueError
        url = (
            "https://data-api.ecb.europa.eu/service/data/EXR/D."
            + "+".join(selected)
            + ".EUR.SP00.A?lastNObservations=2&format=csvdata"
        )
        response = http.fetch("GET", url)
        series = _observations(response.data, selected, fetched_at)
        # A partial response is unavailable, never a fabricated zero row.
        if any(not values for values in series.values()):
            raise ValueError
        latest_days = {max(values) for values in series.values()}
        if len(latest_days) != 1:
            raise ValueError
        publication = latest_days.pop()
        quotes = []
        points = {}
        for code, values in series.items():
            observed = sorted(values.items())[-2:]
            current = observed[-1][1]
            change = percent = None
            if len(observed) == 2:
                previous = observed[0][1]
                delta = current - previous
                change = _reported_delta(delta)
                percent = _reported_delta(delta / previous * Decimal(100))
            symbol = "EUR" + code
            quotes.append(
                {
                    "symbol": symbol,
                    "price": float(current),
                    "change": change,
                    "change_pct": percent,
                }
            )
            points[symbol] = [
                {"time": day.isoformat(), "value": float(value)}
                for day, value in observed
            ]
        return DataRef(
            id="market-" + uuid4().hex,
            source=SOURCE,
            fetched_at=response.fetched_at.isoformat(),
            as_of=None,
            replay=False,
            delayed=False,
            assumptions=(
                [
                    Assumption(
                        label=MARKET_CHANGE_ASSUMPTION,
                        value="two ECB reference days",
                        kind="assumption",
                    )
                ]
                if any(quote["change"] is not None for quote in quotes)
                else []
            ),
            payload={
                "symbols": quotes,
                "market_status": "Currencies only; market hours not provided",
                "quote_time": None,
                "quote_date": publication.isoformat(),
                "rate_basis": "daily_reference",
                "delayed": False,
                "series": points,
            },
        ).check("market_watchlist")
    except SourceUnavailableError as exc:
        return unavailable("market_watchlist", SOURCE, fetched_at, exc.code)
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        return unavailable(
            "market_watchlist", SOURCE, fetched_at, "reference_rates_unavailable"
        )
