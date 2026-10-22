# Briefing cards: flight, weather, arrival timeline and currency watchlist

This guide shows how to build a small live briefing from keyless public
sources and show it as four cards. It uses three parts of ZeoCore:

| Part | Module | What it does |
|---|---|---|
| Sources | `zeo_core.integrations.briefing_sources` | Reads the public APIs and returns one `DataRef` per card. |
| Contract | `zeo_core.contracts.ui` | Checks every `DataRef` and routes it onto a `WidgetBoard`. |
| Renderer | `zeo_core.adapters.widget_html` | Turns the board's cards into self-contained HTML. |

Nothing here needs an API key. The sources are
[MET Norway](https://api.met.no) (weather), [ADSB.lol](https://api.adsb.lol)
(live aircraft position) and the [European Central Bank](https://data-api.ecb.europa.eu)
(daily euro reference rates). Respect their terms: the client identifies itself,
caches each request for at least 60 seconds and keeps to a small per-host
request budget.

## The four cards

| Card | Source | What it shows |
|---|---|---|
| `flight_status` | ADSB.lol | The aircraft's live position and an arrival **estimate computed from it**. It is not an airline time and the card says so. |
| `weather_hourly` | MET Norway | An hourly forecast at the destination. |
| `arrival_timeline` | computed | The estimated landing, a transfer time you state, and the margin to your meeting. |
| `market_watchlist` | ECB | Daily reference rates against the euro, with the change computed from the last two published days. |

## Honest by construction

The contract refuses to show what it cannot back up:

- Every card shows **one provenance label** (`PROVIDER DATA`, `COMPUTED` or
  `REPLAY`) and, unless it is a replay, **one freshness line** (`live` for a
  position, `forecast, issued <time>` from the provider's own issue time,
  `computed from a live position <age> old`, `delayed, not real time`, or `daily
  reference rate, not a live quote`).
- A failed source gives an **empty `DataRef` with a short reason**, shown as
  "unavailable". A previous value is never shown as current.
- Missing data stays missing: no schedule, no rain probability and no previous
  rate are ever filled with zero.
- A value computed by ZeoCore says so and names its inputs. The transfer time
  is labelled as your assumption.
- The renderer escapes every field, withholds anything shaped like a key or a
  URL, and emits no JavaScript and no remote resource.

## Build a briefing

```python
from datetime import UTC, datetime, timedelta

from zeo_core.adapters.widget_html import Place, render_snapshot_html
from zeo_core.contracts.ui import WidgetBoard, WidgetUpsert
from zeo_core.integrations.briefing_sources import (
    PublicBriefingSources,
    StatedDestination,
)

sources = PublicBriefingSources()
try:
    refs = sources.read_briefing(
        "DLH409",                                   # an airborne callsign
        transfer_minutes=60,                        # your assumption
        meeting_time=datetime.now(UTC) + timedelta(hours=6),
        currencies=("USD", "GBP"),
        # The public route lookup may return no route. Say where you are going
        # and the weather and the arrival estimate use it, labelled as yours.
        stated_destination=StatedDestination("EDDF", 50.0333, 8.5706),
    )
finally:
    sources.close()

board = WidgetBoard()
board.begin_request("request-1")
for component, ref in refs.items():
    board.apply(
        WidgetUpsert(
            type="widget.upsert",
            request_id="request-1",
            widget_id=component.replace("_", "-"),
            component=component,
            data_ref=ref.id,
            revision=1,
        ),
        ref,
    )

# Where you are going: the only authority for local times on the cards. Without it the
# times are UTC and say so.
place = Place("EDDF", "Frankfurt", "Europe/Berlin")
html = render_snapshot_html(
    board.snapshot(), document=True, title="My briefing", place=place
)
open("briefing.html", "w", encoding="utf-8").write(html)
```

Open `briefing.html` in a browser. With `document=True` the page carries a
content-security-policy that allows nothing but inline style.

If the aircraft is not airborne, the source is down or a response is not what
was expected, the matching card says "unavailable" with a plain reason and the
other cards still render.

## Update a card when the request changes

`WidgetBoard.begin_request(request_id, replaces=[...])` starts a new request.
Cards named in `replaces` are marked **superseded** until the new request
writes them, and a late result from the old request is refused, so an answer
for the previous destination can never overwrite the card for the new one.

## Use the agent route

To let a model decide which source to read, give a managed agent one tool per
reader and build the cards from the `DataRef`s your handlers returned, never
from the model's prose. See [the OpenAI managed Agents API](../integrations/agents-api.md).

## Reference

- `PublicBriefingSources.flight`, `.weather`, `.market`, `.arrival_timeline`,
  `.read_briefing`; `StatedDestination(code, latitude, longitude)`.
- `DataRef`, `WidgetUpsert`, `WidgetBoard` (`begin_request`, `apply`,
  `snapshot`, `supersede`) in `zeo_core.contracts.ui`.
- `render_snapshot_html(cards, *, document=False, title="Briefing")` and
  `render_card_html(card)` in `zeo_core.adapters.widget_html`.
- [Briefing sources: endpoints, limits and terms](../integrations/briefing-sources.md).
