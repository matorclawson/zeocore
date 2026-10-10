"""The merger's GitHub adapter: ``GitHubReads`` over GitHub's REST API.

A port of zeomerger's internal/github client, under ZEO-LEDGER's adapter rules
(its review of the port, SOW-151):

- the token is a single-repository installation token from a ``TokenSource``
  (``zeo_core.integrations.github.app_tokens``), never a broader one;
- the origin is fixed, redirects are never followed, every body is bounded to
  8 MiB, and every call has a timeout;
- a PR's changed files include ``previous_filename``, so a rename out of a
  protected path is seen; more than 3000 files or 250 commits holds, because
  GitHub truncates there;
- a missing or unreadable veto list holds;
- the merge body is ``sha`` plus ``merge_method: merge``. A 5xx, a dropped
  connection or an answer without ``merged: true`` is unclear; refused is a
  4xx only. It is sent once, never retried. A call that provably sent nothing
  (no token, or the connection was never made) is not sent, and may be
  decided again on a later pass.
- the base branch's required checks are read from its rulesets only. Classic
  branch protection reads as "no required checks", so a repository protected
  that way is refused: it fails closed, as the generated-rulesets policy wants.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from typing import Any, Final, Protocol
from urllib.parse import quote

import httpx

from .decision import (
    Commit,
    MergeNotSentError,
    MergeUnclearError,
    PullRequest,
    Status,
)

GITHUB_API: Final = "https://api.github.com"
MAX_BODY: Final = 8 << 20
_REPO: Final = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class TokenSource(Protocol):
    def token(self, repository: str) -> str:
        """A single-repository installation token for ``repository``."""
        ...


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int = 0, *, sent: bool = True) -> None:
        self.status = status
        #: False only when provably no byte of the request reached GitHub.
        self.sent = sent
        super().__init__(message)


def _repo_path(repo: str) -> str:
    if not _REPO.fullmatch(repo):
        raise GitHubError(f"bad repository {repo!r}")
    owner, name = repo.split("/", 1)
    return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"


class GitHubClient:
    def __init__(
        self,
        *,
        tokens: TokenSource,
        veto_repo: str,
        veto_path: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._tokens = tokens
        self._veto_repo = veto_repo
        self._veto_path = veto_path
        self._http = http_client or httpx.Client(
            base_url=GITHUB_API,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
        )

    def _do(
        self,
        repo: str,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
    ) -> tuple[int, Any]:
        try:
            token = self._tokens.token(repo)
        except Exception as error:  # noqa: BLE001
            raise GitHubError(f"token for {repo}: {error}", sent=False) from None
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        content = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = json.dumps(body).encode()
        try:
            response = self._http.request(
                method, path, content=content, headers=headers
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            # No connection, so no request: nothing reached GitHub.
            raise GitHubError(
                f"transport: {type(error).__name__}", 0, sent=False
            ) from None
        except httpx.TransportError as error:
            raise GitHubError(f"transport: {type(error).__name__}", 0) from None
        raw = response.content
        if len(raw) > MAX_BODY:
            raise GitHubError("response over 8 MiB", response.status_code)
        if response.status_code // 100 != 2:
            first = raw.decode("utf-8", "replace").split("\n", 1)[0][:200]
            raise GitHubError(
                f"HTTP {response.status_code}: {first}", response.status_code
            )
        try:
            return response.status_code, json.loads(raw) if raw else None
        except ValueError:
            raise GitHubError("decoding the answer", response.status_code) from None

    def pull_request(self, repo: str, number: int) -> PullRequest:
        path = _repo_path(repo)
        _, g = self._do(repo, "GET", f"{path}/pulls/{number}")
        files: list[str] = []
        for page in range(1, 31):
            _, listed = self._do(
                repo, "GET", f"{path}/pulls/{number}/files?per_page=100&page={page}"
            )
            for item in listed:
                files.append(item["filename"])
                if item.get("previous_filename"):
                    files.append(item["previous_filename"])
            if len(listed) < 100:
                break
            if page == 30:
                raise GitHubError(
                    "more than 3000 changed files; GitHub lists at most 3000"
                )
        try:
            return PullRequest(
                state=g["state"],
                merged=bool(g.get("merged")),
                merge_commit=g.get("merge_commit_sha") or "",
                draft=bool(g.get("draft")),
                head_sha=g["head"]["sha"],
                base_ref=g["base"]["ref"],
                base_sha=g["base"]["sha"],
                base_repo_id=int(g["base"]["repo"]["id"]),
                author_id=int(g["user"]["id"]),
                mergeable_state=g.get("mergeable_state") or "",
                files=tuple(files),
            )
        except KeyError, TypeError, ValueError:
            raise GitHubError("the PR answer is malformed") from None

    def statuses(self, repo: str, sha: str) -> list[Status]:
        path = _repo_path(repo)
        out: list[Status] = []
        for page in range(1, 11):
            _, listed = self._do(
                repo,
                "GET",
                f"{path}/commits/{quote(sha, safe='')}/statuses"
                f"?per_page=100&page={page}",
            )
            out.extend(
                Status(
                    context=item.get("context") or "",
                    state=item.get("state") or "",
                    description=item.get("description") or "",
                    creator_id=int((item.get("creator") or {}).get("id") or 0),
                )
                for item in listed
            )
            if len(listed) < 100:
                return out
        raise GitHubError("more than 1000 statuses on one commit")

    def commits(self, repo: str, number: int) -> list[Commit]:
        path = _repo_path(repo)
        out: list[Commit] = []
        for page in range(1, 4):
            _, listed = self._do(
                repo, "GET", f"{path}/pulls/{number}/commits?per_page=100&page={page}"
            )
            out.extend(
                Commit(
                    sha=item["sha"],
                    author_id=int((item.get("author") or {}).get("id") or 0),
                    committer_id=int((item.get("committer") or {}).get("id") or 0),
                )
                for item in listed
            )
            if len(listed) < 100:
                break
        if len(out) >= 250:
            # GitHub lists at most 250 commits on a PR, so 250 may be truncated.
            raise GitHubError("250 or more commits; GitHub lists at most 250 on a PR")
        return out

    def vetoes(self) -> tuple[frozenset[str], str]:
        path = _repo_path(self._veto_repo)
        _, g = self._do(
            self._veto_repo, "GET", f"{path}/contents/{self._veto_path}?ref=main"
        )
        if not isinstance(g, dict) or g.get("encoding") != "base64":
            raise GitHubError("the veto list is not base64 content")
        try:
            text = base64.b64decode(str(g["content"]).replace("\n", ""), validate=True)
        except ValueError:
            raise GitHubError("the veto list is not decodable") from None
        return parse_vetoes(text.decode("utf-8", "replace")), str(g.get("sha") or "")

    def required_checks(self, repo: str, branch: str) -> tuple[bool, bool]:
        """Whether the branch's active rules require any status check, and whether
        checks are strict (up to date) or a merge queue is in force. An unreadable
        answer raises, so the merger holds rather than assume (Architect M1)."""
        path = _repo_path(repo)
        present = strict = False
        for page in range(1, 11):
            _, rules = self._do(
                repo,
                "GET",
                f"{path}/rules/branches/{quote(branch, safe='')}"
                f"?per_page=100&page={page}",
            )
            for rule in rules:
                kind = rule.get("type")
                parameters = rule.get("parameters") or {}
                if kind == "required_status_checks" and parameters.get(
                    "required_status_checks"
                ):
                    present = True
                    if parameters.get("strict_required_status_checks_policy"):
                        strict = True
                elif kind == "merge_queue":
                    strict = True
            if len(rules) < 100:
                return present, strict
        raise GitHubError("more than 1000 active rules on one branch")

    def merge(self, repo: str, number: int, sha: str) -> str:
        path = _repo_path(repo)
        try:
            _, g = self._do(
                repo,
                "PUT",
                f"{path}/pulls/{number}/merge",
                {"sha": sha, "merge_method": "merge"},
            )
        except GitHubError as error:
            if not error.sent:
                raise MergeNotSentError(f"merge call not sent: {error}") from None
            if error.status == 0 or error.status >= 500:
                raise MergeUnclearError(f"merge outcome unclear: {error}") from None
            raise
        if not isinstance(g, dict) or g.get("merged") is not True:
            raise MergeUnclearError("merge outcome unclear: answer did not say merged")
        return str(g.get("sha") or "")


def parse_vetoes(text: str) -> frozenset[str]:
    """``- owner/repo#N`` entries from the veto list; other lines are prose."""
    out: set[str] = set()
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("- "):
            continue
        fields = line[2:].split()
        if fields and "/" in fields[0] and "#" in fields[0]:
            out.add(fields[0])
    return frozenset(out)
