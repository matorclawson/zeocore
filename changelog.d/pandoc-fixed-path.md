bump: minor
**`convert_newsletter` and `convert_decoded_message` take `pandoc_path` and `expected_pandoc_sha256`.** A caller can name the pandoc binary by absolute path and pin its sha256, so nothing is looked up on `PATH`. The binary is still copied, hashed and verified before it runs; a relative path is refused as `pandoc_missing` and another digest as `renderer_mismatch`.
