"""One merger pass over the register. (Port of zeomerger ``Pass``.)

Every decision and outcome is journaled. When live, an allowed candidate is
re-checked just before the call (the register and the approval are read
again), its intent is journaled durably, and exactly one merge call is made
with ``sha`` set to the approved head. A lost answer is reconciled by reading
the PR, never by calling again; a receipt with a prior intent is only ever
reconciled.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from .decision import (
    ATTEMPTED,
    DONE,
    MERGED,
    REFUSED,
    UNCLEAR,
    WOULD_MERGE,
    Config,
    GitHubReads,
    MergeUnclearError,
    Receipt,
    approval,
    decide,
    receipt_for,
    short,
)
from .journal import Journal
from .queue import Candidate, Register


class PassError(RuntimeError):
    """The pass stopped: the register is unreadable or a record isn't durable."""


@dataclass
class Pass:
    config: Config
    github: GitHubReads
    journal: Journal
    #: Reads the register afresh: at the start, and again just before each call.
    load: Callable[[], Register]
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def run(self) -> list[Receipt]:  # noqa: C901 -- the Go Pass.Run, step for step
        try:
            register = self.load()
        except Exception as error:  # noqa: BLE001
            raise PassError(f"reading the register: {error}") from None
        out: list[Receipt] = []

        def record(receipt: Receipt) -> None:
            stamped = receipt.with_(
                time=self.now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            try:
                self.journal.append(stamped)
            except Exception as error:  # noqa: BLE001
                raise PassError(f"journaling {receipt.receipt_id}: {error}") from None
            out.append(stamped)

        for candidate in register.candidates():
            if self.journal.attempted(candidate.receipt):
                # One merge call per receipt, ever: reconcile by reading.
                record(
                    self._reconcile(
                        candidate, "a merge was already attempted for this receipt"
                    )
                )
                continue
            receipt = decide(self.config, self.github, candidate)
            if receipt.action != WOULD_MERGE or not self.config.live:
                record(receipt)
                continue
            # sha=head fences the source; this fences the approval.
            why = self._recheck(candidate)
            if why:
                record(receipt.with_(action=REFUSED, reasons=(why,)))
                continue
            # No durable intent, no merge call: record() raises first.
            record(receipt.with_(action=ATTEMPTED))
            try:
                commit = self.github.merge(candidate.repo, candidate.pr, candidate.head)
            except MergeUnclearError as error:
                outcome = self._reconcile(candidate, str(error))
            except Exception as error:  # noqa: BLE001
                outcome = receipt.with_(
                    action=REFUSED,
                    reasons=(*receipt.reasons, f"merge call refused: {error}"),
                )
            else:
                outcome = receipt.with_(action=MERGED, merge_commit=commit)
            record(outcome)
        return out

    def _recheck(self, candidate: Candidate) -> str:
        try:
            register = self.load()
        except Exception as error:  # noqa: BLE001
            return f"re-reading the register: {error}"
        still = any(
            other.receipt == candidate.receipt and not other.blocked
            for other in register.candidates()
        )
        if not still:
            return "the approval is no longer the PR's last ready event"
        try:
            by = approval(self.config, self.github, candidate)
        except Exception:  # noqa: BLE001
            by = 0
        if by == 0:
            return "the approval status is gone or unreadable"
        return ""

    def _reconcile(self, candidate: Candidate, why: str) -> Receipt:
        receipt = receipt_for(candidate).with_(reasons=(why,))
        try:
            pr = self.github.pull_request(candidate.repo, candidate.pr)
        except Exception as error:  # noqa: BLE001
            return receipt.with_(
                action=UNCLEAR, reasons=(*receipt.reasons, f"reconciling: {error}")
            )
        if pr.merged and pr.head_sha == candidate.head:
            return receipt.with_(
                action=DONE,
                merge_commit=pr.merge_commit,
                reasons=(*receipt.reasons, "reconciled: merged at the approved head"),
            )
        if pr.merged:
            return receipt.with_(
                action=DONE,
                merge_commit=pr.merge_commit,
                reasons=(
                    *receipt.reasons,
                    "reconciled: merged, but at head " + short(pr.head_sha),
                ),
            )
        return receipt.with_(
            action=UNCLEAR,
            reasons=(
                *receipt.reasons,
                "reconciled: not merged; no second call is made for this receipt",
            ),
        )


def count(receipts: list[Receipt]) -> dict[str, int]:
    tally: dict[str, int] = {}
    for receipt in receipts:
        tally[receipt.action] = tally.get(receipt.action, 0) + 1
    return tally
