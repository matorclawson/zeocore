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

Only api.met.no, api.adsb.lol and data-api.ecb.europa.eu are allowed, on HTTPS 443
and fixed documented path shapes. Redirects, userinfo, nonstandard ports,
arbitrary URLs and environment proxies are refused. The client serialises
requests, sends an identifying User-Agent, caches each request for at least 60 s
and respects MET's later Expires header. HTTP429 establishes Retry-After
cooldown with no automatic retry. Each client run has a 20-request per-host rolling ten minute
budget and each response is limited to 1 MiB with a 10 s inactivity timeout and a monotonic deadline checked on every unbuffered network chunk. Keep one shared client for the service run; do not create
new clients to evade the rolling budget, source cooldown or cache.

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
Partial ECB replies make the whole market card unavailable.
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

The ADSB route lookup accepts HTTP 200 or 201 Created for the guarded
`POST /api/0/routeset` only. GET responses still require HTTP 200; redirects,
202 Accepted and 204 No Content remain unavailable. Ops observed the 201
status on a real route lookup; regression payloads remain synthetic.

A caller may pass `StatedDestination(code, latitude, longitude)` resolved from
its own versioned local airport table to `flight` or `read_briefing`. Core
validates the code and finite coordinate ranges before access. It does not
look up a destination or contact a new host. A stated destination overrides a
conflicting source route, with a visible disagreement and the explicit label
"destination stated by traveller". The estimate and timeline name this input;
they remain COMPUTED, never an airline schedule.

An empty or malformed route no longer discards a valid position. The flight
card keeps its validated altitude, speed and position age in the existing
provider-facts status string and says "Route not provided by the source".
Without a destination, route and estimate stay not provided; dependent weather
and timeline stay unavailable with that fixed reason. Real position failures
still return an empty unavailable reference. These offline cases use synthetic
payloads, not a claim that a live journey has been accepted.
