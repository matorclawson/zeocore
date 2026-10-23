bump: patch

Tests now pin the shape of the release component: it takes only `project`, `verify` and `build` (never a version, commit or ref), its only trigger is `workflow_call`, no release workflow can be started by hand (only the docs site can), and its three executor files are listed in `.github/release/README.md`, which also shows how another repository calls the component unchanged. No behaviour changes.
