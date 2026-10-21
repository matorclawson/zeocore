"""
The widget-routing contract of a Mantle briefing agent (masterclass SOW-02).

Built to the locked rasa-ops specification (SPEC-brief-me-before-i-fly-v2, schemas
``widget-upsert`` and ``data-ref``). A service picks one of four predefined components
and carries a ``widget.upsert`` event with the answer; the event names a ``data_ref``
whose payload is the component's data. Nothing here generates code, HTML or URLs.

The honesty rules are checked, not left to prose: ``replay`` and ``delayed`` are
separate flags; delayed market data cannot be presented as undelayed; a transfer time
is typed as an assumption; a failed source carries an error and an EMPTY payload, so no
stale value can be shown as current. :class:`WidgetBoard` keeps the routing rules: a
revision at or below the card's current one is ignored, and a result for a superseded
request is refused, so it cannot overwrite the card of the changed destination.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool

COMPONENTS: Final = (
    "flight_status",
    "weather_hourly",
    "arrival_timeline",
    "market_watchlist",
)
Component = Literal[
    "flight_status", "weather_hourly", "arrival_timeline", "market_watchlist"
]

_DATE_TIME: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?"
    r"(Z|[+-][0-9]{2}:[0-9]{2})"
)
_NO_CONTROL: Final = re.compile(r"[^\x00-\x1f\x7f-\x9f<>]*")


def _date_time(value: str) -> str:
    if not _DATE_TIME.fullmatch(value):
        msg = "not an RFC 3339 date-time"
        raise ValueError(msg)
    return value


def _plain(value: str) -> str:
    if not _NO_CONTROL.fullmatch(value):
        msg = "text holds a control character or markup"
        raise ValueError(msg)
    return value


DateTime = Annotated[str, Field(max_length=40), AfterValidator(_date_time)]
Text = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_plain)]
Number = Annotated[float, Field(allow_inf_nan=False)]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Route(_Closed):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, populate_by_name=True
    )
    origin: Text = Field(alias="from")
    destination: Text = Field(alias="to")


class FlightStatusPayload(_Closed):
    flight_number: Text
    route: Route
    scheduled_departure: DateTime
    estimated_departure: DateTime | None
    scheduled_arrival: DateTime
    estimated_arrival: DateTime | None
    terminal: Text | None
    #: The provider's own string. "Delayed" is repeated from it, never computed here.
    reported_status: Text
    source_timestamp: DateTime | None


class WeatherHour(_Closed):
    time: DateTime
    temp_c: Number
    precip_prob: Number
    precip_mm: Number


class WeatherHourlyPayload(_Closed):
    location: Text
    timezone: Text
    valid_from: DateTime
    valid_to: DateTime
    hourly: Annotated[list[WeatherHour], Field(min_length=1, max_length=72)]
    arrival_hour: DateTime | None


class TimelineStep(_Closed):
    label: Text
    time: DateTime


class Stated(_Closed):
    """A number the service states as an assumption, never as measured data."""

    value: Number
    kind: Literal["assumption"]


class ArrivalTimelinePayload(_Closed):
    steps: Annotated[list[TimelineStep], Field(min_length=1, max_length=12)]
    transfer_minutes: Stated
    meeting_time: DateTime
    #: Computed by the service from payload values and shown with its inputs.
    margin_minutes: Number


class Quote(_Closed):
    symbol: Annotated[str, Field(pattern=r"^[A-Z0-9.\-]{1,12}$")]
    price: Number
    change: Number
    change_pct: Number


class SeriesPoint(_Closed):
    time: DateTime
    value: Number


class MarketWatchlistPayload(_Closed):
    symbols: Annotated[list[Quote], Field(min_length=1, max_length=20)]
    market_status: Text
    quote_time: DateTime | None
    #: True when the provider says the quotes are delayed.
    delayed: StrictBool
    series: dict[str, Annotated[list[SeriesPoint], Field(max_length=500)]]


#: The component registry: the payload model each component accepts.
REGISTRY: Final[dict[str, type[_Closed]]] = {
    "flight_status": FlightStatusPayload,
    "weather_hourly": WeatherHourlyPayload,
    "arrival_timeline": ArrivalTimelinePayload,
    "market_watchlist": MarketWatchlistPayload,
}


class Assumption(_Closed):
    label: Text
    value: str | float | int
    kind: Literal["assumption"]


class DataError(_Closed):
    reason: Annotated[str, Field(max_length=200), AfterValidator(_plain)]


class WidgetRefused(ValueError):  # noqa: N818 -- like UiRefused: a refusal, not a fault
    """An event the board will not apply; ``code`` says why and carries no data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DataRef(_Closed):
    """What a ``data_ref`` resolves to: provenance plus the component's payload."""

    id: Annotated[str, Field(min_length=1, max_length=80)]
    source: Annotated[str, Field(min_length=1, max_length=120)]
    fetched_at: DateTime
    #: The provider's own update time, or null when it gives none; never fetched_at.
    as_of: DateTime | None
    replay: StrictBool
    delayed: StrictBool | None = None
    assumptions: list[Assumption] = Field(default_factory=list)
    payload: dict[str, Any]
    error: DataError | None = None

    def check(self, component: str) -> DataRef:
        """Validate the payload against ``component``; return self when it fits."""
        if component not in REGISTRY:
            raise WidgetRefused("unknown_component")
        if self.error is not None:
            # A failed source shows an unavailable state; no stale value is current.
            if self.payload:
                raise WidgetRefused("error_with_payload")
            return self
        try:
            model = REGISTRY[component].model_validate(self.payload)
        except ValueError:
            raise WidgetRefused("payload_does_not_match_component") from None
        if component == "market_watchlist" and (
            # Delayed and replay are distinct; delayed must be said, not guessed.
            self.delayed is None or self.delayed != getattr(model, "delayed", None)
        ):
            raise WidgetRefused("delayed_flag_missing_or_inconsistent")
        if component == "arrival_timeline" and not self.assumptions:
            raise WidgetRefused("transfer_time_not_labelled_as_assumption")
        return self


