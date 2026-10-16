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
