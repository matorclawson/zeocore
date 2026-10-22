bump: patch
**`zeo.ui-result/v1` schema: text facts carry the class D pattern in the exported JSON Schema (zeocore-SOW-42).** The exported schema now refuses control characters in a `text` fact value as the validator does; every bad-fact vector is now asserted by its exact refusal code.
