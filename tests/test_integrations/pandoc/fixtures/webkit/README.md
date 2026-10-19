# WebKit differential fixtures for the hidden-text rules (zeocore-SOW-41)

`sow41-differential.json` is dt-library's synthetic contribution
(`matorclawson/org-ducktyper-ai@7b4762a282d82d9045706d9e6b0c62f849cf2ad7`,
`projects/dt-library/evidence/sow41-fixture-contribution-20261019/fixtures.json`), taken
unchanged. Four paired shapes, each measured in native WebKit (8 loads, 24 stable
snapshots): `expected_painted` is what the browser showed. The converter must keep the
painted marker and drop the hidden one. No private content, no external load.
