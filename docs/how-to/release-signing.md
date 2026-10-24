# Sign and verify a release

`zeocore release sign` and `zeocore release verify` are the org's single
signing step (ZEOCORE-SOW-19 to SOW-22). A seat never writes its own signer.

## What is built

| | internal | distributed |
|---|---|---|
| **darwin** | built: "ZEO Runtime Code Signing", leaf SHA-1 `093E02C5…` | held, `notarisation_unbuilt` |
| **linux** | refused, `platform_signature_unadopted` | refused, `platform_signature_unadopted` |

The legacy policies are verify-only. `sign` refuses them, and `verify` holds
them as `legacy_policy_unpinned` until their exact pins are recorded.

## The manifest

Inputs come from the `signing` section of the reviewed `release.yaml`, never
from flags. Paths are relative to the manifest.

```yaml
signing:
  release_manifest: 1
  product: pomar
  executables:
    - name: pomar-host
      path: bin/pomar-host
      unsigned_sha256: "…"          # from the build's receipt
      identifier: ac.zeo.pomar.pomar-host
      audience: internal
      platform: darwin
      policy: current
      entitlements:                 # or: none
        path: entitlements/pomar-host.plist
        sha256: "…"
```

A current binding's identifier is always `ac.zeo.<product>.<name>`.

## Bundles

`release_manifest: 2` adds `bundles` beside `executables`: a macOS `.app`,
signed whole and inside-out (ZEOCORE-SOW-32). A bundle is for the internal
audience only. Version 1 manifests are unchanged.

```yaml
signing:
  release_manifest: 2
  product: zeoapp
  bundles:
    - name: zeoapp
      path: app/build/Zeo.app
      bundle_identifier: org.zeroemployee.zeo.operator   # Info.plist's CFBundleIdentifier
      identifier: org.zeroemployee.zeo.operator          # equals it, for the outer bundle only
      audience: internal
      platform: darwin
      policy: current
      entitlements: none
      unsigned_tree_sha256: "…"     # zeocore release tree-digest, at build time
      main_executable:              # Info.plist's CFBundleExecutable; or: none
        path: Contents/MacOS/ZEOPlayground
        unsigned_sha256: "…"
      nested:                       # every piece of code, by its path inside the .app
        - path: Contents/Frameworks/libZEOConsoleRenderer.dylib
          kind: macho
          unsigned_sha256: "…"      # the file's sha256
          identifier: ac.zeo.zeoapp.console-renderer
          entitlements: none
        - path: Contents/Resources/SwiftTerm_SwiftTerm.bundle
          kind: bundle              # a codeless resource bundle: still signed
          unsigned_sha256: "…"      # a nested bundle's subtree tree digest
          identifier: ac.zeo.zeoapp.swiftterm-resources
          entitlements: none
          main_executable: none
```

- **The outer bundle keeps its own identifier.** `identifier` equals
  `bundle_identifier`, the one reviewed exception to
  `ac.zeo.<product>.<name>`. Nested code keeps the rule, and its name, which
  names its receipt, is the identifier's last part.
- **The tree digest.** `zeocore release tree-digest < {"path": "app/build/Zeo.app"}`
  prints `unsigned_tree_sha256`: the sha256 of one line per entry, sorted by
  path as UTF-8 bytes, each `path TAB type TAB mode TAB value LF`. The path is
  relative and UTF-8; the type is `file`, `dir` or `symlink`; the mode is
  `755` for a directory or a file with any execute bit, `644` otherwise, `-`
  for a link; the value is the file's sha256, the link's target, or `-`. A
  path or target with a control character, a backslash or bytes that aren't
  UTF-8 is refused, so nothing is escaped. So are a fifo, socket or device, a
  file with more than one hard link, an entry with the setuid, setgid or
  sticky bit, and one with any BSD file flag but transparent compression.
  Extended attributes, owners and times aren't part of it: xattrs are neither
  described nor sealed by the tree digest (codesign seals the resources), and
  the signing copy carries only each file's bytes and its pack mode. The pack and the step
  compute it with the same code.
