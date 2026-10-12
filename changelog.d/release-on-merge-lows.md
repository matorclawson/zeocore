bump: patch
**Release on merge, hardened.** A tag whose version never reached PyPI now stops the next release until its own Publish run is re-run, so a failed upload can't be skipped past and lose its notes. After every upload, the publish job checks that PyPI's files for the version are byte-for-byte this build's.
