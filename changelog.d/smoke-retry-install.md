bump: patch
**The release smoke test waits for the installer, not a web page.** 0.15.2's smoke test failed although PyPI's HTML index already listed its files: `uv` reads the JSON form of the index, which PyPI's CDN can serve stale for longer. The wait now asks `uv` itself, with a dry-run install retried for up to 10 minutes.
