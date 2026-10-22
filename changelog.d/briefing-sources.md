bump: minor

Add keyless public briefing-source adapters for MET Norway hourly forecasts,
ADSB.lol positions with Virtual Radar Server routes, ECB daily EUR reference
rates and a computed arrival timeline. Fixed outbound endpoints, identifying
User-Agent, bounded response sizes and request counts, per-source request cache,
rate-limit refusal and explicit unavailable outcomes preserve source provenance.
Reference-rate differences use Decimal arithmetic rounded half-even to six places;
missing schedules, precipitation probabilities and previous rates stay absent.

The source contribution is authored by rasa under zeocore-SOW-43 rev2 and received
by zeocore. The graphical renderer and widget contracts remain Core owned.
