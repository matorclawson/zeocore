bump: minor

Add the widget-routing contract for a live dashboard: the component registry
(flight_status, weather_hourly, arrival_timeline, market_watchlist), the
`widget.upsert` event, data references that carry provenance and the typed honesty
labels (replay, delayed, assumption), and `WidgetBoard`, which refuses a stale
revision and a result for a superseded request. Schemas: `zeocore schema ui.widget-upsert`
and `ui.widget-data`.
