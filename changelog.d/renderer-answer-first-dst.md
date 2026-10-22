bump: patch

Follow-up to the traveller cards. Each card reads in the order title (and any REPLAY or
SUPERSEDED state), the answer, how fresh it is, then the provenance labels. A list that crosses a
daylight-saving change names the zone on every time (01:00 EDT, 01:00 EST) in the weather table and
the timeline, and a timeline that spans two days shows the weekday on every stop. The weather chart is
drawn in a 300-unit box so a 262 px card renders its labels above 16 px. The tables sit in a labelled,
keyboard-focusable scroll region. `render_snapshot_html` and `render_card_html` take an optional aware
`now` so relative text ("in about 2 h") is the same every time; without it the text counts from each
card's own fetch time. The test that checked "no socket, no bind" by searching the source text now
checks the syntax tree (no socket-type import, no bind or listen call), because the markup attribute
`tabindex` contains the letters "bind".
