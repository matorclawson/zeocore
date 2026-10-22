bump: patch

`run_native` names why the provider rejected a call. A failed `NativeTurn` with
`error_code="provider_error"` now carries `provider_detail`, built only from the
error's own short identifiers: `http_400 code=model_not_found param=agent.model
type=invalid_request_error`. Each part must be a short plain token or it is dropped; the
provider's message is never included, and neither is a key or a URL.
