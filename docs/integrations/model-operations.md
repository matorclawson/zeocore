# Model-operation client contribution

This draft prepares keyless text-completion requests and checks supplied model
receipts. It does not dispatch requests, read keys, pair devices or register a
provider. It has no invoke method. Receipt checks establish format consistency,
not an authentic provider call, settlement or permission to retry.

## Draft inputs

This contribution binds model-ops draft 11
`38fe80e0ac6e35f045d1dd518da9ca31dab19c2c33c26f1d5a042643c8cdff11`,
not the contract.
These are owner drafts, **not in this repository**. A public checkout cannot
reproduce their contents; an adopted contract needs the project's normal
contract distribution.

| Owner draft, not in this repo | SHA256 |
|---|---|
| SCHEMA-draft-11.md | `38fe80e0ac6e35f045d1dd518da9ca31dab19c2c33c26f1d5a042643c8cdff11` |
| DESIGN-NOTE-rev-h.md | `e3ffee1cbc91f92218966bbfa9ccfc5a4a7631c7447bfd21a148376b43393f19` |
| request-vectors.json | `0f4c199a873e0282779edb50669ce5f4470789c7d41349d75bc2d8a9a9a4a85a` |
| request-vectors-5.json | `1af60fe28860848bc5f24427face7ee749c5c7ba222ab4779e2053b7f6de1f98` |

Draft 11 is self-contained and replaces the earlier schemas. Its recording
double stamps the schema hash into `draft_schema`; the parser requires that
exact labelled `model-ops-draft-11 sha256:<draft-11 hash>` stamp. This does not qualify an installed receiver or freeze its contract.
Proposed operations are `anthropic.messages.create`, `openai.responses.create`
and `google.gemini.generate`.

## Requests and caller-owned evidence

Closed request models accept model, messages, optional system, required
max_output_tokens and optional temperature. Temperature is finite, from 0 to 1, with at most two decimal places; the
catalogue must explicitly allow it.
The request is at most 256 KiB in RFC 8785 canonical JSON. Each catalogue model
has an owner-selected output ceiling; preparation enforces the lower of that
and the explicit connection ceiling. Integer counters fit the interoperable
canonical JSON range, up to 2^53 minus 1.

`TextCompletionRequest.canonical_bytes()` supplies the bytes the caller retains;
`request_sha256()` fingerprints those bytes. There is **no request artifact** in
this draft. The Broker receipt supplies the digest and byte count. The parser
requires the caller's expected digest and size and compares them when present.
This binds a supplied receipt to a retained copy; it does not prove dispatch.

Use `parse_model_request_json()` for untrusted raw input: it rejects duplicate
keys, invalid UTF-8, nonfinite numbers, exponents and excess decimal digits
before typed validation. Generic JSON loaders can discard that lexical evidence.
The raw input bound is 512 KiB; canonical arguments remain bounded to 256 KiB. Optional keys are absent or
valid, never explicit null.
The owner's synthetic request vectors are retained at
`tests/test_integrations/model_ops/fixtures/request-vectors-base.json`
for byte/digest and refusal tests, SHA256
`0f4c199a873e0282779edb50669ce5f4470789c7d41349d75bc2d8a9a9a4a85a`.
The extended refusal set is `request-vectors-extra.json` in the same directory,
SHA256 `1af60fe28860848bc5f24427face7ee749c5c7ba222ab4779e2053b7f6de1f98`.

`DraftModelOperationClient.prepare()` retains the connection fence and caller's
idempotency key in an existing `HostedOperationRequest`. Constructor inputs and
copied request models are revalidated. The receiver must authenticate and qualify the supplied connection ceiling
and provider catalogue; this client does not inspect a live connection or
establish those settings.

`ModelCatalogue.proposed_contract_sha256` records owner-selected draft provenance
for consumers retaining their catalogue. It is not checked against a frozen
registry and does not authorize execution. Alias-pattern checks only catch known
patterns; the owner must pin real snapshot IDs.

## Receipts and costs

A refusal before the budget is read carries no cost, budget, usage or completion.
A bad-request refusal has no validated fingerprint; other validated refusals
require it, with lifecycle fingerprints optional. A missing cap, unpriced request or inconsistent ledger projection refuses without inventing cost or budget
fields. Other budget details carry the refused worst case that did not fit, `held` is false:
that amount was neither held nor spent. Provider-reached receipts carry cost and
budget context, including provider, UTC reservation day and accounting scope.
Missing usage stays unknown. Native usage is bounded evidence, separate from
normalized components and never used here for price arithmetic.

