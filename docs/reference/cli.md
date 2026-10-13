# The `zeocore` command

`zeocore` lets another service or language use zeocore without importing
Python: the ZEOconnect Broker and WEB, a TypeScript pipeline, a shell
script. Each call reads JSON on stdin and writes JSON lines on stdout, the
result last.

```console
$ zeocore help --json
$ zeocore version
{"cli_protocol": "1", "commands": ["artifact", "connections", "digest", "doctor", "help", "invoke", "login", "logout", "release", "schema", "validate", "version", "whoami"], "ok": true, "schema": "zeocore.version/v1", "zeoconnect_protocol": "1", "zeocore": "…"}
$ zeocore schema list
$ zeocore schema hosted.operation-request
$ echo '{"code": "RATE_LIMITED", "message": "slow down"}' | zeocore validate connections.normalized-error
$ echo '{"b": 1, "a": [1, 2]}' | zeocore digest
{"ok": true, "schema": "zeocore.digest/v1", "sha256": "sha256:…"}
```

An agent starts with `zeocore help --json`, the machine catalogue
(`zeocore.help/v1`): every command, its arguments, what it reads on stdin,
the exit family and the schema its result names. A test holds the catalogue
to each command's parser, so it can't drift from the code. Every answer is
JSON already, so `zeocore help` without `--json` answers the same.

## Commands

| Command | Reads stdin | Answers |
|---|---|---|
| `help [--json]` | no | the machine catalogue |
| `version` | no | the release, `cli_protocol`, the ZEOconnect protocol, the commands |
| `doctor [--profile P]` | no | is this machine ready: Python, session store, pairing, `ZEOCONNECT_URL`, extras; see below |
| `schema list` | no | the stable schema names |
| `schema <name>` | no | that contract as JSON Schema 2020-12 |
| `validate <name>` | yes | `{"ok": true, "value": …}`, the normalized value, or `{"ok": false, "errors": [{"loc": […], "type": …}]}` |
| `digest` | yes | the sha256 of the value's RFC 8785 canonical bytes |
| `login`, `logout`, `whoami`, `connections`, `invoke`, `artifact get` | see below | the ZEOconnect client, for a product running as its own service user; not for seats, which use `zeo` |
| `release sign`, `release verify`, `release tree-digest` | yes | the shared signing step, and a bundle's tree digest; see [Sign and verify a release](../how-to/release-signing.md) |
| `release pypi plan \| admit \| apply \| approve \| reconcile \| verify` | yes | the PyPI adapter; see [Release to PyPI](../how-to/release-pypi.md) |

## Is this machine ready? `zeocore doctor`

`zeocore doctor` answers what an agent would otherwise find out by failing. It is
local and read-only: no network call, nothing written, and never a token, a path
or an environment value in the answer.