class WidgetUpsert(_Closed):
    """``widget.upsert``: put the referenced data on one card."""

    type: Literal["widget.upsert"]
    request_id: Annotated[str, Field(min_length=1, max_length=80)]
    widget_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
    component: Component
    data_ref: Annotated[str, Field(min_length=1, max_length=80)]
    revision: Annotated[int, Field(ge=1, le=2**31 - 1)]


class WidgetBoard:
    """
    The cards of one conversation, and the rules that route an event onto them.

    ``begin_request`` starts the request that owns the board and supersedes the one
    before it: an event of a superseded request is refused, so a slow result for the
    old destination cannot overwrite the card of the new one. Within a request a
    revision at or below the card's current one is ignored (``apply`` returns False).
    Cards are kept by ``widget_id``, so an update changes the existing card.
    """

    def __init__(self) -> None:
        self._request_id: str | None = None
        self._cards: dict[str, tuple[int, str, DataRef]] = {}
        self._retired: set[str] = set()

    def begin_request(self, request_id: str) -> None:
        if request_id in self._retired:
            raise WidgetRefused("request_already_superseded")
        if self._request_id is not None and self._request_id != request_id:
            self._retired.add(self._request_id)
            self._cards.clear()
        self._request_id = request_id

    def supersede(self, request_id: str) -> None:
        """Mark a request superseded without starting another: its late results fail."""
        self._retired.add(request_id)
        if request_id == self._request_id:
            self._request_id = None
            self._cards.clear()

    def apply(self, event: WidgetUpsert, data: DataRef) -> bool:
        """Apply an event; False when ignored (a stale revision), raise when refused."""
        if event.request_id != self._request_id:
            raise WidgetRefused("superseded_request")
        if data.id != event.data_ref:
            raise WidgetRefused("data_ref_mismatch")
        data.check(event.component)
        current = self._cards.get(event.widget_id)
        if current is not None:
            if current[1] != event.component:
                raise WidgetRefused("component_changed")
            if event.revision <= current[0]:
                return False
        self._cards[event.widget_id] = (event.revision, event.component, data)
        return True

    def cards(self) -> dict[str, DataRef]:
        return {key: value[2] for key, value in self._cards.items()}