- **Links** are allowed only where a framework needs them: its
  `Versions/Current`, naming one version beside it, and a link at its top
  whose target starts `Versions/`. Every hop of the resolution, through any
  other link, stays inside that framework. Anything else, an absolute
  target, a `..` that leaves it, a dangling link or a loop, refuses the
  bundle as `bundle_unsafe`. The walk and the copy never follow a link, the
  check runs on the unsigned original, on the copy before signing and at
  every verify, and a listed path or main executable with a link in it is
  `bundle_unsafe`: codesign is never pointed through one.
- **Every piece of code is listed.** Before anything is signed, the step finds
  every Mach-O file by its magic number, every other file with an execute bit,
  and every nested `.app`, `.framework`, `.bundle`, `.xpc`, `.appex`,
  `.plugin`, `.kext`, `.dext` or `.systemextension`. One that isn't listed is
  `nested_unlisted`. Each item's `kind` is `macho` or `bundle`, and an item
  that isn't that kind is `nested_missing`, so an executable script can't be
  listed and refuses the bundle.
- **A bundle's main executable is its own.** `main_executable` is declared and
  hashed, for the outer bundle and for each nested bundle, and is never a
  nested item: listing it is refused. Its path is the file Info.plist's
  `CFBundleExecutable` names, relative to its bundle: `Contents/MacOS/<name>`,
  or a framework's binary's real path, such as `Versions/A/<name>`. Info.plist
  with a `CFBundleExecutable` needs a matching declaration, of a Mach-O file;
  without one, the declaration is `none`. Anything else is
  `main_executable_invalid`. It is signed by its bundle's own codesign call
  and verified after it, under the bundle's identifier.
- **A codeless resource bundle** (`kind: bundle`, `main_executable: none`) is
  listed and signed like any nested bundle, with `--identifier`. Its own
  `CFBundleIdentifier` is kept and isn't checked; only the outer bundle's
  `bundle_identifier` is checked against its Info.plist.
- **Signing** copies the bundle to `work_dir/signed/<name>/`, keeping links,
  signs the nested items deepest first, then the outer bundle. It never uses
  `--deep`, hardened runtime or a timestamp. Each nested item gets a receipt,
  and the bundle a receipt naming the signed tree's digest and the sha256 of
  every nested receipt. A bundle is one unit: a clean failure closes every
  part of it, so the next run signs a fresh copy; a lost answer is
  reconciled by reading the whole bundle back.
