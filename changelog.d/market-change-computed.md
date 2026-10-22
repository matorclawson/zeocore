bump: minor

The HTML renderer labels a market card's change and change % as COMPUTED, not
PROVIDER DATA, when the data reference declares the assumption
`change_computed_from_two_reference_days` (`widget_html.MARKET_CHANGE_ASSUMPTION`):
"change and change % COMPUTED from two reference days <day> and <day>, not provider
data (inputs: reference rates from <source>, fetched <time>)". The price keeps its one
PROVIDER DATA card label and the card its one freshness line. Nothing is claimed for a
REPLAY, for a card without the declaration, or when no change exists (it stays "change
unavailable"). The assumption is a derivation note, not an ASSUMPTION tag.
