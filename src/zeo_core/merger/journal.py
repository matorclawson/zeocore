"""The merger's own durable journal, separate from the register.

JSON lines, appended and ``fsync``ed before ``append`` returns, so an intent
is on disk before any merge call. (Port of zeomerger internal/journal.)
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Protocol

from .decision import ATTEMPTED, Receipt


class Journal(Protocol):
    def append(self, receipt: Receipt) -> None:
        """Must not return until the record is durable."""
        ...

    def attempted(self, receipt_id: str) -> bool:
        """Whether an intent was ever journaled for the receipt id."""
        ...


class FileJournal:
    """One 0600 file. An unreadable line refuses to open the journal at all."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._attempted: set[str] = set()
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        self._file = os.fdopen(descriptor, "r+", encoding="utf-8")
        self._file.seek(0)
        for number, line in enumerate(self._file, start=1):
            try:
                record = json.loads(line)
                receipt_id = record["receipt_id"]
                if not isinstance(receipt_id, str) or not receipt_id:
                    raise ValueError
            except ValueError, KeyError, TypeError:
                self._file.close()
                raise ValueError(
                    f"{self._path} line {number}: unreadable journal record"
                ) from None
            if record.get("action") == ATTEMPTED:
                self._attempted.add(receipt_id)

    def append(self, receipt: Receipt) -> None:
        line = json.dumps(receipt.record(), separators=(",", ":")) + "\n"
        with self._lock:
            self._file.write(line)
            self._file.flush()
            os.fsync(self._file.fileno())
            if receipt.action == ATTEMPTED:
                self._attempted.add(receipt.receipt_id)

    def attempted(self, receipt_id: str) -> bool:
        with self._lock:
            return receipt_id in self._attempted

    def close(self) -> None:
        self._file.close()