- **Admission** counts the bundle once and each nested item once against the
  16-item limit (a main executable doesn't count). Nested identifiers must be
  among the grant's `ac.zeo.<product>.<name>` identifiers, and the outer
  bundle's among its `bundle_identifiers`: the only identifiers outside
  `ac.zeo.`, each granted to exactly one product. An outer identifier in
  another product's `ac.zeo.` namespace is refused in the manifest.
- **No hardened runtime** for internal signing, so library validation is off:
  a process running as the same user can load a dylib into the app, and it
  then acts with the app's TCC grants and its connections. The signature
  shows what was released; it doesn't stop a same-user process. That user
  boundary is the control for internal releases.

## Running it

```console
$ export ZEOCORE_SIGNING_HOST=mac-mini      # this host's `hostname -s`, lower case
$ echo '{"manifest": "release.yaml", "work_dir": "/path/to/work"}' | zeocore release sign
$ echo '{"manifest": "release.yaml", "work_dir": "/path/to/work"}' | zeocore release verify
```

- `sign` runs only on the configured signing host. Anywhere else it holds as
  `signing_host_mismatch`. It refuses the whole release before any signer is
  used if an input's digest differs. It never signs the unsigned original: it
  signs a copy in `work_dir/signed/`, and writes `work_dir/receipts/sign-<name>.json`
  once.
- `verify` is read-only and runs on any host. A release counts as signed only
  when `verify` passes.

## Signing from another host

The operator's decision: signing happens on matormini only, and no second copy
of the key is made. A build host sends a request pack there and gets a
response pack back (ZEOCORE-SOW-23).

```console
$ echo '{"manifest": "release.yaml", "out": "request.tar",
        "repo": "owner/repo", "commit": "<40-hex merged commit>",
        "manifest_path": "release.yaml"}' | zeocore release pack
$ echo '{"manifest": "release.yaml", "response": "response.tar",
        "work_dir": "/path/to/new/dir"}' | zeocore release receive
```

- `pack` holds `request.json` (where the manifest was reviewed), the manifest,
  and every file it lists, and nothing else. It is deterministic, and its
  sha256 is the request id.
- `receive` unpacks the signed candidates and their receipts into an empty
  directory, then runs `verify` on this host. A release counts as signed only
  when that passes.
- Opening a pack refuses links, absolute paths, `..`, duplicates, unlisted
  files and any digest that differs. A link is allowed only inside a listed
  bundle, under the bundle's link rules. In a response, `signed/<name>/` and
  the signed bundle must be real directories inside the work directory,
  never links.

### The service on matormini

```console
$ export ZEOCORE_SIGNING_HOST=mac-mini
$ echo '{"listen": "100.x.y.z:7744", "state_dir": "/path/to/state"}' | zeocore release serve
```

On the build host:

```console
$ echo '{"pack": "request.tar", "service": "100.x.y.z:7744", "out": "response.tar"}' \
    | zeocore release send
```

The service listens only on a tailnet `100.x` address and takes the caller's
node from `tailscale whois`. Sending the same pack again is safe: a signed
request gets the same answer and is never signed twice. It admits a request
only when every condition of the admission rule (ZEOCORE-SOW-24 §1) holds,
before any signer is used:

- the caller's node may ask for that product, on zeocore's reviewed allowlist;
- matormini reads the manifest from GitHub at the request's commit itself,
  and it is byte-identical; the commit is on the default branch; and the repo
  comes from the allowlist;
- every file matches the manifest, and no unlisted file is accepted;
- every binding is internal: the Developer ID is never signed unattended;
- every identifier is one declared for that product on that node;
- at most 512 MiB, 16 executables, and 12 requests per node per hour.
  `ZEOCORE_SIGNING_MAX_EXECUTABLES` and `ZEOCORE_SIGNING_MAX_REQUESTS_PER_HOUR`
  can tighten the last two, never loosen them.

Every request, admitted or refused, is journaled in `state_dir/requests.jsonl`.
A locked keychain is held as `signing_keychain_locked`, and is never unlocked.
Only a signed answer is stored: a refusal is worked out again, so a manifest
refused as unreviewed is admitted once its commit merges.

The rule is the operator's to sign. The service does not run against the
ZEO Runtime key until the code-signing ruling that carries it is signed.

## Digests from a reviewed build inventory

By default the manifest pins every digest it signs, and a person writes the new
digests into `release.yaml` for each release. With `pins: inventory` in the
`signing` section the manifest keeps only the reviewed structure (paths,
identifiers, audience, entitlements) and carries no digest; the digests come
from an inventory file made after the build.

```yaml
signing:
  release_manifest: 2
  pins: inventory
  product: pomar
  # executables / bundles as usual, without any unsigned_*sha256 field
```

The inventory is JSON, schema `zeo.build-inventory/v1`, with the product and
one digest for every pin the manifest needs. Its keys are exact:
`executable:<name>`, `bundle:<name>:tree`, `bundle:<name>:main`,
`bundle:<name>:nested:<path>` and `bundle:<name>:nested:<path>:main`. A key
the manifest does not need, a key it needs and the inventory lacks, an inventory
for another product, and a pin left in the manifest all refuse. A manifest that
pins its own digests refuses an inventory, so there is never a second source of
pins. After loading, the result is the same fully pinned manifest the step has
always signed: nothing downstream changes.

The inventory is a file in the owner repository, `release/inventory/<version>.json`,
and goes through an ordinary pull request. It is trusted only as far as the
evidence for it. The signing service reads GitHub itself and admits the request
only when the commit is on the default branch, the commit's own tree holds
exactly these bytes at the path the request names (`inventory_path`), and the
latest `council/approve` status on that commit was created by the adopted
approver (the App's bot user id, not any account that happens to use the same
context name) and is a success. A later failure supersedes an earlier success.
Anything else is `inventory_unreviewed` (refused), and a GitHub that cannot be
read, or a reader that cannot read approvals, is `review_unreadable` (held).
The review is of the digests against the head-bound build receipt, not a
rebuild.

On the command line, `sign`, `verify` and `receive` take an optional `inventory`
(the file), and `pack` takes `inventory` and `inventory_path` (where it sits in
the repository at `commit`). The pack carries the inventory as `inventory.json`
beside the manifest. A request with neither is byte for byte what it always was.

### The release event after the seal

The release event (`zeo.release-event/v1`, made after signing from the signed
hashes) is authenticated, if at all, after the seal and never before it.
`zeo_core.release.signing.event_auth` is the verifier side of that step. It
refuses unless the seal's `verify` passed, unless the event is the unchanged
canonical document (the codec of `.github/release/event.py`, with exactly its
fields and a digest that matches its payload), and unless whatever
authenticated it authenticated exactly that payload.

It does not sign. The producer's only adopted authentication is a code
signature and the tailnet identity of the caller; it has none for a JSON
payload, and using a signing key for one is a new key purpose that belongs to
the operator. Until a mechanism is adopted the default, `Unadopted`, answers
`held` with `event_authentication_unadopted`. No key, service or second signer
is added here.

## What verify checks

A valid Apple signature alone is not enough. For every executable:

- the signed file's sha256 matches its sign receipt, and the unsigned original
  (if present) still matches the manifest;
- the signature is explicit and names both the identifier and the certificate:
  a bare cdhash, an ad-hoc signature, or one with no leaf certificate is
  `not_signed`, even when its requirement text names the pin;
- the leaf certificate's SHA-1 is the audience's pin. The other audience's
  certificate is `wrong_audience`; any other certificate is `identity_mismatch`;
- the designated requirement is exactly the binding's, and `codesign --verify
  --strict -R` accepts the file against it. Only codesign's exit 3 is a
  non-match; a syntax or tool error holds as unreadable;
- the signed entitlements equal the manifest's file exactly: none dropped,
  none added. `none` means no entitlements blob at all, so an empty
  entitlements file is refused as `entitlements_invalid`: declare `none`.

For a bundle, each nested item is checked like that, then the outer bundle
with the same checks (its identifier is `bundle_identifier`, which must
still be Info.plist's `CFBundleIdentifier`), then each main executable under
its bundle's identifier and entitlements, and then `codesign --verify --strict --deep` on the whole: `--deep` verifies
every nested signature and the sealed resources, and is used only to verify.
The signed tree's digest must match the bundle's receipt, each nested item's
digest its own receipt, and the bundle's receipt must name each nested
receipt's sha256. The tree is walked again, so code added after signing is
refused.

The bundle's verify receipt also carries `spctl`, the `status` and `output` of
`spctl --assess --type execute -vv`. It is information only: an internal
bundle isn't notarised, so Gatekeeper rejecting it is expected, and its answer
never makes a pass or a failure.

The link between the signed file and the unsigned original is the receipt
chain. Stripping both signatures does not give equal bytes, so a byte-level
link isn't claimed.

## Outcomes

The exit family is zeocore's: 0 done; 2 invalid; 11 waiting; 13 ambiguous; 20
held or refused.

| Outcome | Exit | Meaning |
|---|---|---|
| `signed`, `verified` | 0 | done |
| `signing_keychain_locked` | 11 | "unlock the login keychain". Reported only when the keychain's settings say so, never guessed from a codesign error |
| `signing_attempt_unreconciled` | 13 | an attempt's answer was lost. Nothing is signed again until the candidate is reconciled |
| `signing_identity_missing`, `signing_failed_unknown`, `journal_unreadable`, `signing_host_*`, `notarisation_unbuilt`, `legacy_policy_unpinned`, `verify_unreadable`, `signing_in_progress` | 20 | held |
| `inventory_unreviewed` | 20 | refused: the inventory is not the reviewed one (unmerged, not in the commit's tree, differs from it, or no approval by the adopted approver) |
| `not_signed`, `identity_mismatch`, `wrong_audience`, `entitlements_mismatch`, `companion_unsigned`, `input_mismatch`, `signed_digest_mismatch`, `receipt_mismatch`, `receipt_invalid`, `entitlements_invalid`, `platform_signature_unadopted`, `legacy_policy_verify_only`, `bundle_unsafe`, `nested_unlisted`, `nested_missing`, `main_executable_listed`, `main_executable_invalid`, `bundle_identifier_mismatch` | 20 | refused |

## Custody

Only the operator unlocks a keychain, grants key access, or changes ACLs,
partition lists or trust. The step reads key-access metadata and nothing else.
It never prints a key or a password.
