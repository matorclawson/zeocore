# Model-operation client contribution

This draft prepares keyless text-completion requests and checks supplied model
receipts. It does not dispatch requests, read keys, pair devices or register a
provider. It has no invoke method. Receipt checks establish format consistency,
not an authentic provider call, settlement or permission to retry.

## Draft inputs

The contribution binds **model-ops draft 3**, not a frozen Broker contract.
These are owner drafts, **not in this repository**. A public checkout cannot
reproduce their contents; an adopted contract needs the project's normal
contract distribution.

| Owner draft, not in this repo | SHA256 |
|---|---|
| SCHEMA-draft-3.md | `753d8b1ca692f7b579ff340544d4e2321525464919ecf1a0ac418bc1598449d9` |
| DESIGN-NOTE-rev-d.md | `2984a7839a4f2ade0103fbddd4adeafdb437eb5e1f39fcdeea11ec41a71474b3` |

Draft 3 replaces the earlier request schema and receipt deltas. Its recording
double stamps the schema hash into `draft_schema`; the parser requires that
exact labelled `model-ops-draft-3 sha256:<hash>` stamp. This does not qualify an installed receiver or freeze its contract.
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
require it, with lifecycle fingerprints optional. When a budget refusal includes the worst case that did not fit, `held` is false:
that amount was neither held nor spent. Provider-reached receipts carry cost and
budget context, including provider, UTC reservation day and accounting scope.
Missing usage stays unknown. Native usage is bounded evidence, separate from
normalized components and never used here for price arithmetic.

Reservation, tariff estimate and remaining budget stay separate. The parser
neither calculates prices nor settles or releases reservations. Unknown outcomes
must keep their reservation held. Incomplete estimates also keep their hold. A malformed ambiguous receipt raises
`AmbiguousModelReceiptError`: the call may have been dispatched and must not be
retried until its outcome and reservation are settled. Other parsing failures
also do not prove that no dispatch happened.

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
