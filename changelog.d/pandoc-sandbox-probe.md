bump: patch

The pandoc sandbox probe no longer fails on pandoc 3.10. It now requires what was
measured on 3.10 and 3.11: no embedded data, and either a non-zero exit or pandoc's
"Could not fetch resource" warning, instead of a version-gated non-zero exit.

sow: zeocore-SOW-44
