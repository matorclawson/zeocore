bump: patch

`PublicSourceHTTP` serialises requests per host instead of globally. Before, one lock was held for the
whole of every request, including the one-request-per-second pacing wait, so a read from one source
(say the flight source waiting out its pacing) kept every other source (weather, exchange rates) from
starting. Now each host has its own lock: the same host still never has two requests in flight, and its
pacing, its 60 s cache and its 20-requests-per-10-minutes budget are unchanged, but different hosts run at
the same time. The refused-request counter is still exact under many threads.
