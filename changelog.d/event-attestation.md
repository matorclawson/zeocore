bump: minor

Add `zeocore release verify-event`, which authenticates a published `release-event.json` by its GitHub attestation: the certificate must name `publish.yml` of `zeroemployeeorg/zeocore` on `main`, a push trigger, a GitHub-hosted runner and the event's commit, and the attested subject must be these bytes. It requires an expected tag or commit, answers `authenticated`, `refused` or `held`, and never treats a check it could not complete as authenticated. Authenticity is not release authorization: `check_release_approval` is a separate check, which reads the council's approval at the pull request head of the release's merge commit. `publish.yml` gains an `attest-event` job that attests the uploaded event. The signing path's `authenticate_event` is unchanged and still held.

sow: zeocore-SOW-44
