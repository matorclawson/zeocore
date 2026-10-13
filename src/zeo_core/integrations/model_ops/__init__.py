"""Inert model-operation contribution; no provider registration or execution."""

from .models import CataloguedModel, ModelCatalogue, ModelMessage, TextCompletionRequest
from .service import DraftModelOperationClient

__all__ = [
    "CataloguedModel",
    "DraftModelOperationClient",
    "ModelCatalogue",
    "ModelMessage",
    "TextCompletionRequest",
]
