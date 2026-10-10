"""The signing step's durable journal: an intent is on disk before codesign runs.

JSON lines, appended and ``fsync``ed, in a 0600 file held by one process at a
time. An intent with no later ``signed`` or ``failed`` record is an attempt
whose answer was lost. It is reconciled by reading the candidate back, never
by signing again (SOW-20 §4).
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

INTENT: Final = "intent"
SIGNED: Final = "signed"
FAILED: Final = "failed"
Action = Literal["intent", "signed", "failed"]


class JournalHeldError(RuntimeError):
    """Another signing process holds the journal: this one must not run."""


class SigningJournal:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._open: set[str] = set()
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise JournalHeldError(
                f"{path} is held by another signing process"
            ) from None
        self._file = os.fdopen(descriptor, "r+", encoding="utf-8")
        self._file.seek(0)
        for number, line in enumerate(self._file, start=1):
            try:
                record = json.loads(line)
                key, action = record["key"], record["action"]
                if not isinstance(key, str) or action not in (INTENT, SIGNED, FAILED):
                    raise ValueError
            except ValueError, KeyError, TypeError:
                self._file.close()
                raise ValueError(
                    f"{path} line {number}: unreadable journal record"
                ) from None
            self._track(key, action)

    def _track(self, key: str, action: str) -> None:
        if action == INTENT:
            self._open.add(key)
        else:
            self._open.discard(key)

    def append(self, key: str, action: Action, at: datetime) -> None:
        record = {"key": key, "action": action, "at": at.isoformat()}
        self._file.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())
        self._track(key, action)

    def is_open(self, key: str) -> bool:
        """Whether an attempt was journaled for ``key`` and never closed."""
        return key in self._open

    def close(self) -> None:
        self._file.close()
