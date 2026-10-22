bump: patch

The HTML renderer shows the cards the way a traveller reads them. Pass `place=Place("JFK",
"New York JFK", "America/New_York")` to `render_snapshot_html` or `render_card_html` and every
time is in that place's zone, short ("Landing about 17:35 at JFK, in about 7 h 55 min"), with
the full ISO time only in a small details line; without a `Place` every time is UTC and says
so (a payload's own `timezone`, often UTC, is never trusted as the traveller's). Each card
leads with its one big fact: the flight's landing time, "14 °C, 0.2 mm rain at landing",
"You will make it: 32 h 5 min spare" (or "miss it"), "1 EUR = 1.1234 USD". Rates are shown to
4 places and changes to 2 (half-even; a nonzero value is never shown as zero). Fields the
source lacks are left out, and "airline schedule not provided by this source" is said once.
Freshness lines: a forecast says "forecast, issued <the provider's issue time>" (never the
fetch time) and the arrival timeline "computed from a live position <age> old"; replay shows
no line, delayed beats daily beats these, and a position is "live". The weather chart is
larger with labelled hours and degrees, and the two tables scroll inside their card instead of
running into the next. Layout is CSS checked by tests, not measured in a browser by this
change.
