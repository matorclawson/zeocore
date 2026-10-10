"""The merger's own durable journal, separate from the register.

JSON lines, appended and ``fsync``ed before ``append`` returns, so an intent
is on disk before any merge call. (Port of zeomerger internal/journal.)
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path
from typing import Protocol

from .decision import ATTEMPTED, NOT_SENT, Receipt


class Journal(Protocol):
    def append(self, receipt: Receipt) -> None:
        """Must not return until the record is durable."""
        ...

    def attempted(self, receipt_id: str) -> bool:
        """Whether an intent is journaled for the receipt id and not released.

        Only a later ``not-sent`` record releases an intent: the call provably
        never left the host.
        """
        ...


class JournalHeldError(RuntimeError):
    """Another merger process holds the journal: this one must not run."""


class FileJournal:
    """One 0600 file, held by exactly one process at a time.

    Opening takes an exclusive, non-blocking ``flock`` and refuses if another
    process holds it, so two merger runs can never each make one call for the
    same receipt (Architect M2). An unreadable line refuses to open the journal.
    The lock is released by ``close`` or when the process exits.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._attempted: set[str] = set()
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise JournalHeldError(
                f"{self._path} is held by another merger process"
            ) from None
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
            elif record.get("action") == NOT_SENT:
                self._attempted.discard(receipt_id)

    def append(self, receipt: Receipt) -> None:
        line = json.dumps(receipt.record(), separators=(",", ":")) + "\n"
        with self._lock:
            self._file.write(line)
            self._file.flush()
            os.fsync(self._file.fileno())
            if receipt.action == ATTEMPTED:
                self._attempted.add(receipt.receipt_id)
            elif receipt.action == NOT_SENT:
                self._attempted.discard(receipt.receipt_id)

    def attempted(self, receipt_id: str) -> bool:
        with self._lock:
            return receipt_id in self._attempted

    def close(self) -> None:
        self._file.close()
