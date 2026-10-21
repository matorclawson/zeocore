"""
The widget-routing contract of a live dashboard (software-factories-masterclass-SOW-02).

A service picks one of a fixed set of components and sends a ``widget.upsert`` event
that names a data reference. The reference resolves to validated data that carries its
provenance. The browser renders predefined components only; nothing here generates
code. Every data model is closed: an unknown key is an error and nothing is coerced.

The honesty labels are typed, not prose: a replayed fixture says ``replay``, delayed
market data says ``delayed`` with its delay, and a transfer time without a transport
source says ``assumption``. :class:`WidgetBoard` keeps the routing rules: a lower
revision never overwrites a newer one, and a result for a superseded request cannot
overwrite the card of the request that replaced it.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from zeo_core.contracts.ui.card import OwnerRef, ShortText, Timestamp

COMPONENTS: Final = (
    "flight_status",
    "weather_hourly",
    "arrival_timeline",
    "market_watchlist",
)
Component = Literal[
    "flight_status", "weather_hourly", "arrival_timeline", "market_watchlist"
]
Basis = Literal["live", "delayed", "replay", "assumption"]

_DECIMAL: Final = re.compile(r"-?(0|[1-9][0-9]{0,15})(\.[0-9]{1,8})?")
_WIDGET_ID: Final = r"^[a-z][a-z0-9_.-]{0,63}$"


def _decimal(value: str) -> str:
    if not _DECIMAL.fullmatch(value):
        msg = "not a decimal string"
        raise ValueError(msg)
    return value


DecimalString = Annotated[str, Field(max_length=26), AfterValidator(_decimal)]
WidgetId = Annotated[str, Field(pattern=_WIDGET_ID)]
RequestId = Annotated[str, Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Provenance(_Closed):
    """Where a value came from, when it was observed, and the label it must carry."""

    source: OwnerRef
    observed_at: Timestamp
    basis: Basis
    timezone: ShortText | None = None
    delay_minutes: Annotated[int, Field(ge=1, le=1440)] | None = None

    @model_validator(mode="after")
    def _delay_goes_with_delayed(self) -> Provenance:
        if (self.basis == "delayed") != (self.delay_minutes is not None):
            msg = "delay_minutes is required for delayed data and only for it"
            raise ValueError(msg)
        return self


class FlightStatusData(_Closed):
    flight: ShortText
    origin: ShortText
    destination: ShortText
    scheduled_at: Timestamp
    estimated_at: Timestamp | None = None
    terminal: ShortText | None = None
    status: ShortText
    provenance: Provenance


class WeatherHour(_Closed):
    at: Timestamp
    temperature_c: DecimalString
    precipitation_percent: Annotated[int, Field(ge=0, le=100)]


class WeatherHourlyData(_Closed):
    location: ShortText
    hours: Annotated[list[WeatherHour], Field(min_length=1, max_length=48)]
    provenance: Provenance


class TimelineStep(_Closed):
    kind: Literal["flight", "transfer", "meeting", "other"]
    label: ShortText
    at: Timestamp
    basis: Basis

    @model_validator(mode="after")
    def _a_transfer_is_never_live(self) -> TimelineStep:
        # No transport source exists in v1: a transfer is the service's own estimate,
        # so it has to say so, whatever the other steps are.
        if self.kind == "transfer" and self.basis != "assumption":
            msg = "a transfer time without a transport source is an assumption"
            raise ValueError(msg)
        return self


class ArrivalTimelineData(_Closed):
    steps: Annotated[list[TimelineStep], Field(min_length=1, max_length=12)]
    provenance: Provenance


class MarketRow(_Closed):
    symbol: Annotated[str, Field(pattern=r"^[A-Z0-9.\-]{1,12}$")]
    price: DecimalString
    change_percent: DecimalString


class MarketWatchlistData(_Closed):
    rows: Annotated[list[MarketRow], Field(min_length=1, max_length=20)]
    market_status: Literal["open", "closed"]
    provenance: Provenance

    @model_validator(mode="after")
    def _never_presented_as_real_time(self) -> MarketWatchlistData:
        if self.provenance.basis == "assumption":
            msg = "market data is live, delayed or replay, never an assumption"
            raise ValueError(msg)
        return self


#: The component registry: the data model each component accepts.
REGISTRY: Final[dict[str, type[_Closed]]] = {
    "flight_status": FlightStatusData,
    "weather_hourly": WeatherHourlyData,
    "arrival_timeline": ArrivalTimelineData,
    "market_watchlist": MarketWatchlistData,
}


class WidgetData(_Closed):
    """What a data reference resolves to: the component, its data, validated."""

    data_ref: OwnerRef
    component: Component
    data: dict[str, Any]

    @model_validator(mode="after")
    def _data_matches_component(self) -> WidgetData:
        REGISTRY[self.component].model_validate(self.data)
        return self


class WidgetUpsert(_Closed):
    """``widget.upsert``: put the referenced data on one card."""

    type: Literal["widget.upsert"]
    request_id: RequestId
    widget_id: WidgetId
    component: Component
    data_ref: OwnerRef
    revision: Annotated[int, Field(ge=1, le=2**31 - 1)]


class WidgetRefused(ValueError):  # noqa: N818 -- like UiRefused: a refusal, not a fault
    """An event the board will not apply; ``code`` says why and carries no data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class WidgetBoard:
    """
    The cards on one dashboard, and the rules that route an event onto them.

    ``begin_request`` starts the request that owns the board; an event of any earlier
    request is refused, so a slow result for the old destination cannot overwrite the
    card of the new one. Within a request, a revision at or below the card's current one
    is refused. Cards are kept by ``widget_id``, so an update changes the existing card.
    """

    def __init__(self) -> None:
        self._request_id: str | None = None
        self._cards: dict[str, tuple[int, WidgetUpsert, WidgetData]] = {}
        self._retired: set[str] = set()

    def begin_request(self, request_id: str) -> None:
        if request_id in self._retired:
            raise WidgetRefused("request_already_superseded")
        if self._request_id is not None:
            self._retired.add(self._request_id)
        self._request_id = request_id
        self._cards.clear()

    def apply(self, event: WidgetUpsert, data: WidgetData) -> None:
        if event.request_id != self._request_id:
            raise WidgetRefused("superseded_request")
        if data.data_ref != event.data_ref:
            raise WidgetRefused("data_ref_mismatch")
        if data.component != event.component:
            raise WidgetRefused("component_mismatch")
        current = self._cards.get(event.widget_id)
        if current is not None:
            if current[1].component != event.component:
                raise WidgetRefused("component_changed")
            if event.revision <= current[0]:
                raise WidgetRefused("stale_revision")
        self._cards[event.widget_id] = (event.revision, event, data)

    def cards(self) -> dict[str, WidgetData]:
        return {key: value[2] for key, value in self._cards.items()}
