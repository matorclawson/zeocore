bump: patch
**CI and the release gate install a pinned, checksummed Pandoc.** CI for zeocore #116 timed out when the Ubuntu mirror served Pandoc at 54 kB/s. `tools/install-pandoc.sh` now fetches upstream's 3.1.3 `.deb` from GitHub's CDN, with bounded retries, and refuses it unless its sha256 matches. Both `ci.yml` and `publish.yml` use it. CI's job timeout also rises from 10 to 15 minutes.