Reservation, tariff estimate and remaining budget stay separate. The parser
neither calculates prices nor settles or releases reservations. Unknown outcomes
must keep their reservation held. Incomplete estimates also keep their hold.
Every receipt-validation failure raises `AmbiguousModelReceiptError`, regardless
of the supplied response status: the call may have been dispatched. Replay with
the same idempotency key or settle its outcome and reservation; never retry with
a new key. The parser does not replay requests or settle reservations itself.

Provider-reached receipts with known normalized usage and no incomplete cost
components must include the tariff estimate; otherwise it must be absent.
Native usage retains JSON scalars, objects and lists without coercing their
values. Its root object counts as the first of two container levels, with at
most 32 total object keys. Sensitive field names and nonfinite numbers are
refused. Draft 11 sets no additional key-length, string-length or list-length
limits for this native block; empty JSON keys are valid. Native integral numeric values must fit the signed RFC8785 safe range,
from -(2^53-1) to 2^53-1, regardless of their original spelling. Other finite
native fractions remain valid; booleans stay booleans. Broker-normalized counts
and money are nonnegative integral values in the safe range. The consumer
accepts 1.0 or 1e2 as integral values and represents them internally as integers;
it refuses fractions, booleans, nonfinite values and out-of-domain values.
Optional receipt keys and normalized usage components are absent or valid,
never explicit null. These rules do not forbid JSON nulls inside native data.
The parser receives already-decoded values and cannot preserve lexical number
spellings; callers needing original evidence must retain the response bytes.

An unknown outcome always retains its reservation. If its usage is known and
complete, its tariff estimate is still required by the draft; that estimate
does not settle the hold, establish successful delivery or permit a retry.

A confirmed completion requires matching outer and receipt artifact metadata.
Expired replay requires a matching digest/size marker and expired receipt
metadata, and supplies no fetchable completion. The caller must retain fetched
bytes and check their digest; the parser never fetches or authenticates them.
Rate-source page strings are retained and never fetched.

Confirmed means delivery, not task success. A max-token or content-filter completion must explicitly
report `truncated: true`. Reported model differences must have a consistent
`model_mismatch` flag; an uncatalogued reported model cannot claim a complete
estimate. Consumers must still verify owner-pinned model identities before
attributing benchmark results. No prefix or alias substitution is inferred.

Detailed refusal reasons must match the existing error code and status, including
in-flight and reused-key conflicts. Stop and expired application grants are HTTP
refusals, not model receipt reasons. The shared hosted response model and error
enum remain unchanged.

Use synthetic inputs in tests. Text completion alone does not establish a
coding-agent tool loop or any new AX benchmark result.


The incomplete-component vocabulary is closed: usage, cache_write, reasoning,
model, price and blocks. Rate-source pages are bounded to eight, and provider
request IDs to 128 characters. Lifecycle refusals may omit
identity/fingerprint evidence when no validated request was available.
Key-conflict refusals require the requested identity and validated fingerprint.

The draft's model_mismatch flag is true for every reported/requested ID
difference, whether the reported ID is catalogued or not. The incomplete model component is present exactly for an uncatalogued
difference, and forbidden otherwise; a catalogued difference can have a complete
estimate. Callers must explicitly pass their owner-pinned
`catalogued_reported_models` tuple; there is no implicit empty catalogue. Neither case proves the requested model was used;
raw IDs remain available for consumer checks and benchmark attribution.

Undeterminable response status raises `AmbiguousModelReceiptError`, preserving
dispatch uncertainty. The raw request reader checks string length before
encoding and rejects a BOM, trailing JSON and excessive nesting; valid -0.0
temperature canonicalises to 0.

Provider-stage receipts require an explicit `replayed` boolean;
pre-reservation refusals forbid it. `usage_provider_omitted: true` means the
Broker omitted native usage because it could not emit an in-domain block.
The marker requires absent native data and is forbidden before reservation.
The parser checks the supplied flag; it cannot authenticate stored-record
provenance or reconstruct omitted data.

Message whitespace follows the draft's exact Unicode White_Space set, rather
than Python's broader default stripping. U+001C through U+001F remain valid
message text. String bounds count Unicode code points.

## Execution with injected authority and evidence

`zeo_core.integrations.model_ops.execution.ModelOperationExecutor` composes the
existing preparer, receipt parser and hosted artifact client. It constructs no
transport, reads no credentials and registers no operation. Model operations
remain design-only until ZEOconnect adopts their receiving contract and route.
This class is not a way to activate that route or bypass its controls.

Supply the pinned `DraftModelOperationClient`, a caller-authorized
`HostedAuthorizedTransport`, a protected retention callback, a durable admission
callback, the rate revision, reported-model catalogue and completion-byte cap.
There are no default ports. `prepare(request, idempotency_key=...)` returns a
frozen `PreparedModelCall` without dispatch. `execute(prepared)` first rechecks
the complete canonical envelope against the pinned preparer, retains it, then
requires a `ModelAdmission` binding that exact envelope digest and replay key.
The admission callback must validate authority and reserve spending before it
returns. `ModelAdmission` is only an echo of that external decision, not a grant.
The Broker independently enforces its own controls and budgets.

