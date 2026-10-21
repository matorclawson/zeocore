bump: minor

Add the widget-routing contract for a Mantle briefing agent, built to the rasa-ops
specification: the component registry (flight_status, weather_hourly,
arrival_timeline, market_watchlist), the `widget.upsert` event, `data_ref` with
provenance (replay and delayed as separate flags, a nullable provider update time,
typed assumptions, an error with an empty payload), and `WidgetBoard`, which ignores
a stale revision and refuses a result for a superseded request. Schemas:
`zeocore schema ui.widget-upsert` and `ui.widget-data-ref`.
