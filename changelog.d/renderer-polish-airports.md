bump: patch

Display polish for the traveller cards. Pass `airports={"KJFK": Airport("KJFK", "JFK", "New York"), ...}`
(the class's airport table) to `render_snapshot_html` or `render_card_html` and the cards say "Landing about
17:35 at JFK" and "London (LHR) to New York (JFK)", with the ICAO codes only in the details line; without
the table the codes are shown as given. The arrival timeline no longer repeats its verdict as a minutes line
(the headline carries it, coloured late or spare). The market card drops the market-hours line and writes the
publication date in words ("published 21 Oct"). The long "COMPUTED from the flight estimate, ..." label is now
"COMPUTED" with its inputs named in the details (a replay still never claims a computation). The adapter's own
live-position text ("Live position: 25000 ft, 350 kt, position 5 s old") is shown as "In the air · 25,000 ft ·
350 kt · position 5 s old"; any other status text is shown as the provider gave it.
