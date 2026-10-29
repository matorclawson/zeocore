bump: minor
**YouTube publish jobs can read their bytes from a staging store.** A job may name a staged object (store label, key, version) instead of a local file; the executor takes an injected `object_reader`, checks the stored sha256 and re-hashes the object once, and refuses on any mismatch. Local-file jobs behave as before.
