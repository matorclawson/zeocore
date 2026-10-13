bump: patch
**`zeocore release tree-digest` runs on a plain install.** It only walks a directory, but it answered `extra_missing` without the `github` extra, because the whole `release` module was imported first. Its request check and its code now live in `cli/release_usage.py`, standard library only, and every other `release` verb still needs the extra and answers `extra_missing` as before.
