# Intervention cards

`zeo_core.contracts.ui.intervention_cards.InterventionCardPayload` validates the
`DataRef.payload` of a `widget.upsert` whose component is `intervention_card`
(zeocore-SOW-54, slice S1). One closed model per `situation`:

| `situation` | Shows |
|---|---|
| `work_location` | where claimed work lives (repo, branch, commit, PR, local only) |
| `checkpoint` | a requested checkpoint, its delivery and response |
| `blocker` | a reported blocker, since when, its release condition |
| `decision` | one ask: `decide`, `review`, `do` or `session`, with exactly that ask's fields |
| `request_status` | delivery, response, outcome and an explicit deadline |
| `outcome` | `verified` (with evidence and the verification ref), `failed` or `cancelled` |
| `project_checkpoint` | one project: seat answers, last product vs administrative progress, wait, local-only work, per-source freshness |

## Rules that hold everywhere

- A model is closed and strict: an unknown key is an error, nothing is coerced.
- An optional key is **absent** when unknown, never `null`.
- Text refuses markup and control, format and separator characters.
- A card computes nothing: no age, lateness or order. The owning service supplies them.
- `intervention_ref`, when present, equals `work_ref.work_id`. A `wi_` id must be a
  canonical ULID; a pre-existing source id is accepted otherwise.
- An action names exactly one id from the closed list of its `route`
  (`intervention`, `reserved` or `task`). The route is descriptive: the backend
  decides authority from the action id and the signed-in person.
- A disabled action states its reason. A project card offers only
  `request_checkpoint`, `request_unblock` and `explain_blocker`.
- A review ask carries exactly two image descriptors (metadata only, at most 8 MiB
  each and 16 MiB together, 4096 on a side). A `do` ask may carry bounded result
  slots and display-only pinned texts whose `sha256` is of the exact UTF-8 bytes.

## The Core profile of `widget.upsert`

`intervention_card` is the fifth component of Core's own widget profile
(`contracts/zeocore-v1/ui.widget-upsert.schema.json`). The four components of the
locked briefing spec (`flight_status`, `weather_hourly`, `arrival_timeline`,
`market_watchlist`) are unchanged and are listed separately as `RASA_OPS_COMPONENTS`;
the conformance tests run against exactly those four. The locked schema is closed, so
a consumer validating against it refuses `intervention_card`, and that is correct.

Core emits no card itself. A service that sends `widget.upsert` events to a consumer
pinned to the locked four must not send it this component: the card belongs to boards
that read the Core profile.

## For a front end

A front end reads the card as JSON and does not import Python.

- **Schema:** `contracts/zeocore-v1/ui.intervention-card.schema.json`, also printed by `zeocore schema ui.intervention-card`. It is JSON Schema 2020-12 and closed: no unknown keys.
- **TypeScript types:** `contracts/zeocore-v1/ui.intervention-card.d.ts`, generated from the schema. `InterventionCard` is a union discriminated by `situation`. A key that may be absent is optional in the type and is never `null` on the wire.
- **What the types cannot say:** rules that tie fields together, such as `intervention_ref` equal to `work_ref.work_id`, are checked by the validator. A front end shows what it is given and does not rely on the types for those rules.
- **Examples:** `tests/test_contracts/fixtures/intervention_cards/valid/*.dataref.json` holds one valid card for each situation and shape. All data is fake.

Both files are regenerated from the model; a test fails when they drift.
