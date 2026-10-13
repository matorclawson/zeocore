"""Inert model-operation contribution; no provider registration or execution."""

from .models import ModelCatalogue, ModelMessage, TextCompletionRequest
from .service import DraftModelOperationClient

__all__ = [
    "DraftModelOperationClient",
    "ModelCatalogue",
    "ModelMessage",
    "TextCompletionRequest",
]
