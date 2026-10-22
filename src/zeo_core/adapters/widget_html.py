"""
HTML cards for a WidgetBoard snapshot (masterclass SOW-02, zeocore-SOW-43).

``render_snapshot_html(board.snapshot())`` turns the cards of one conversation into a
self-contained HTML fragment: inline CSS and inline SVG only. It is a pure function:
no JavaScript, no external resource, no link, no IO, and the same input gives the
same bytes. The page can be shown anywhere without trusting its data:

* every value is HTML-escaped, and only whitelisted fields are rendered (the payload
  is never dumped); it is re-validated against its component model first, so a payload
  that does not fit renders as an UNAVAILABLE card without any of its values;
* a key-shaped string or anything that looks like a capability (a URL, ``javascript:``)
  is replaced by ``[withheld]`` wherever it would appear;
* the honest labels are always visible: REPLAY, DELAYED, ASSUMPTION, SUPERSEDED, a
  missing provider update time ("provider update time not given"), a missing field
  ("not provided").

Rendering is not authority: it grants nothing and checks nothing the board did not.
"""

from __future__ import annotations

import html
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from zeo_core.contracts.ui.widgets import REGISTRY, CardState

WITHHELD: Final = "[withheld]"
_DERIVED: Final = (
    "COMPUTED from the flight estimate, the stated transfer time and the meeting time"
)
#: The assumption label a service attaches when an estimated arrival was computed from
#: a live position, not reported by a provider: the card then says so.
LIVE_POSITION_ASSUMPTION: Final = "estimated_arrival_from_live_position"
#: The assumption label a service attaches when a market card's change and change % were
#: computed from two reference days, not reported by a provider: the card then says so.
MARKET_CHANGE_ASSUMPTION: Final = "change_computed_from_two_reference_days"
_DERIVATIONS: Final = (LIVE_POSITION_ASSUMPTION, MARKET_CHANGE_ASSUMPTION)

#: Control (Cc), format (Cf: bidi overrides, zero-width) and line/paragraph separator
#: (Zl, Zp) characters: never rendered, whatever field they arrive in.
_INVISIBLE: Final = re.compile(
    "[\u0000-\u001f\u007f-\u009f\u00ad\u0600-\u0605\u061c\u06dd\u070f"
    "\u0890\u0891\u08e2\u180e\u200b-\u200f\u2028-\u202e\u2060-\u2064"
    "\u2066-\u206f\ufeff\ufff9-\ufffb\U000110bd\U000110cd"
    "\U00013430-\U0001343f\U0001bca0-\U0001bca3\U0001d173-\U0001d17a"
    "\U000e0001\U000e0020-\U000e007f]"
)
#: An error reason is shown only when it is plain prose, never an identifier or a URL.
_PROSE: Final = re.compile(r"[A-Za-z][A-Za-z0-9 ,.'()\-]{2,118}[A-Za-z0-9.)]")

_KEY_SHAPE: Final = re.compile(
    r"sk-[A-Za-z0-9_\-]{10,}"
    r"|(?i:[A-Za-z0-9_]*(?:token|secret|passw(?:or)?d|apikey|api_key|authorization"
    r"|credential)[A-Za-z0-9_]*)\s*[=:]\s*\S+"
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/=\-]{10,}"
    r"|gh[pousr]_[A-Za-z0-9]{16,}"
    r"|AKIA[0-9A-Z]{12,}"
    r"|xox[abprs]-[A-Za-z0-9\-]{8,}"
    r"|-----BEGIN"
    r"|[A-Za-z0-9+/_\-]{40,}"
)
_CAPABILITY: Final = re.compile(
    r"[A-Za-z][A-Za-z0-9+.\-]*://|(?<![A-Za-z])(?:javascript|vbscript|data):", re.I
)

_TITLES: Final = {
    "flight_status": "Flight status",
    "weather_hourly": "Arrival weather",
    "arrival_timeline": "Arrival timeline",
    "market_watchlist": "Market watchlist",
}

