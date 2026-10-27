bump: patch
The release workflow's digest step now falls back to `shasum -a 256` where GNU `sha256sum` is missing, and the fallback-after-timeout subprocess test uses an attempt bound wide enough for a loaded host. No runtime behaviour changes.
