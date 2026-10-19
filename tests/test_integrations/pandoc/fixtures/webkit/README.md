# WebKit measurements for the hidden-text behaviour (zeocore-SOW-41)

`sow41-differential.json` is dt-library's synthetic contribution
(`matorclawson/org-ducktyper-ai@7b4762a282d82d9045706d9e6b0c62f849cf2ad7`,
`projects/dt-library/evidence/sow41-fixture-contribution-20261019/fixtures.json`), taken
unchanged. Four paired shapes (eight cases), each measured in native WebKit (8 loads,
24 stable snapshots): `expected_painted` is what the browser showed. No private
content, no external load.

The converter keeps every painted marker. It drops an unpainted one only where the
element's own style says `display:none`; the clipping cases (`max-height:0` or
`max-width:0` with `overflow:hidden`) are kept and reported in `diagnostics`, because
CSS clipping is not evaluated. The test pins both facts: it is the documented fidelity
limit with browser evidence.
