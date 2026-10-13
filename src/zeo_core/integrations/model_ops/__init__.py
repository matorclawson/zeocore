"""Inert model-operation contribution; no provider registration or execution."""

from .models import CataloguedModel, ModelCatalogue, ModelMessage, TextCompletionRequest
from .receipts import (
    AmbiguousModelReceiptError,
    ModelReceipt,
    ParsedModelResponse,
    parse_model_response,
)
from .service import DraftModelOperationClient

__all__ = [
    "AmbiguousModelReceiptError",
    "CataloguedModel",
    "DraftModelOperationClient",
    "ModelCatalogue",
    "ModelMessage",
    "ModelReceipt",
    "ParsedModelResponse",
    "parse_model_response",
    "TextCompletionRequest",
]
