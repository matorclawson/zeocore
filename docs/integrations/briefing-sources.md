# Public briefing sources

The briefing sources integration reads three fixed public keyless APIs and
returns the maintained four-component DataRef contract. It never takes model
prose as a source value, reads a provider credential or scrapes HTML.

Create PublicSourceHTTP with an optional injected httpx transport and clock,
then PublicBriefingSources with that client. Call flight(callsign),
weather(latitude, longitude, location=airport_code, arrival=aware_datetime),
market(currencies), or arrival_timeline(flight_reference,
transfer_minutes=the_explicitly_stated_number, meeting_time=aware_datetime).
Close the client after use. read_briefing composes all four references and
uses the route's destination coordinates for the weather request.

Only api.met.no, api.adsb.lol and data-api.ecb.europa.eu are allowed, on HTTPS443
and fixed documented path shapes. Redirects, userinfo, nonstandard ports,
arbitrary URLs and environment proxies are refused. The client serialises
requests, sends an identifying User-Agent, caches each request for at least60s
and respects MET's later Expires header. HTTP429 establishes Retry-After
cooldown with no automatic retry. Each client run has a20-request per-host
budget and each response is limited to1MiB with a10s timeout. Do not create
new clients to evade that budget.

MET Norway hourly forecasts carry CC BY4.0 attribution. Missing precipitation
probability stays null. ADSB.lol position data carry ODbL1.0 attribution and
routes credit Virtual Radar Server standing data, CC0. Positions older than120s,
missing/zero ground speed, ambiguous multi-leg routes or an unconfirmed route
produce an empty unavailable reference. The arrival estimate is computed from
position, speed and great-circle distance: it is not an airline time. No
scheduled date, terminal or airline status is invented.

The timeline uses only that validated estimate, the traveller's stated transfer
and meeting time. Transfer is explicitly an ASSUMPTION. ECB daily EUR reference
rates retain their actual publication date and say daily reference rate, not a
live quote. Changes use Decimal arithmetic rounded half-even to six decimal
places; a missing previous observation leaves change null. Source: ECB
statistics; data are freely available from ECB. Derived changes are identified.
No stock prices, invented rows, today-stamped holidays or fake midnight
publication time are supplied.

Provider failures return an error DataRef with empty payload and a fixed plain
reason. An expired value is never a successful fallback for a failed refresh.
Personal aircraft-owner, passenger and crew fields are discarded before cache
custody. Each displayed value needs the renderer's honest provenance and
freshness labels; the sources do not implement a second HTML renderer.

The recorded smoke contains one real forecast, an empty aircraft response and
an unavailable ECB transport. Successful synthetic offline vectors test
behaviour; they are not live-source or full-demo proof. Independent consuming
fresh-clone and graphical acceptance remain separate.
