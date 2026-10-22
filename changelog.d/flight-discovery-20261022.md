bump: minor
Add a bounded flight-discovery reader that matches live airborne positions near a destination against a caller-supplied, dated public route subset. One guarded ADS-B point GET is cached and budgeted; results contain up to five matched candidates, with an explicit distinction between no flights found and provider failure. No route dataset or new host is added to Core.