The retention callback receives `request`, `response_normalized` or `completion`
and immutable bytes. It must durably retain them before returning their
`sha256:` digest; a different digest refuses. Canonical request bytes describe
the intent and are not a raw HTTP-wire capture. Normalized response bytes are
the validated hosted model serialized again; they are not raw provider evidence.
Consumers needing native evidence must retain that separately at transport.

Dispatch happens once per explicit `execute` call. The parser checks the model,
rate revision and canonical argument fingerprint. Completion fetches go through
`HostedConnectionClient.download_artifact`, which verifies size and digest; an
oversized descriptor is refused before fetching. An expired replay returns its
expired disposition without fetching or starting a new occurrence. A known
refusal returns its parsed disposition without a completion.

An uncertain, malformed or timed-out response, failed artifact check or failed
post-dispatch retention raises `ModelExecutionAmbiguousError`. Its `prepared`
attribute is the same original immutable request. Retain the reservation and
reconcile it under the caller's authority; the executor never retries, generates
a replacement key, or settles/releases budget. A failure before dispatch says
the executor has not sent the request, but its external admission may still need
reconciliation. Neither a delivered completion nor a scripted test proves a
working application or a successful AX benchmark attempt.


## Explicit fenced receipt successor (draft 14)

Import `parse_fenced_model_response` from
`zeo_core.integrations.model_ops.fenced_receipts`. It binds model-ops draft 14
`93267ddc27c95747fbb20b98f1ee64e1bc791e53e038d0d95b62faf8043091ab`,
not a callable contract. Council voiced that exact design on 16 October 2026.
The existing `model_ops.parse_model_response` and `ModelReceipt` remain pinned
to draft 11. Each rejects the other stamp; there is no automatic fallback,
schema-selection flag or rewrite of received evidence.

The new parser requires the original `HostedOperationRequest`, the explicit
rate revision and the reported-model catalogue. Its own `FencedModelReceipt`
and four-field `ReceiptBinding` project the actual compared connection values.
Use the parser for semantic checks; constructing the typed receipt alone does
not compare it with a request. Known fields cannot be null. Binding fields are
closed and strictly typed; common unrelated member-receipt metadata remains
ignored for forward compatibility.

A successful comparison must match the original connection, external identity,
enrolment revision and any supplied connector pin. A mismatch must report the
actual changed value and the correct fixed error message; external identity
has precedence when both expected values differ. The parser distinguishes
`precheck_mismatch`, `dispatcher_mismatch` and `dispatcher_unavailable`.
A pre-check mismatch has no budget fields and requires its stored replay flag.
A dispatcher no-call outcome has a recorded reservation projection with required
cost/budget fields and no held or invented provider-usage evidence. Other early
refusals still forbid a replay flag. Generic `provider_unavailable` alone never
proves that no provider call occurred.

Returned stage and fence-outcome values describe consistent receipt claims.
They do not prove native provider-call counts, authenticate a producer, grant a
retry or release money. The authenticated transport and retained original full
request bind the exchange: a model receipt does not echo its idempotency key.
The receiver's native tests must separately prove its actual reservation and
call transitions. Missing or contradictory evidence raises
`AmbiguousModelReceiptError` with same-request/key reconciliation guidance and
no automatic retry. Completion/expired-replay descriptors, model attribution,
usage bounds and rate/request fingerprints remain checked.

This explicit source interface does not activate a model route, enrol a
connection, read a credential, fetch an artifact or run an AX trial. Connect it
to a separately reviewed executor successor after owner review and the full
verification gate; the existing draft-11 executor candidate is unchanged.

## Explicit fenced execution composition

`model_ops.fenced_execution.FencedModelOperationExecutor` is the explicit draft-14 execution entry point. It prepares the same immutable original intent through the existing public preparer, retains it, obtains injected durable admission, and sends it once. It validates the retained response against that original request through `parse_fenced_model_response`, then checks downloaded completion size and digest before returning `FencedModelExecution`. There is no schema-selection flag or automatic fallback. The legacy `ModelOperationExecutor` remains draft11.

Storage, admission, authenticated transport and bounded deadlines are caller-supplied responsibilities. A lost response, malformed receipt or failed retention keeps the original key uncertain; the executor never retries, substitutes a key, or releases funds. Early no-budget refusal, reserved-then-released mismatch and dispatcher unavailability stay distinct parsed claims. Those claims do not independently authenticate a producer or authorise a refund. This composition does not activate a Broker receiver, qualify an isolated AX build or verify a live provider.