Each check is one event line, `{"event": "check", "check": …, "ok": …, "message": …}`.
The result is last, with `ok`, `outcome` (`ready` or `not_ready`), `failing` (the
names of the checks that failed) and `checks` (the same lines, for a caller that
doesn't stream). A failing check carries `next`, the exact command or step that
fixes it, and `who` when someone other than the caller must act.

| Check | Passes when | When it fails, `next` is |
|---|---|---|
| `python` | Python meets the package's floor, 3.14 | `uv tool install --python 3.14 zeocore` |
| `zeoconnect_url` | `ZEOCONNECT_URL` is unset or an allowed Broker origin | unset it, or set `ZEOCONNECT_DEVELOPMENT=1` for a development Broker |
| `session_store` | the Keychain (asked for at most 5 s), or the file `ZEOCORE_SESSION_FILE` names, is readable | how to make the store usable, for that refusal |
| `pairing` | this profile holds a grant that hasn't expired | `zeocore login`, by `who`: a person, who approves it in ZEOconnect |
| `extra:github` | `zeocore release` can load | `pip install 'zeocore[github]'` |

Exit 0 when every check passes, 20 when any fails (for `doctor`, 20 means *not
ready*), 2 for a `--profile` that can't be used, found before any check prints. Checks
print as they finish. A
failing `zeoconnect_url` or `session_store` also fails `pairing`, which then says
it was not checked: with a bad origin `zeocore login` would fail too, so no
person is named.

## The ZEOconnect client

!!! warning "Not for seats: seats use `zeo`"
    These commands are for **products**, and only when the product runs as
    its **own macOS service user**, `zs-<product>` (the first is
    `zs-newsroom-intake`). They are **not a seat interface**: a seat reaches
    the Broker only through ZEO-RT's front door, with its `zeo` command
    (ZEOCORE-SOW-27), and they are deprecated for seats now.

    From Broker contract 1.4, the Broker accepts a product's grant only from
    that product's own service user. From any other user, `rodbot` and a
    product author's own login included, it is refused with
    `app_identity_unverified`. A seat's grant is refused with
    `door_required`, and a grant adopted as no principal with
    `no_principal`. zeocore can't tell these apart on one machine, so the
    Broker enforces them, not this command.

Products reach the ZEOconnect Broker through these commands instead of writing a
Broker client of their own. An app never holds a provider token. The machine
holds only that app's own device grant, in the macOS Keychain.

| Command | Reads stdin | Does |
|---|---|---|
| `login [--device-name N]` | no | Pairs this device. Emits `{"event": "pair", "verification_url", "user_code", "expires_at"}`, waits for a person to approve in ZEOconnect, then stores the grant. |
| `logout` | no | Revokes the grant and deletes it. |
| `whoami` | no | Says whether this profile is paired, with its device id and expiry. Local only: no network, never a token. |
| `connections [--service S]` | no | Lists the grant's connections. |
| `invoke <operation_id> --connection C [--occurrence L] [--wait SECONDS \| --wait-approval SECONDS]` | the arguments object | Runs one operation and answers the Broker's response plus `request_key` and `retry`. |
| `artifact get --out PATH` | an artifact descriptor | Downloads an output to a new file and checks its size and digest. |

**Refusals from Broker contract 1.4.0.** Each comes before custody, so nothing
happened, and the same grant can't succeed. The answer names it in `outcome`,
with `retry: "none"` and the way back in `message`:

| `outcome` | Means | The way back |
|---|---|---|
| `door_required` | a seat's grant | seats use `zeo`, ZEO-RT's front door |
| `app_identity_unverified` | a product's grant from another macOS user | run the product as its own service user, `zs-<product>` |
| `no_principal` | a grant adopted as no principal | the owner adopts the grant as the product's principal |

A stop answers `outcome: "stopped"` with its `control` and `scope`, and a
Broker that doesn't speak this zeocore's protocol answers
`outcome: "upgrade_required"`.

**A product's service user: the named session file.** By the operator's
ruling of 2026-10-12, a product running as its own service user
(`zs-<product>`) may keep its grant in one named file instead of a Keychain,
by setting `ZEOCORE_SESSION_FILE` to an absolute path, for example in its
launchd definition. It is **opt-in, never a fallback**: without that variable
the Keychain is the only store, and a Keychain that can't be read is still
exit 12. zeocore refuses the file, with exit 12, unless all of these hold:

- the directory is owned by that user, mode 0700;
- the path has no symbolic link;
- no directory above it is writable by another user unless it is sticky;
- the file is a regular file owned by that user, with no group or other
  access.

Writes are atomic, at 0600. The file records the Broker it was paired with
and is never sent to another. A session file names one grant, so it can't be
combined with `--profile`. ZEO-RT qualifies it on the real account.

Every client command takes `--profile P`, or reads `ZEOCORE_PROFILE`, to choose
the grant, so one machine can run several apps, each paired on its own. A
profile is attribution, not a security boundary: all profiles of one macOS user
can read each other. With no usable Keychain, a command fails closed with exit
12. There is no plaintext fallback.

The origin is the production Broker unless `ZEOCONNECT_URL` names another.
Development origins also need `ZEOCONNECT_DEVELOPMENT=1`.

### Invoking

- **The idempotency key is the request.** `request_key` is derived from the
  connection, the operation, the canonical arguments and `--occurrence`, so
  running the same invocation again is an exact replay of the stored outcome
  and never a second act. Use a new `--occurrence` for a deliberate new act.
- **`--wait-approval SECONDS`** handles an effect that needs a person's
  approval. It emits `{"event": "approval", "approval_url", "request_key"}`
  once, then asks again with the same key every 5 s until the answer is no
  longer `approval_required`, or the time runs out (exit 10).
- **`--wait SECONDS`** (1 to 3600) waits until the outcome is final, so an agent
  never has to poll. It asks again with the same key every 5 s through a
  person's approval, an answer still in flight, and ZEOconnect being
  unavailable. It emits each kind of wait once: the `approval` event, then
  `{"event": "waiting", "reason": "in_flight" | "unavailable", "request_key"}`.
  The exit is the final answer's own. At the deadline it is the last answer's:
  10 or 11. An ambiguous answer (13), or a Broker that didn't answer at all, is
  never retried. `--wait` and `--wait-approval` can't be combined.
- **`retry`** says what another attempt needs:
  - `same_request`: ask again as it is;
  - `new_occurrence`: the failure is recorded, so a retry is a new act;
  - `none`: change the request.
- **Exit codes:**
  - 0 `confirmed`;
  - 10 `approval_required`;
  - 11 still in flight, or ZEOconnect is unavailable;
  - 12 not paired, or no session store;
  - 13 `ambiguous`, or no answer arrived (asking again with the same request is
    safe);
  - 20 refused, `failed_safe` or stopped.

## Rules every command keeps (`cli_protocol` 1)

- **stdout is JSON lines and nothing else.** Events (a pairing code, a doctor
  check, an approval link, waiting) carry an `event` key. The result is always the last line: it
  carries `ok` and `schema` (`zeocore.<command>/v1`; `zeocore.usage/v1` for
  an unknown command; `zeocore.internal/v1` for an internal error) and never
  `event`. `zeocore schema <name>` is the one exception: its answer is the
  contract itself, named by its own `$schema` and `$id`. A command with no
  events prints exactly one line. Diagnostics go to stderr.
- **Input is strict JSON:** no duplicate keys, no `NaN` or infinities, at most
  1 MiB.
- **Errors never echo input.** A validation error is its location and type only.
  A location keeps declared field names and list positions; any other key (inside
  a free-form object such as `arguments`, or an unknown extra) shows as `*`.
- **Exit status** is one family, the same numbers as the YouTube publish
  command's:

  | Code | Meaning |
  |---|---|
  | 0 | done |
  | 2 | invalid input or command; nothing was sent |
  | 10 | approval required |
  | 11 | waiting: try the same request later |
  | 12 | not paired: run `zeocore login` |
  | 13 | ambiguous; never retried by the command |
  | 20 | held or refused, including a value that doesn't match its schema; for `doctor`, not ready |
  | 1 | internal error in zeocore: `{"ok": false, "outcome": "internal"}`, no detail. For an operation, treat it like 13 |

A change to these rules is a new `cli_protocol`, announced like a contract change.

## Schemas

| Name | Contract |
|---|---|
| `connections.normalized-error` | `NormalizedError` |
| `connections.observation-artifact` | `ObservationArtifact` |
| `hosted.artifact-descriptor` | `HostedArtifactDescriptor` |
| `hosted.availability-snapshot` | `AvailabilitySnapshot` |
| `hosted.connection-summary` | one `GET /v1/connections` entry (`HostedConnectionWire`) |
| `hosted.operation-request` | `HostedOperationRequest` |
| `hosted.operation-response` | `HostedOperationResponse` |
| `revolut.account` | Revolut `Account` |
| `revolut.transaction-page` | Revolut `TransactionPage` |

- **A name is a promise.** It keeps naming the same contract until a major
  version. New names are additive.
- **The files are committed** in the repository under `contracts/zeocore-v1/`
  as `<name>.schema.json`, for callers that vendor files and run no zeocore at
  all. A test fails if a model and its committed schema drift.
- **Not listed yet:** Revolut's `TransactionQuery`. Its wire field is `from`
  (Broker contract §6), but the model's field is `from_` with no alias, so its
  schema would name the wrong field.

## Digests

`digest` is the sha256 of the value's [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785)
(JCS) canonical bytes, prefixed `sha256:`. Two parties that each canonicalize
the same JSON value get the same digest without sharing code.
