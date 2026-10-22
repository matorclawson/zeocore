bump: patch

The HTML renderer no longer calls a forecast or a computed timeline "live". The weather
card's one freshness line reads "forecast, not a live reading" and the arrival timeline's
reads "computed from its inputs, not a live reading"; the flight card (a position read now)
keeps "live", and the daily and delayed lines and the REPLAY rule are unchanged. Tables
(the hourly forecast and the market watchlist) now sit in a scrolling box inside their card
and cards may shrink in the grid, so a wide table no longer runs out of its card into the
next one at desktop width, and no value is clipped. The layout is set by CSS rules checked
by tests; it has not been measured in a browser by this change.
