"""The real GitHub reader for the release-approval check (SOW-44 rev 2, W3).

``check_release_approval`` takes a reader with four reads. This is that reader,
over ``gh api``: read-only, no token handled here, nothing written. A read that
cannot complete raises ``GitHubReadError``; the check turns that into "not
approved", never into approval.

The release commit is the merge commit the merger made. The council's
``council/approve`` status is on the pull request's HEAD, so the reader finds
the one pull request that was merged AS that commit: its ``merge_commit_sha``
must equal the commit, not merely mention it, it must have been merged into the
default branch, and there must be exactly one.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Final

from .event_attestation import GhRunner, GitHubReadError, MergedPull
from .inventory import Status

_API: Final = ("gh", "api")


class GhApiReader:
    """``MergeReads`` over ``gh api`` for one runner (``gh`` by default)."""

    def __init__(self, run: GhRunner) -> None:
        self._run = run

    def _get(self, path: str, *extra: str) -> Any:  # noqa: ANN401 -- parsed GitHub JSON, shape checked by each caller
        argv: Sequence[str] = (*_API, path, *extra)
        try:
            result = self._run(argv)
        except OSError as error:
            raise GitHubReadError(f"gh could not be run: {error}") from None
        if result.code != 0:
            first = (result.stderr.strip().splitlines() or ["gh failed"])[0]
            raise GitHubReadError(first)
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise GitHubReadError("gh printed no readable JSON") from None

    def default_branch(self, repo: str) -> str:
        body = self._get(f"repos/{repo}")
        name = body.get("default_branch") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name:
            raise GitHubReadError("the repository has no default branch name")
        return name

    def is_ancestor(self, repo: str, commit: str, branch: str) -> bool:
        body = self._get(f"repos/{repo}/compare/{commit}...{branch}")
        status = body.get("status") if isinstance(body, dict) else None
        if status in ("ahead", "identical"):
            return True
        if status in ("behind", "diverged"):
            return False
        raise GitHubReadError("the comparison has no readable status")

    def repo_id(self, repo: str) -> int:
        body = self._get(f"repos/{repo}")
        identifier = body.get("id") if isinstance(body, dict) else None
        if not isinstance(identifier, int) or isinstance(identifier, bool):
            raise GitHubReadError("the repository has no numeric id")
        return identifier

    def merged_pull(self, repo: str, commit: str) -> MergedPull | None:
        """The one DISTINCT pull request merged as ``commit`` into the default
        branch, read over every page; None unless there is exactly one."""
        branch = self.default_branch(repo)
        pages = self._get(
            f"repos/{repo}/commits/{commit}/pulls?per_page=100", "--paginate", "--slurp"
        )
        if not isinstance(pages, list):
            raise GitHubReadError("the commit's pull requests are not a list of pages")
        found: dict[int, MergedPull] = {}
        for page in pages:
            if not isinstance(page, list):
                raise GitHubReadError(
                    "a page of the commit's pull requests is not a list"
                )
            for pull in page:
                candidate = self._qualifying(pull, commit, branch)
                if candidate is not None:
                    found.setdefault(candidate.number, candidate)
        if len(found) != 1:
            return None
        (only,) = found.values()
        return only

    @staticmethod
    def _qualifying(pull: object, commit: str, branch: str) -> MergedPull | None:
        """The pull request if it was merged into ``branch`` AS ``commit``."""
        if not isinstance(pull, dict):
            return None
        base, head = pull.get("base"), pull.get("head")
        number, merged_at = pull.get("number"), pull.get("merged_at")
        if not (
            isinstance(base, dict)
            and base.get("ref") == branch
            and isinstance(head, dict)
            and isinstance(head.get("sha"), str)
            and isinstance(number, int)
            and not isinstance(number, bool)
            and isinstance(merged_at, str)
            and merged_at
            and pull.get("merge_commit_sha") == commit
        ):
            return None
        return MergedPull(number, head["sha"], merged_at)

    def approval_statuses(self, repo: str, commit: str, context: str) -> list[Status]:
        """``context``'s statuses on ``commit``, newest first."""
        pages = self._get(
            f"repos/{repo}/commits/{commit}/statuses?per_page=100",
            "--paginate",
            "--slurp",
        )
        if not isinstance(pages, list):
            raise GitHubReadError("the commit's statuses are not a list")
        found: list[Status] = []
        for page in pages:
            for item in page if isinstance(page, list) else [page]:
                if isinstance(item, dict) and item.get("context") == context:
                    creator = item.get("creator")
                    identifier = (
                        creator.get("id") if isinstance(creator, dict) else None
                    )
                    description, created = (
                        item.get("description"),
                        item.get("created_at"),
                    )
                    found.append(
                        Status(
                            str(item.get("state")),
                            identifier if isinstance(identifier, int) else None,
                            description if isinstance(description, str) else None,
                            created if isinstance(created, str) else None,
                        )
                    )
        return found


__all__ = ["GhApiReader"]
