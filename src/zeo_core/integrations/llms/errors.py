"""Errors an LLM client raises that the outer execution policy must classify."""

from __future__ import annotations

from zeo_core.core.errors import ZeoIntegrationError


class DispatchedReplyError(ZeoIntegrationError):
    """The request was sent and answered, but the reply is not usable.

    An empty reply, one cut off at the token limit, or one that ended as a tool
    call. The provider did the work and may have billed for it, so the attempt
    DID start; it is a ``ZeoIntegrationError`` and not a ``ZeoApiError`` so that
    ``LLMClient.chat`` does not retry it and pay for the same reply again.
    """


__all__ = ["DispatchedReplyError"]
