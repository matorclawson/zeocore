# Model-operation client contribution

This draft contributes typed, keyless inputs, request preparation and receipt parsing for the
proposed ZEOconnect model operations. It does not register a provider, dispatch
requests, read keys, pair devices or establish coding-agent access.

`ModelMessage`, `TextCompletionRequest` and `ModelCatalogue` are closed models.
They bound serialized UTF-8 JSON (including escaping and structure) to the
proposed 256 KiB limit, validate finite temperature from 0 to 1, and enforce each model’s
owner-selected output-token ceiling. They retain exact catalogue model
IDs and omit prompt text from representations. A catalogue is not a spending
grant. The receiving owner must qualify exact model IDs and limits.

`DraftModelOperationClient.prepare()` returns an existing
`HostedOperationRequest` with the selected connection, connector revision,
expected-binding fence and caller-supplied idempotency key. Proposed operations
are Anthropic Messages, OpenAI Responses and Gemini generation. No transport
URL, native provider header or key can be supplied. There is no invoke method.

The draft follows model-ops-20261013 revisions A/B. It deliberately remains
outside the reviewed hosted-service registry. Its request limits and operation
names are contribution proposals, not frozen Broker contracts. After contract
review, add receiving-compatible request/receipt mappings and fake handshake
checks, then qualify the installed and allowlisted receiver before activation.

Use only synthetic inputs in tests. No live provider call or secret is needed.

The request subset now follows SCHEMA-draft-1 SHA256
`4b45e5913e3ad7eee937f34f31e5fbb187890bca094e8cabe6e3325ad68d629a`.
The five accepted fields are model, messages, system, max_output_tokens and
temperature.

`parse_model_response()` checks a synthetic or supplied hosted response against
the requested model and catalogue revision. It keeps reservation, tariff estimate
and remaining budget separate. Missing usage stays missing: it is not counted as
zero. Native provider usage is a bounded evidence block and is never used for
price arithmetic. The parser neither calculates prices nor settles reservations.

Receipt parsing follows draft 2 delta b for response mapping and expiry, and
draft 2 delta c for stage-specific fields, SHA256
`f9b85f190ae5bb98c60da175a1afd59ebdcd0da3a626d047687ca8452b370e5c`.
Detailed refusal reasons must match the existing uppercase error code and response
status. A live completion uses the outer artifact descriptor. An expired replay
has matching digests and sizes in its marker and receipt metadata; it supplies no
fetchable completion. These checks never establish that a provider was called,
that usage is authentic or that a retry is safe.

This is draft format conformance, not a frozen contract or live handshake. The
receiving owner and council must review the contract, including failure receipts,
before any receiver qualification or activation.


A validation or catalogue refusal happens before the budget is read: it carries
a request fingerprint, with no cost, budget, usage or artifact fields. A budget
refusal carries the worst-case amount that would not fit and `held: false`.
That amount was refused, not spent or reserved. Once the provider is reached,
cost, budget and request-artifact metadata are required. `held` records whether
the reservation remains held; an unknown outcome must keep it held. Fields that
were never computed are absent, never filled with zeros.

Lifecycle receipts carry capability and one of the owner's exact reasons:
`connection_revoked`, `connection_repair_required` or
`connector_revision_unavailable`. They carry no computed request, usage, cost or
budget fields. Unknown or absent refusal reasons are rejected rather than guessed. HTTP-level
stop and expired application grants are not parsed as model-operation receipts.


## External draft inputs

These owner-provided files are **not in this repository**, not public contract
artifacts and not frozen. They reside under the absolute directory
`/Volumes/ZeoWork/zeoconnect/design/model-ops-20261013/`. A reader without that
host cannot reproduce their contents from this PR. Once adopted, the frozen
contract must be provided through the project's normal contract distribution.

| File under that directory | SHA256 |
|---|---|
| DESIGN-NOTE-rev-a.md | `7ad6355ea8a6f83c3712981e2a30c261c27777bd5399ffa7f980e1e7459109dc` |
| DESIGN-NOTE-rev-b.md | `5c721ca329eb009a4d5a23e8a692f440e206e510e81e2dda15ce6d29b107854b` |
| SCHEMA-draft-1.md | `4b45e5913e3ad7eee937f34f31e5fbb187890bca094e8cabe6e3325ad68d629a` |
| SCHEMA-draft-2-delta.md | `892163086ed9d2fd5241acc25503ea51765e086b2c2aa9ce732353b3f8734347` |
| SCHEMA-draft-2-delta-b.md | `e737e8fe615143e776b746d1c7547693e7b29a749f6ca8562fb7057aa6b0e007` |
| SCHEMA-draft-2-delta-c.md | `f9b85f190ae5bb98c60da175a1afd59ebdcd0da3a626d047687ca8452b370e5c` |

The lifecycle names were clarified separately in owner message
`ans_bcc931b11f593da5` on 14 October. That message is not a frozen contract either.
No installed Broker contract version is claimed by these draft formats.

## What the checks establish

A malformed ambiguous receipt raises `AmbiguousModelReceiptError`: the call may
have been dispatched and must not be retried until its outcome and reservation
are settled. Other parsing failures also do not prove that no dispatch happened.

In the owner draft, a live completion uses the outer artifact descriptor;
`receipt.completion` is a second descriptor on replay. When present, both must
match. Its absence on a live result means there is no independent receipt copy
to compare. The caller must retain and check fetched bytes against the digest;
this parser checks metadata and never fetches or authenticates those bytes.

`model_reported` is retained as evidence, not matched to a live provider catalogue.
The consumer and receiving owner must check the exact provider-reported ID against
the pinned snapshot or an explicitly reviewed alias mapping before treating a
result as belonging to the requested model. No prefix or alias substitution is
inferred here. The catalogue's alias-name checks only catch known patterns;
the owner remains responsible for pinning actual snapshot IDs.

Rate-source page strings are retained as evidence and are never fetched. All
integer counters fit canonical JSON's interoperable range, up to 2^53 minus 1.
