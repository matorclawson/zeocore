# Model-operation client contribution

This draft contributes typed, keyless inputs and request preparation for the
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

The first planned capability is text completion. A coding-agent tool loop needs
a separate contract and complete tool-call/results evidence. The organisation's
budget must cover all providers and unsettled reservations, with other campaign
costs accounted for separately. Native usage and rates, request/completion
artifacts, safe refusals and expired replay remain receiving prerequisites.

Use only synthetic inputs in tests. No live provider call or secret is needed.

The request subset now follows SCHEMA-draft-1 SHA256
`4b45e5913e3ad7eee937f34f31e5fbb187890bca094e8cabe6e3325ad68d629a`.
The five accepted fields are model, messages, system, max_output_tokens and
temperature. Receipt mapping waits for clarification of native usage keys,
artifact formats/expiry and non-confirmed hosted-response shapes. No frozen
contract or live handshake is claimed.
