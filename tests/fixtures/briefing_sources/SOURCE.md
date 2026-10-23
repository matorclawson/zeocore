# Public-source recording provenance

sow: zeocore-SOW-43

One authorised source-read campaign, 2026-10-22 00:52:51–00:52:52 UTC,
using Python3.12.13 standard-library HTTPS with no candidate imports, proxy,
credential, retry, redirect or listener. Deadline60s, timeout10s, response1MiB.
Identifying User-Agent: RasaCommunityFlightBriefing/1.0 (+https://rasa.community).
Only three requests occurred; the route lookup was skipped because no observed
position existed. No coordinate was invented to force a route response.

- adsb-position.json: actual HTTP200 empty aircraft result for UAE4. Reduced to
  now/ac and whitelisted aircraft fields. ADSB.lol contributors, ODbL1.0.
- adsb-routes.json: JSONnull records the SKIPPED lookup; not a provider response.
  Positive route tests explicitly use synthetic test vectors, never live claims.
- met-locationforecast.json: actual HTTP200 public MET Norway compact forecast
  at the explicitly selected EGLL smoke point. Data from MET Norway, CC BY4.0.
  This stand-alone source check does not claim it was the observed flight's
  destination. Forecast raw response39122B SHA256
  1e10154feabe6af5e1317c2cb8be53a904a3e4222e252d7c6d4e0c5f01242d24.
- ecb-rates.csv: header-only normalised fixture because the actual bounded ECB
  request failed with URLError. It is not a successful quote response. Positive
  ECB arithmetic/date tests explicitly use synthetic CSV vectors. Source:
  ECB statistics, freely available; daily reference rate, not a live quote.

The raw ADS-B body hash is
adf5a410cea51007e972eaa75718d4e5a3ad8a8747ea485fa3dc33326baa33a2.
No passenger, crew, owner, credential or capability field is retained.
Public reads are intermediate evidence, not candidate or consuming-demo acceptance.
