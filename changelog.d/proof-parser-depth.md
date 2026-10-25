bump: patch

Refuse excessively nested proof-report JSON with a generic ValueError instead of exposing RecursionError. The contract, schema and proof identity are unchanged.
