bump: minor

Add `zeo_core.adapters.widget_html`: `render_snapshot_html(board.snapshot())` and
`render_card_html(card)` turn the cards of a `WidgetBoard` into HTML with inline CSS and
inline SVG only (flight status, an hourly weather chart, an arrival timeline with the
margin, a market watchlist with sparklines). A pure function: no JavaScript, no external
resource, no link, no IO; every value is escaped; key-shaped strings and URLs are
withheld; REPLAY, DELAYED, ASSUMPTION and SUPERSEDED labels are always visible; an
invalid reference renders as unavailable.

Every card carries ONE provenance label: REPLAY, or PROVIDER DATA for real data (a value
computed from a live position says COMPUTED FROM LIVE POSITION beside it, flagged by the
assumption label `estimated_arrival_from_live_position`); ASSUMPTION and SUPERSEDED
appear where they apply. Every card that is not a REPLAY carries ONE freshness line:
"daily reference rate, not a live quote" (a payload with `rate_basis: daily_reference`),
"DELAYED, not real time", or "live"; "live" is never shown for a daily or delayed value.
Each card has an attribution line; with `document=True` the title is also the visible
page heading. Missing schedules, rain probabilities and market changes render as "not
provided" or "change unavailable", never as 0. An error reason is shown only as plain
prose; numbers are shown exactly as given (the shortest exact decimal: a tiny nonzero value is never shown as 0); secret assignments (`NAME_TOKEN=...`) are withheld with key-shaped strings and URLs.
