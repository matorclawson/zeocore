"""The merger, as a zeocore app (operator ruling, 2026-10-10).

It merges pull requests the Architect approved, under RULING-467 amendment 3
and the Architect's QUEUE-SPEC-v2, whose decision contract ZEO-LEDGER owns.
This package is a port of ZEO-LEDGER's Go core (zeomerger), held to its
language-neutral parity fixtures in ``tests/fixtures/merger-v1/``.

The merger reads the register, checks every condition against GitHub,
re-reads the register and the approval just before the call, journals its
intent durably, and merges with ``sha`` set to the approved head. It never
writes the register, never retries with another head, never makes a second
merge call for one receipt, and never guesses: anything it can't establish
refuses or holds the merge. Its key custody is its own, never ZEOconnect's.
"""

from .decision import (
    ATTEMPTED,
    BLOCKED,
    DONE,
    HELD,
    MERGED,
    NOT_SENT,
    REFUSED,
    UNCLEAR,
    WOULD_MERGE,
    Commit,
    Config,
    GitHubReads,
    MergeNotSentError,
    MergeUnclearError,
    PullRequest,
    Receipt,
    Repo,
    Status,
    decide,
)
from .journal import FileJournal, Journal, JournalHeldError
from .queue import Candidate, Register, RegisterError, parse_register
from .run import Pass

__all__ = [
    "ATTEMPTED",
    "BLOCKED",
    "DONE",
    "HELD",
    "MERGED",
    "NOT_SENT",
    "REFUSED",
    "UNCLEAR",
    "WOULD_MERGE",
    "Candidate",
    "Commit",
    "Config",
    "FileJournal",
    "GitHubReads",
    "Journal",
    "JournalHeldError",
    "MergeNotSentError",
    "MergeUnclearError",
    "Pass",
    "PullRequest",
    "Receipt",
    "Register",
    "RegisterError",
    "Repo",
    "Status",
    "decide",
    "parse_register",
]
