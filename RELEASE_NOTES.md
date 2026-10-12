# zeocore 0.15.1

A patch release for 0.15.0. What 0.15.0 shipped is in its
[release notes](https://github.com/zeroemployeeorg/zeocore/releases/tag/v0.15.0).

## Fixed

- **The `zeocore` command runs on a plain install.** In 0.15.0,
  `uv tool install zeocore` and `pip install zeocore` installed a command that
  crashed at start-up, because two of its modules came only with extras. The
  library itself imported fine.
  - `rfc8785`, which the canonical digest needs, is now a base dependency.
  - `zeocore release` loads only when it is used. Without the `github` extra
    it answers `extra_missing` (exit 20) and names the extra.
  - The publish smoke test now runs the command on the plain install.

  On 0.15.0, the workaround is `pip install 'zeocore[runtime-host,github]==0.15.0'`.

## Changed

- **Every publish goes through the protected `pypi` environment.** The
  workflow's TestPyPI target, which had no protection, is gone.

## Upgrade

```bash
uv tool install zeocore==0.15.1      # the command
uv pip install "zeocore==0.15.1"     # the library, pinned with hashes in your lock
```
