"""The merger's decision core: one candidate against GitHub's live state.

A line-by-line port of zeomerger's ``Decide`` (ZEO-LEDGER owns the contract
and reviews this port). It reads only through ``GitHubReads`` and never
merges. Reasons keep the Go core's wording, because the parity fixtures
check them as substrings.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final, Protocol

from .queue import Candidate

# Actions.
MERGED: Final = "merged"
WOULD_MERGE: Final = "would-merge"  # plan mode: every condition holds, not live
REFUSED: Final = "refused"  # a condition failed
HELD: Final = "held"  # a condition could not be established
BLOCKED: Final = "blocked"  # an after entry has not merged yet
ATTEMPTED: Final = "attempted"  # the intent, journaled before the merge call
UNCLEAR: Final = "unclear"  # outcome unknown, and reconciliation couldn't settle it
DONE: Final = "already-handled"

# Base policies (QUEUE-SPEC-v2 §3).
CHECKS_ON_CURRENT_BASE: Final = "checks-on-current-base"
UNCHANGED_BASE: Final = "unchanged-base"

APPROVE_CONTEXT: Final = "architect/approve"


class MergeUnclearError(RuntimeError):
    """A merge call whose outcome is unknown. Never called again; reconciled."""


@dataclass(frozen=True)
class PullRequest:
    state: str  # "open" or "closed"
    merged: bool
    merge_commit: str
    draft: bool
    head_sha: str
    base_ref: str
    base_sha: str
    base_repo_id: int
    author_id: int
    mergeable_state: str  # GitHub's mergeable_state; "clean" means its rules pass
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class Status:
    context: str
    state: str
    description: str
    creator_id: int


@dataclass(frozen=True)
class Commit:
    sha: str
    author_id: int  # 0 when GitHub links no account
    committer_id: int


@dataclass(frozen=True)
class Repo:
    id: int
    base_policy: str


@dataclass(frozen=True)
class Config:
    repos: Mapping[str, Repo]
    architect_user_id: int
    role_app_ids: Mapping[int, str]  # role App account id -> seat
    merger_user_id: int
    merger_repo: str
    protected_paths: Sequence[str] = ()
    #: True only after QUEUE-SPEC-v2 §1's cutover is recorded; otherwise plan.
    live: bool = False


class GitHubReads(Protocol):
    """Everything the merger asks of GitHub. Only ``merge`` changes anything."""

    def pull_request(self, repo: str, number: int) -> PullRequest: ...

    def statuses(self, repo: str, sha: str) -> Sequence[Status]: ...

    def commits(self, repo: str, number: int) -> Sequence[Commit]: ...

    def vetoes(self) -> tuple[frozenset[str], str]:
        """The operator's veto list as ``owner/repo#N`` entries, and its revision."""
        ...

    def merge(self, repo: str, number: int, sha: str) -> str:
        """Merge only if the head is still ``sha``; return the merge commit.

        An outcome it can't establish raises ``MergeUnclearError``.
        """
        ...


@dataclass(frozen=True)
class Receipt:
    v: int
    repo: str
    pr: int
    head: str
    base_ref: str
    receipt_id: str
    register_line: int
    action: str = ""
    reasons: tuple[str, ...] = ()
    time: str = ""
    base_sha: str = ""
    merge_commit: str = ""
    veto_revision: str = ""
    approval_creator_id: int = 0

    def with_(self, **changes: object) -> Receipt:
        return replace(self, **changes)  # type: ignore[arg-type]

    def record(self) -> dict[str, object]:
        """The journal line, with the Go core's field names and omissions."""
        out: dict[str, object] = {
            "v": self.v,
            "time": self.time,
            "repo": self.repo,
            "pr": self.pr,
            "head": self.head,
            "base_ref": self.base_ref,
        }
        if self.base_sha:
            out["base_sha"] = self.base_sha
        out["receipt_id"] = self.receipt_id
        out["action"] = self.action
        if self.reasons:
            out["reasons"] = list(self.reasons)
        if self.merge_commit:
            out["merge_commit"] = self.merge_commit
        if self.veto_revision:
            out["veto_revision"] = self.veto_revision
        if self.approval_creator_id:
            out["approval_creator_id"] = self.approval_creator_id
        out["register_line"] = self.register_line
        return out


def short(value: str) -> str:
    return value[:8] if len(value) > 8 else value