STYLE: Final = (
    # Rasa brand tokens (design system v0.2.0, as in the deck): taupe page and card,
    # ink text, pink accents. System fallbacks only: no font is fetched.
    ".zc-board{container-type:inline-size;max-width:1440px;margin:0 auto;"
    "font:18px/26px Inter,system-ui,-apple-system,'Segoe UI',sans-serif;"
    "color:#181210;background:#FAF6F0}"
    ".zc-grid{display:grid;grid-template-columns:minmax(0,1fr);gap:16px}"
    "@container (min-width:960px){.zc-grid{grid-template-columns:"
    "repeat(2,minmax(0,1fr));gap:24px}}"
    ".zc-card{display:flex;flex-direction:column;min-width:0;border:1px solid #C8C0B6;"
    "border-radius:16px;padding:20px;background:#FFFBF5;color:#181210}"
    "@container (min-width:600px){.zc-card{padding:28px}}"
    ".zc-card h3{margin:0 0 6px;font:500 26px/30px Newsreader,Georgia,serif}"
    ".zc-big{margin:6px 0 10px;font:500 32px/36px Inter,system-ui,sans-serif}"
    "@container (min-width:600px){.zc-big{font-size:42px;line-height:46px}"
    ".zc-card h3{font-size:28px;line-height:32px}}"
    ".zc-prov{margin:8px 0 0;font-size:13px;line-height:18px;color:#524A43;"
    "overflow-wrap:anywhere}"
    ".zc-attr{margin:auto 0 0;padding-top:20px;font-size:13px;line-height:18px;"
    "color:#524A43;overflow-wrap:anywhere}"
    ".zc-tag{display:inline-block;margin-right:4px;padding:0 8px;border-radius:6px;"
    "font-size:13px;font-weight:700;background:#EDE8E0;color:#181210}"
    ".zc-tag-replay{background:#FDFBE5;color:#7A6F1E}"
    ".zc-tag-delayed{background:#FFEDE4;color:#AA4C1E}"
    ".zc-tag-assumption{background:#EDE8E0;color:#524A43}"
    ".zc-tag-superseded{background:#EDE8E0;color:#524A43}"
    ".zc-tag-provider{background:#C6FEE1;color:#2B5E47}"
    ".zc-tag-computed{background:#EDE8E0;color:#AA1E54}"
    ".zc-tag-derived{background:#EDE8E0;color:#AA1E54}"
    ".zc-fresh{margin:4px 0;font-size:15px;line-height:21px;color:#524A43}"
    ".zc-context{margin:0 0 12px;font-size:15px;line-height:21px;color:#524A43}"
    ".zc-note{margin:4px 0;font-size:15px;line-height:21px;color:#524A43}"
    ".zc-details{margin:6px 0;font-size:13px;line-height:18px;color:#524A43}"
    ".zc-details p{margin:2px 0;overflow-wrap:anywhere}"
    ".zc-title{font:500 28px/32px Newsreader,Georgia,serif;color:#181210}"
    ".zc-unavail{color:#AA4C1E;font-weight:700}"
    ".zc-late{color:#AA4C1E;font-weight:700}.zc-spare{color:#2B5E47;font-weight:700}"
    ".zc-scroll{overflow-x:auto;max-width:100%}"
    ".zc-card table{border-collapse:collapse;width:100%}"
    ".zc-card td,.zc-card th{padding:4px 6px;text-align:right;"
    "border-bottom:1px solid #C8C0B6;font-variant-numeric:tabular-nums}"
    ".zc-card td:first-child,.zc-card th:first-child{text-align:left}"
    ".zc-chart{display:block;width:100%;max-height:300px;min-height:220px;"
    "background:#EDE8E0;border-radius:12px}"
    ".zc-temp{fill:none;stroke:#AA1E54;stroke-width:3}"
    ".zc-precip{fill:#E85A91;opacity:.35}"
    ".zc-arrival{stroke:#181210;stroke-dasharray:4 3}"
    ".zc-axis{stroke:#524A43}.zc-dot{fill:#AA1E54}.zc-spark{fill:none;stroke:#AA1E54}"
    ".zc-svg text{font-size:20px;fill:#181210}"
    ".zc-steps{margin:8px 0;padding-left:22px}.zc-steps li{margin:2px 0}"
)


def _get(value: object, key: str) -> Any:  # noqa: ANN401 -- objects or dicts
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _t(value: object) -> str:
    """Escape a value for HTML text or an attribute; withhold keys and capabilities."""
    text = _INVISIBLE.sub("\ufffd", str(value))
    squashed = _INVISIBLE.sub("", str(value))  # a zero-width char must not hide a key
    if any(
        pattern.search(candidate)
        for pattern in (_KEY_SHAPE, _CAPABILITY)
        for candidate in (text, squashed)
    ):
        return WITHHELD
    # "=" too, so that text can never read as an attribute assignment (onerror=...)
    return html.escape(text, quote=True).replace("=", "&#61;")


