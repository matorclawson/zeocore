"""The data-only UI card contract (``zeo.ui-result/v1``, zeocore-SOW-42)."""

from __future__ import annotations

from zeo_core.contracts.ui.card import SCHEMA_TAG, UiCardResult
from zeo_core.contracts.ui.widgets import (
    DataRef,
    WidgetBoard,
    WidgetRefused,
    WidgetUpsert,
)
from zeo_core.contracts.ui.wire import UiRefused, errors_of, validate_bytes

__all__ = [
    "SCHEMA_TAG",
    "UiCardResult",
    "UiRefused",
    "DataRef",
    "WidgetBoard",
    "WidgetRefused",
    "WidgetUpsert",
    "errors_of",
    "validate_bytes",
]
