# zeocore 0.15.0

ZeoCore 0.15.0 ships every change since 0.12.0. Three planned releases were never
published (0.13.0, 0.14.0 and the binding-fence release), and their changes ship
together here. Python 3.14 or newer remains required. See
[CHANGELOG.md](CHANGELOG.md) for the complete history, with every entry.

## The `zeocore` command

`zeocore` is now an installed command with one protocol: JSON lines on stdout,
the result last, and one exit family (0 done; 2 invalid; 10 approval required;
11 waiting; 12 not paired; 13 ambiguous; 20 held or refused). It has
`version`, `schema`, `validate` and `digest`, the ZEOconnect client, and
`release`.

**The ZEOconnect client** (`login`, `logout`, `whoami`, `connections`,
`invoke`, `artifact get`) is **for products**, running as their own macOS
service user (`zs-<product>`). It is **not for seats**: a seat reaches the
Broker through ZEO-RT's front door, with `zeo`. A product's service user can
keep its grant in a named file (`ZEOCORE_SESSION_FILE`, 0600 in its own 0700
directory), opt-in and never a fallback from the Keychain.

## ZEOconnect Broker contracts 1.0.0 to 1.4.0

The client conforms to Broker contract 1.0.0 and follows 1.2.0's expected-binding
fence, 1.2.1's changed-request marker and 1.2.2's re-enrolment code. From
1.4.0 it recognises the three access refusals, `door_required`,
`app_identity_unverified` and `no_principal`, in both of their forms, and says
how to get access back. A stop names its control and scope, and an old
protocol says to upgrade. `contracts/broker-1.4/` holds the request-digest parity
fixtures that ZEO-RT's Go door is checked against.

## The shared release component

- **Sign and verify** (`zeocore release sign | verify | pack | receive | send |
  serve`): one signing step for every org executable, internal Darwin
  releases first. Signing happens on matormini only, through a tailnet service
  that admits only reviewed, merged manifests. That service doesn't run against
  the key until the operator signs the code-signing ruling.
- **The PyPI adapter** (`zeocore release pypi plan | admit | apply | approve |
  reconcile | verify`) for a reviewed table of targets: zeocore, zeocreator and
  sovereign-agent. Under the operator's delegation, the `zeo-release` App
  creates the release tag and matorclawson approves the deployment once.
  `admit` refuses unless the two of them, together, are the only way to release.

## Also new

- **The merger**, a zeocore app: a port of ZEO-LEDGER's Go core, held to its
  parity fixtures.
- **GitHub App installation tokens** for a principal that holds its own key.
- **Revolut Business reads** for expenses, receipts and labels: an offline
  contract, with no live request made to Revolut.

## Breaking and deprecated

- `merge_pull_request` needs the head `sha`, and merges only by `merge`.
- The ZEOconnect client commands are deprecated for seats.

## Fixes

- **Hosted access:** it reaches ZEOconnect again. A Broker 503 is an outage, never a
  refusal or a stop, and a response without its protocol header is terminal.
- **YouTube:** retention no longer deletes data during a stop or an outage, and a
  job survives a kill mid-write.
- **The custody relay** no longer retries what no retry can cure.
- **A GitHub effect** is sent once and never retried.
- **`make setup` and CI** install exactly what `uv.lock` pins.

## Upgrade

```bash
uv tool install zeocore==0.15.0      # the command
uv pip install "zeocore==0.15.0"     # the library, pinned with hashes in your lock
```
