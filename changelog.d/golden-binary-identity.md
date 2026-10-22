bump: patch

**Newsletter golden test: the renderer digest is pinned only for the recorded pandoc binary (zeocore-SOW-41).** `renderer_digest` names the pandoc binary's sha256, so the same pandoc version from a different build (the official 3.11 release on the ENG-UK host, against the Homebrew 3.11 bottle the golden was recorded with) is a different renderer by design. The test now compares the digest only when the binary matches the recorded one, and expects it to differ otherwise; the HTML and its sha256 are still compared whenever the version matches. Test-only; no behaviour change.