#: A provider name: letters, digits, space and . , ( ) - only (no / ? = & @ : % ...).
_NAME: Final = re.compile(r"[A-Za-z0-9 .,()\-]{1,120}")


def _source(value: object) -> str:
    """The attribution: a plain provider name, else withheld (never a URL or a key)."""
    text = "" if value is None else str(value)
    if not text:
        return "unknown"
    return _t(text) if _NAME.fullmatch(text) else WITHHELD


def _num(value: object) -> str:
    """A finite number exactly as given: the shortest decimal that reads back the same.

    No rounding, no exponent: 0.0000004 stays 0.0000004 (never 0) and 1.23456789 stays
    1.23456789. Not finite is "n/a"; zero (also -0.0) is "0".
    """
    number = float(value)  # type: ignore[arg-type]
    if not math.isfinite(number):
        return "n/a"
    if number == 0:
        return "0"
    text = format(Decimal(repr(number)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _pct(value: object) -> str:
    return "not provided" if value is None else f"{_num(value)} %"


def _or(value: object, fallback: str = "not provided") -> str:
    return _t(value) if value not in (None, "") else fallback


def _tag(kind: str, text: str) -> str:
    return f'<span class="zc-tag zc-tag-{kind}">{text}</span>'


def _labels(data: object) -> list[str]:
    return [str(_get(item, "label")) for item in _get(data, "assumptions") or ()]


def _tags(card: CardState, extra: Sequence[str] = ()) -> str:
    data = card.data_ref
    labels = _labels(data)
    replay = _get(data, "replay") is True
    if replay:  # REPLAY takes precedence over everything: it is the one label
        tags = [_tag("replay", "REPLAY")]
    elif str(card.component) == "arrival_timeline":
        # every value of the timeline is derived from other cards' values and the
        # traveller's stated transfer and meeting time: say so, naming the inputs
        tags = [_tag("derived", _DERIVED)]
    else:
        tags = [_tag("provider", "PROVIDER DATA")]
    if any(label not in _DERIVATIONS for label in labels) or ("assumption" in extra):
        tags.append(_tag("assumption", "ASSUMPTION"))
    if card.replaced is True:
        tags.append(_tag("superseded", "SUPERSEDED"))
    return "".join(tags)


def _stamp(value: object, place: Place | None) -> str:
    return _clock(value, place, zone_name=True) if _moment(value) else _or(value)


def _provenance(card: CardState, place: Place | None = None) -> str:
    data = card.data_ref
    fetched, as_of = _get(data, "fetched_at"), _get(data, "as_of")
    when = _stamp(as_of, place) if as_of else "provider update time not given"
    return (
        '<p class="zc-attr">Data: '
        f"<strong>{_source(_get(data, 'source'))}</strong></p>"
        f'<p class="zc-prov">fetched {_stamp(fetched, place)}'
        f" &middot; provider time {when}</p>"
        '<details class="zc-details"><summary>exact times</summary>'
        f"<p>fetched (ISO): {_or(fetched)}</p>"
        f"<p>provider time (ISO): {_or(as_of, 'not given')}</p></details>"
    )


def _article(
    card: CardState,
    body: str,
    extra: Sequence[str] = (),
    fresh: str = "",
    place: Place | None = None,
) -> str:
    component = str(card.component)
    title = _t(_TITLES.get(component, component))
    return (
        f'<article class="zc-card zc-{_t(component)}"'
        f' data-widget="{_t(card.widget_id)}">'
        f"<h3>{title} {_tags(card, extra)}</h3>{fresh}{body}{_provenance(card, place)}"
        "</article>"
    )


def _age(seconds: float) -> str:
    if seconds < 90:  # noqa: PLR2004 -- a minute and a half
        return f"{int(round(seconds))} s"
    return _span(seconds / 60)


def _fresh(card: CardState, payload: Any, place: Place | None = None) -> str:  # noqa: ANN401
    """
    EXACTLY ONE freshness line, none for a REPLAY. First match wins:
    delayed, daily reference, then what is true of the component (a forecast with its
    real issue time; a timeline with the age of the position it came from), else live.
    """
    data = card.data_ref
    if _get(data, "replay") is True:
        return ""
    if _get(data, "delayed") is True or getattr(payload, "delayed", False) is True:
        text = "DELAYED, not real time"
    elif getattr(payload, "rate_basis", None) == "daily_reference":
        text = "daily reference rate, not a live quote"
    elif str(card.component) == "weather_hourly":
        issued = _get(data, "as_of")
        text = (
            f"forecast, issued {_clock(issued, place, zone_name=True)}"
            if _moment(issued) is not None
            else "forecast, issue time not provided"
        )
    elif str(card.component) == "arrival_timeline":
        position, fetched = (
            _moment(_get(data, "as_of")),
            _moment(_get(data, "fetched_at")),
        )
        text = (
            "computed from a live position "
            f"{_age((fetched - position).total_seconds())} old"
            if position is not None and fetched is not None and fetched >= position
            else "computed from a live position (age not provided)"
        )
    else:
        text = "live"
    return f'<p class="zc-fresh"><strong>{text}</strong></p>'


def _unavailable(card: CardState, reason: str = "", place: Place | None = None) -> str:
    body = '<p class="zc-unavail">unavailable</p>'
    return _article(card, body + (f"<p>{reason}</p>" if reason else ""), place=place)


def _reason(error: object) -> str:
    """A failed source's reason, only if it is plain prose (no identifier, URL, key)."""
    text = _INVISIBLE.sub(" ", str(_get(error, "reason") or "")).strip()
    if " " not in text or "_" in text or not _PROSE.fullmatch(text):
        return ""
    if re.search(r"[0-9a-fA-F]{8,}", text) or re.search(
        r"\b[A-Za-z0-9-]+\.[A-Za-z]{2,}\b", text
    ):
        return ""  # an identifier or a host name
    return _t(text)


@dataclass(frozen=True)
class Place:
    """Where the traveller is going: the only authority for local times on a card.

    The class that owns the airport table supplies it; the renderer never guesses a time
    zone from a code or from a payload (a payload's ``timezone`` is the provider's,
    often UTC). Without a Place every time is shown in UTC and labelled so.
    """

    code: str
    name: str
    timezone: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.code, str)
            or re.fullmatch(r"[A-Z]{3,4}", self.code) is None
            or not isinstance(self.name, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .,'()-]{0,39}", self.name) is None
            or not isinstance(self.timezone, str)
            or re.fullmatch(r"[A-Za-z0-9_+/-]{1,40}", self.timezone) is None
        ):
            raise ValueError("invalid_place")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError("invalid_place") from None


def _moment(value: object) -> datetime | None:
    """An aware timestamp, or None (a bare day, a malformed or a naive value)."""
    if not isinstance(value, str) or len(value) < 20:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.utcoffset() is not None else None


def _zone(place: Place | None) -> Any:  # noqa: ANN401
    return ZoneInfo(place.timezone) if place is not None else UTC


def _clock(value: object, place: Place | None, *, zone_name: bool = False) -> str:
    """``14:01`` in the place's zone (``14:01 EDT`` named); UTC without a place."""
    moment = _moment(value)
    if moment is None:
        return _or(value)
    local = moment.astimezone(_zone(place))
    text = local.strftime("%H:%M")
    if zone_name or place is None:
        text += f" {local.tzname()}"
    return text


def _day_clock(value: object, place: Place | None, ref: object) -> str:
    """Like ``_clock`` but with the weekday when the day differs from ``ref``'s day."""
    moment, anchor = _moment(value), _moment(ref)
    if moment is None:
        return _or(value)
    local = moment.astimezone(_zone(place))
    base = _clock(value, place)
    if anchor is not None and local.date() != anchor.astimezone(_zone(place)).date():
        return f"{local.strftime('%a')} {base}"
    return base


def _span(minutes: float) -> str:
    """``2 h 10 min`` / ``45 min`` / ``under a minute`` (whole minutes, rounded)."""
    total = int(round(abs(minutes)))
    if total < 1:
        return "under a minute"
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f"{hours} h {mins} min"
    return f"{hours} h" if hours else f"{mins} min"


def _fixed(value: object, places: int) -> str:
    """Rounded half-even to ``places`` (HTML-safe); a nonzero value never shows as 0."""
    number = float(value)  # type: ignore[arg-type]
    if not math.isfinite(number):
        return "n/a"
    quantum = Decimal(1).scaleb(-places)
    rounded = Decimal(repr(number)).quantize(quantum, rounding=ROUND_HALF_EVEN)
    if rounded == 0 and number != 0:
        return ("-" if number < 0 else "") + "&lt;" + format(quantum, "f")
    return format(abs(rounded) if rounded == 0 else rounded, "f")  # no "-0.00"


def _line(label: str, value: str) -> str:
    return f"<p>{label}: <strong>{value}</strong></p>"


def _flight(
    payload: Any,  # noqa: ANN401
    computed: bool = False,  # noqa: FBT001, FBT002
    inputs: tuple[str, str] = ("", ""),
    place: Place | None = None,
    ref: object = None,
) -> str:
    route = payload.route
    absent = "not provided"
    code = "" if route.destination == absent else route.destination  # the route's own
    estimate = payload.estimated_arrival
    when, now = _moment(estimate), _moment(ref)
    if estimate and when is not None:
        head = f"Landing about {_day_clock(estimate, place, ref)}"
        head += f" at {code}" if code else ""
        if now is not None and when >= now:
            head += f", in about {_span((when - now).total_seconds() / 60)}"
    else:
        head = "No landing estimate"
    if route.origin == absent and route.destination == absent:
        where = ""
    elif route.origin == absent:
        where = f" to {_t(route.destination)}"
    else:
        where = f" {_t(route.origin)} &rarr; {_t(route.destination)}"
    scheduled = (
        ("departure", payload.scheduled_departure),
        ("estimated departure", payload.estimated_departure),
        ("airline arrival", payload.scheduled_arrival),
    )
    given = "".join(
        _line(label, _clock(value, place, zone_name=True))
        for label, value in scheduled
        if value
    ) + (_line("terminal", _t(payload.terminal)) if payload.terminal else "")
    computed_tag = (
        " "
        + _tag(
            "computed",
            "COMPUTED FROM LIVE POSITION, not an airline time "
            f"(inputs: live position from {inputs[0]}, fetched {inputs[1]})",
        )
        if computed and estimate
        else ""
    )
    details = (
        '<details class="zc-details"><summary>details</summary>'
        + _line("estimated arrival (ISO)", _or(estimate))
        + _line("source time (ISO)", _or(payload.source_timestamp))
        + "</details>"
    )
    return (
        f'<p class="zc-big">{_t(head)}</p>'
        f"<p><strong>{_t(payload.flight_number)}</strong>{where}</p>"
        + (
            _line(
                "estimated arrival",
                _clock(estimate, place, zone_name=True) + computed_tag,
            )
            if estimate
            else ""
        )
        + given
        + (
            ""
            if given
            else '<p class="zc-note">airline schedule not provided by this source</p>'
        )
        + _line("status (as reported)", _t(payload.reported_status))
        + details
    )


def _scale(values: Sequence[float]) -> tuple[float, float]:
    low, high = min(values), max(values)
    return (low, high if high > low else low + 1.0)


def _zones(hours: Sequence[Any], place: Place | None) -> str:
    """The zone name(s) of the table's hours, in order (EDT; EDT/EST over a change)."""
    if place is None:
        return "UTC"
    names: list[str] = []
    for hour in hours:
        moment = _moment(hour.time)
        name = moment.astimezone(_zone(place)).tzname() if moment is not None else None
        if name and name not in names:
            names.append(name)
    return "/".join(names) or "UTC"


def _weather(payload: Any, place: Place | None = None) -> str:  # noqa: ANN401
    hours = payload.hourly
    # Drawn at about its rendered size (a 360-unit-wide box in a 280 to 360 px card), so
    # its 20-unit labels are 16 px or more on screen instead of a scaled-down 5 px.
    width, height, left, top, bottom = 360.0, 300.0, 56.0, 16.0, 52.0
    plot_h = height - top - bottom
    step = (width - left - 10) / max(len(hours), 1)
    low, high = _scale([h.temp_c for h in hours])
    points, bars, marker, ticks, dots = [], [], "", [], []
    every = max(1, -(-len(hours) // 5))  # at most five hour labels
    previous_day = None
    for index, hour in enumerate(hours):
        x = left + step * (index + 0.5)
        y = top + plot_h * (1 - (hour.temp_c - low) / (high - low))
        points.append(f"{x:.1f},{y:.1f}")
        dots.append(f'<circle class="zc-dot" cx="{x:.1f}" cy="{y:.1f}" r="4"></circle>')
        moment = _moment(hour.time)
        local_day = moment.astimezone(_zone(place)).date() if moment else None
        if index % every == 0:
            label = f"{_clock(hour.time, place)[:2]}h"
            if local_day is not None and local_day != previous_day and index:
                label += f" {local_day.strftime('%a')}"  # a new local day: say which
            ticks.append(
                f'<text x="{x:.1f}" y="{height - 10:.0f}" text-anchor="middle">'
                f"{label}</text>"
            )
        previous_day = local_day
        if hour.precip_prob is not None:  # no probability: no bar, never a 0 % bar
            prob = min(max(float(hour.precip_prob), 0.0), 100.0)
            bar_h = plot_h * prob / 100.0
            bars.append(
                f'<rect class="zc-precip" x="{x - step * 0.3:.1f}"'
                f' y="{top + plot_h - bar_h:.1f}"'
                f' width="{step * 0.6:.1f}" height="{bar_h:.1f}"></rect>'
            )
        if hour.time == payload.arrival_hour:
            marker = (
                f'<line class="zc-arrival" x1="{x:.1f}" y1="{top}" x2="{x:.1f}"'
                f' y2="{top + plot_h:.1f}"></line>'
            )
    base = f"{top + plot_h:.1f}"
    axis = (
        f'<line class="zc-axis" x1="{left}" y1="{base}" x2="{width - 10}"'
        f' y2="{base}"></line>'
    )
    labels = (
        "".join(ticks)
        + f'<text x="2" y="{top + 16}">{_fixed(high, 0)}&deg;C</text>'
        + f'<text x="2" y="{top + plot_h:.1f}">{_fixed(low, 0)}&deg;C</text>'
    )
    chart = (
        f'<svg class="zc-svg zc-chart" viewBox="0 0 {width:.0f} {height:.0f}"'
        ' width="100%" role="img"'
        ' aria-label="Hourly temperature and precipitation probability">'
        "<title>Hourly temperature and precipitation probability</title>"
        f"{axis}{''.join(bars)}"
        f'<polyline class="zc-temp" points="{" ".join(points)}"></polyline>'
        f"{''.join(dots)}{marker}{labels}</svg>"
    )
    chosen = next((h for h in hours if h.time == payload.arrival_hour), None)
    row, at = (chosen, "at landing") if chosen is not None else (hours[0], "next hour")
    rain = "no rain" if row.precip_mm == 0 else f"{_fixed(row.precip_mm, 1)} mm rain"
    head = f"{_fixed(row.temp_c, 0)} &deg;C, {rain} {at}"
    zones = _zones(hours, place)
    zone_text = zones if "/" not in zones else f"local time ({zones})"
    legend = (
        f'<p class="zc-note">Dashed line: landing about {_clock(row.time, place)}</p>'
        if chosen is not None
        else ""
    )
    show_prob = any(h.precip_prob is not None for h in hours)
    rows = "".join(
        f"<tr><td>{_clock(h.time, place)}</td><td>{_fixed(h.temp_c, 1)} &deg;C</td>"
        + (f"<td>{_pct(h.precip_prob)}</td>" if show_prob else "")
        + f"<td>{_fixed(h.precip_mm, 1)} mm</td></tr>"
        for h in hours
    )
    name = _t(place.name) if place is not None else _t(payload.location)
    return (
        f'<p class="zc-big">{head}</p>'
        f"<p>{name} &middot; times in {_t(zone_text)}</p>{chart}{legend}"
        + (
            ""
            if show_prob
            else '<p class="zc-note">rain probability not provided by this source</p>'
        )
        + '<div class="zc-scroll"><table><tr><th>time</th><th>temp</th>'
        + ("<th>rain</th>" if show_prob else "")
        + f"<th>precip</th></tr>{rows}</table></div>"
        + '<details class="zc-details"><summary>details</summary>'
        + _line("forecast from (ISO)", _t(payload.valid_from))
        + _line("forecast to (ISO)", _t(payload.valid_to))
        + "</details>"
    )


def _timeline(payload: Any, place: Place | None = None) -> str:  # noqa: ANN401
    stops = [(s.label, s.time) for s in payload.steps] + [
        ("Meeting", payload.meeting_time)
    ]
    stops.sort(key=lambda stop: _moment(stop[1]) or datetime.max.replace(tzinfo=UTC))
    items = "".join(
        f"<li><strong>{_day_clock(when, place, payload.meeting_time)}</strong> "
        f"{_t(label)}</li>"
        for label, when in stops
    )
    margin = float(payload.margin_minutes)
    transfer = f"(assuming a {_num(payload.transfer_minutes.value)} min transfer)"
    if margin < 0:
        head = f"You will miss it: {_span(margin)} late {transfer}"
        verdict = f'<span class="zc-late">{_num(-margin)} min late</span>'
    else:
        head = f"You will make it: {_span(margin)} spare {transfer}"
        verdict = f'<span class="zc-spare">{_num(margin)} min spare</span>'
    zone = "UTC" if place is None else _t(place.name)
    return (
        f'<p class="zc-big">{_t(head)}</p>'
        f'<ol class="zc-steps" aria-label="Timeline, in time order">{items}</ol>'
        + f"<p>Transfer: <strong>{_num(payload.transfer_minutes.value)} min</strong> "
        + f"{_tag('assumption', 'ASSUMPTION')} (not live traffic)</p>"
        + f"<p>Meeting at {_clock(payload.meeting_time, place, zone_name=True)}"
        + f" ({zone} time): {verdict}</p>"
        + '<details class="zc-details"><summary>details</summary>'
        + _line("meeting (ISO)", _t(payload.meeting_time))
        + "</details>"
    )


def _spark(points: Sequence[Any]) -> str:
    if len(points) < 2:
        return ""
    values = [float(p.value) for p in points]
    low, high = _scale(values)
    step = 100.0 / (len(values) - 1)
    coords = " ".join(
        f"{step * i:.1f},{22 - 20 * (v - low) / (high - low):.1f}"
        for i, v in enumerate(values)
    )
    return (
        '<svg class="zc-svg" viewBox="0 0 100 24" width="100" height="24" role="img"'
        ' aria-label="trend">'
        f'<polyline class="zc-spark" points="{coords}"></polyline></svg>'
    )


def _change(q: Any) -> tuple[str, str]:  # noqa: ANN401
    """One symbol's change; unavailable (never 0) without a previous value."""
    if q.change is None or q.change_pct is None:
        return ("change unavailable", "change unavailable")
    return (_fixed(q.change, 2), f"{_fixed(q.change_pct, 2)} %")


def _computed_change(payload: Any, inputs: tuple[str, str]) -> str:  # noqa: ANN401
    """The value-level label of a change computed from two reference days."""
    days = sorted(
        {
            str(point.time)
            for q in payload.symbols
            if q.change is not None
            for point in payload.series.get(q.symbol, ())
        }
    )
    named = f" {' and '.join(_t(day) for day in days)}" if days else ""
    return (
        '<p class="zc-computed"><strong>change and change % COMPUTED from two '
        f"reference days{named}</strong>, not provider data "
        f"(inputs: reference rates from {inputs[0]}, fetched {inputs[1]})</p>"
    )


def _market(
    payload: Any,  # noqa: ANN401
    computed: bool = False,  # noqa: FBT001, FBT002
    inputs: tuple[str, str] = ("", ""),
) -> str:
    rows = ""
    for q in payload.symbols:
        change, pct = _change(q)
        rows += (
            f"<tr><td>{_t(q.symbol)}</td><td>{_fixed(q.price, 4)}</td>"
            f"<td>{change}</td><td>{pct}</td>"
            f"<td>{_spark(payload.series.get(q.symbol, ()))}</td></tr>"
        )
    first = payload.symbols[0]
    pair = re.fullmatch(r"EUR([A-Z]{3})", str(first.symbol))
    head = (
        f"1 EUR = {_fixed(first.price, 4)} {pair[1]}"
        if pair
        else f"{_t(first.symbol)} {_fixed(first.price, 4)}"
    )
    published = _line("published", _t(payload.quote_date)) if payload.quote_date else ""
    quoted = _line("quote time", _t(payload.quote_time)) if payload.quote_time else ""
    return (
        f'<p class="zc-big">{head}</p>'
        + _line("market", _t(payload.market_status))
        + published
        + quoted
        + (
            _computed_change(payload, inputs)
            if computed and any(q.change is not None for q in payload.symbols)
            else ""
        )
        + '<div class="zc-scroll"><table><tr><th>symbol</th><th>rate</th>'
        + "<th>change</th><th>change %</th><th></th></tr>"
        + f"{rows}</table></div>"
    )


_BODIES: Final = {
    "flight_status": _flight,
    "weather_hourly": _weather,
    "arrival_timeline": _timeline,
    "market_watchlist": _market,
}


def render_card_html(card: CardState, *, place: Place | None = None) -> str:
    """One card as an ``<article>``; an invalid reference renders as unavailable."""
    if not isinstance(card, CardState):  # model text or a raw dict is never rendered
        return (
            '<article class="zc-card zc-unavailable"><h3>Card</h3>'
            '<p class="zc-unavail">unavailable</p></article>'
        )
    data = card.data_ref
    error = _get(data, "error")
    if error is not None:
        return _unavailable(card, _reason(error), place)
    component = str(card.component)
    model = (
        REGISTRY.get(component) if isinstance(REGISTRY.get(component), type) else None
    )
    if model is None or component not in _BODIES:
        return _unavailable(card, place=place)
    try:
        data.check(component)  # the board's own rules, also for a card built directly
        payload = model.model_validate(_get(data, "payload"))
    except (ValidationError, TypeError, ValueError, AttributeError):
        return _unavailable(card, place=place)  # WidgetRefused is a ValueError
    fresh = _fresh(card, payload, place)
    ref = _get(data, "fetched_at")
    if component == "flight_status":
        # a REPLAY never claims a live computation
        computed = _get(
            data, "replay"
        ) is not True and LIVE_POSITION_ASSUMPTION in _labels(data)
        inputs = (
            _source(_get(data, "source")),
            _stamp(_get(data, "fetched_at"), place),
        )
        return _article(
            card, _flight(payload, computed, inputs, place, ref), (), fresh, place
        )
    if component == "market_watchlist":
        computed = _get(
            data, "replay"
        ) is not True and MARKET_CHANGE_ASSUMPTION in _labels(data)
        inputs = (
            _source(_get(data, "source")),
            _stamp(_get(data, "fetched_at"), place),
        )
        return _article(card, _market(payload, computed, inputs), (), fresh, place)
    if component == "weather_hourly":
        return _article(card, _weather(payload, place), (), fresh, place)
    if component == "arrival_timeline":
        return _article(card, _timeline(payload, place), (), fresh, place)
    return _unavailable(card)  # not reachable: every registered component is handled


def _context(place: Place | None) -> str:
    """The board's one destination line: whose clock the times are on."""
    text = (
        f"Times are in {_t(place.name)} ({_t(place.code)}) local time"
        if place is not None
        else "Destination local time unavailable; times are in UTC"
    )
    return f'<p class="zc-context">{text}</p>'


def render_snapshot_html(
    cards: Sequence[CardState],
    *,
    document: bool = False,
    title: str = "Briefing",
    place: Place | None = None,
) -> str:
    """
    The cards of a ``WidgetBoard.snapshot()`` as HTML, in snapshot order.

    ``document=False`` returns a fragment (one ``<section class="zc-board">`` with its
    own ``<style>``); ``document=True`` wraps it in a full page whose content-security
    policy allows nothing but inline style. ``place`` (a :class:`Place`) is where the
    traveller is going: with it every time is in that zone, without it in UTC.
    """
    body = "".join(render_card_html(card, place=place) for card in cards) or (
        "<p>no cards yet</p>"
    )
    section = (
        f'<section class="zc-board"><style>{STYLE}</style>'
        f"{_context(place)}"
        f'<div class="zc-grid">{body}</div></section>'
    )
    if not document:
        return section
    heading = f'<h1 class="zc-title">{_t(title)}</h1>'
    policy = "default-src 'none'; style-src 'unsafe-inline'"
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy"'
        f' content="{html.escape(policy, quote=False)}">'
        f"<title>{_t(title)}</title></head><body>{heading}{section}</body></html>"
    )
