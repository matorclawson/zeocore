bump: patch
Adds `kit/`, a Go module (standard library only) with the zeo-cli/v1 command kit's first slice: the grammar, exit table, RESULT line, the help, version, doctor and status verbs, and a conformance suite. It changes nothing in the Python package; every merge is a release, so this one is a patch. The kit's gate is `make kit-verify`, separate from `make verify`.
