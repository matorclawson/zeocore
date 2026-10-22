bump: minor

Widget payloads can say "not provided" instead of inventing a value, for sources that do
not give one. `FlightStatusPayload.scheduled_departure` and `scheduled_arrival` and
`WeatherHour.precip_prob` may be null or absent (a keyless ADS-B position has no
schedule; a forecast may have no rain probability). A daily reference rate
(`MarketWatchlistPayload`) can carry its publication day as `quote_date` (a calendar
day, no invented midnight) and `rate_basis: "daily_reference"` (not a live quote);
series points may be dated by day. Existing payloads stay valid.

A market row's `change` and `change_pct` may be null or absent when there is no previous
value to compare with; they are never filled with 0.
