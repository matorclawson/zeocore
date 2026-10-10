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
  files and any digest that differs.
- The service that carries packs to matormini is not built yet. It waits on
  the Architect's ruling on how a request is admitted (`zeocore#23#signing-admission`).

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
- the signed entitlements equal the manifest's file: none dropped, none added.

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
| `not_signed`, `identity_mismatch`, `wrong_audience`, `entitlements_mismatch`, `companion_unsigned`, `input_mismatch`, `signed_digest_mismatch`, `receipt_mismatch`, `receipt_invalid`, `entitlements_invalid`, `platform_signature_unadopted`, `legacy_policy_verify_only` | 20 | refused |

## Custody

Only the operator unlocks a keychain, grants key access, or changes ACLs,
partition lists or trust. The step reads key-access metadata and nothing else.
It never prints a key or a password.