def receipt_for(candidate: Candidate) -> Receipt:
    return Receipt(
        v=1,
        repo=candidate.repo,
        pr=candidate.pr,
        head=candidate.head,
        base_ref=candidate.base_ref,
        receipt_id=candidate.receipt,
        register_line=candidate.line,
    )


def decide(  # noqa: C901 -- line-by-line port of the Go core, for parity review
    config: Config, github: GitHubReads, candidate: Candidate
) -> Receipt:
    """The action the candidate allows now, without merging."""
    receipt = receipt_for(candidate)

    def refuse(why: str) -> Receipt:
        return receipt.with_(action=REFUSED, reasons=(*receipt.reasons, why))

    def hold(why: str) -> Receipt:
        return receipt.with_(action=HELD, reasons=(*receipt.reasons, why))

    if candidate.blocked:
        return receipt.with_(
            action=BLOCKED, reasons=("waits on " + ", ".join(candidate.blocked),)
        )
    repo = config.repos.get(candidate.repo)
    if repo is None:
        return refuse("repository not in scope")
    if repo.id != candidate.repo_id:
        return refuse(f"register repo_id {candidate.repo_id}, configured {repo.id}")
    if candidate.repo == config.merger_repo:
        return refuse("the merger's own repository")
    if repo.base_policy not in (CHECKS_ON_CURRENT_BASE, UNCHANGED_BASE):
        return refuse("no reviewed base policy for this repository")
    try:
        pr = github.pull_request(candidate.repo, candidate.pr)
    except Exception as error:  # noqa: BLE001 -- anything unreadable holds
        return hold(f"reading the PR: {error}")
    receipt = receipt.with_(base_sha=pr.base_sha)
    if pr.state != "open":
        return refuse(f"PR is {pr.state}")
    if pr.draft:
        return refuse("PR is a draft")
    if pr.head_sha != candidate.head:
        return refuse(f"live head {pr.head_sha}, approved {candidate.head}")
    if pr.base_ref != candidate.base_ref:
        return refuse(f"live base {pr.base_ref}, approved {candidate.base_ref}")
    if pr.base_repo_id != candidate.repo_id:
        return refuse(
            f"live repository id {pr.base_repo_id}, approved {candidate.repo_id}"
        )
    if pr.author_id == config.merger_user_id:
        return refuse("opened by the merger")
    if repo.base_policy == UNCHANGED_BASE and pr.base_sha != candidate.base_sha:
        return refuse(
            f"base moved from {short(candidate.base_sha)} to {short(pr.base_sha)},"
            " and this repository proves no checks on a new base"
        )
    for path in pr.files:
        for prefix in config.protected_paths:
            if path.startswith(prefix):
                return refuse(f"touches the merger's configuration: {path}")
    try:
        by = approval(config, github, candidate)
    except Exception as error:  # noqa: BLE001
        return hold(f"reading statuses: {error}")
    if by == 0:
        return refuse(
            f"no {APPROVE_CONTEXT} status by the adopted issuer carrying"
            f" {candidate.receipt} on this head"
        )
    receipt = receipt.with_(approval_creator_id=by)
    if pr.mergeable_state != "clean":
        return refuse(
            f'GitHub\'s rules do not pass: mergeable_state "{pr.mergeable_state}"'
        )
    try:
        vetoes, revision = github.vetoes()
    except Exception as error:  # noqa: BLE001
        return hold(f"reading the veto list: {error}")
    receipt = receipt.with_(veto_revision=revision)
    if f"{candidate.repo}#{candidate.pr}" in vetoes:
        return refuse("on the operator's veto list")
    try:
        commits = github.commits(candidate.repo, candidate.pr)
    except Exception as error:  # noqa: BLE001
        return hold(f"reading commits: {error}")
    if not commits:
        return hold("no commits to attribute")
    for commit in commits:
        for who in (commit.author_id, commit.committer_id):
            if who == config.architect_user_id:
                return refuse(f"commit {short(commit.sha)} is the Architect's own work")
            if who == 0 or not config.role_app_ids.get(who):
                return hold(
                    f"commit {short(commit.sha)} is not attributed to a seat's role App"
                )
    return receipt.with_(action=WOULD_MERGE)


def approval(config: Config, github: GitHubReads, candidate: Candidate) -> int:
    """The creator id of a qualifying architect/approve status on the head, or 0."""
    for status in github.statuses(candidate.repo, candidate.head):
        if (
            status.context == APPROVE_CONTEXT
            and status.state == "success"
            and status.creator_id == config.architect_user_id
            and candidate.receipt in status.description
        ):
            return status.creator_id
    return 0
