"""What the PyPI adapter reads: GitHub through ``gh``, PyPI anonymously.

No token passes through zeocore. GitHub is read through the ``gh`` CLI, as
the signing service's admission does, and PyPI is read over HTTPS from its
one fixed origin, with no redirects followed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol
from urllib.parse import quote

import httpx

from zeo_core.release.signing.admission import GhCli, ReviewUnreadableError

PYPI: Final = "https://pypi.org"


class TargetUnreadableError(RuntimeError):
    """GitHub or PyPI could not be read: the stage holds, never assumes."""


@dataclass(frozen=True)
class WorkflowRun:
    status: str  # queued, in_progress, waiting, requested, pending, completed
    conclusion: str | None  # success, failure, cancelled, ... once completed


@dataclass(frozen=True)
class PyPIFile:
    filename: str
    sha256: str
    yanked: bool


class GitHub(Protocol):
    def default_branch(self, repo: str) -> str: ...

    def is_ancestor(self, repo: str, commit: str, branch: str) -> bool: ...

    def file_at(self, repo: str, commit: str, path: str) -> bytes | None: ...

    def tag_commit(self, repo: str, tag: str) -> str | None:
        """The commit the tag points at, or None if there is no tag.

        An annotated tag is followed to its commit. This adapter creates
        lightweight tags, but earlier releases (v0.12.0) were annotated.
        """
        ...

    def required_reviewers(self, repo: str, environment: str) -> frozenset[str] | None:
        """The environment's required reviewers, or None if it has no rule."""
        ...

    def runs(
        self, repo: str, workflow: str, commit: str
    ) -> tuple[WorkflowRun, ...]: ...


class PyPI(Protocol):
    def files(self, project: str, version: str) -> tuple[PyPIFile, ...] | None:
        """The release's files, or None if PyPI has no such release."""
        ...

    def publishers(
        self, project: str, version: str, filename: str
    ) -> tuple[Mapping[str, Any], ...] | None:
        """Each provenance bundle's publisher, or None if there is none."""
        ...


class GitHubCli(GhCli):
    """GitHub's REST API through ``gh api``, so no token passes through here."""

    def _object(self, endpoint: str) -> dict[str, Any] | None:
        try:
            return self._json(endpoint)
        except ReviewUnreadableError as error:
            raise TargetUnreadableError(str(error)) from None

    def default_branch(self, repo: str) -> str:
        try:
            return super().default_branch(repo)
        except ReviewUnreadableError as error:
            raise TargetUnreadableError(str(error)) from None

    def is_ancestor(self, repo: str, commit: str, branch: str) -> bool:
        try:
            return super().is_ancestor(repo, commit, branch)
        except ReviewUnreadableError as error:
            raise TargetUnreadableError(str(error)) from None

    def file_at(self, repo: str, commit: str, path: str) -> bytes | None:
        try:
            return super().file_at(repo, commit, path)
        except ReviewUnreadableError as error:
            raise TargetUnreadableError(str(error)) from None

    def tag_commit(self, repo: str, tag: str) -> str | None:
        answer = self._object(f"repos/{repo}/git/ref/tags/{quote(tag)}")
        if answer is None:
            return None
        target = answer.get("object")
        # A tag of a tag is possible; follow a bounded number of them.
        for _ in range(4):
            if not isinstance(target, dict) or not isinstance(target.get("sha"), str):
                raise TargetUnreadableError("GitHub's tag answer is unreadable")
            if target.get("type") == "commit":
                return str(target["sha"])
            if target.get("type") != "tag":
                raise TargetUnreadableError(
                    "the tag points at neither a commit nor a tag"
                )
            annotated = self._object(f"repos/{repo}/git/tags/{target['sha']}")
            if annotated is None:
                raise TargetUnreadableError("the annotated tag can't be read")
            target = annotated.get("object")
        raise TargetUnreadableError("the tag chain is too long")

    def required_reviewers(self, repo: str, environment: str) -> frozenset[str] | None:
        answer = self._object(f"repos/{repo}/environments/{quote(environment)}")
        if answer is None:
            return None
        reviewers: set[str] = set()
        found = False
        for rule in answer.get("protection_rules") or ():
            if isinstance(rule, dict) and rule.get("type") == "required_reviewers":
                found = True
                for entry in rule.get("reviewers") or ():
                    login = (entry.get("reviewer") or {}).get("login")
                    if isinstance(login, str):
                        reviewers.add(login)
        return frozenset(reviewers) if found else None

    def runs(self, repo: str, workflow: str, commit: str) -> tuple[WorkflowRun, ...]:
        endpoint = (
            f"repos/{repo}/actions/workflows/{quote(workflow)}/runs"
            f"?head_sha={commit}&event=push"
        )
        answer = self._object(endpoint) or {}
        runs = []
        for run in answer.get("workflow_runs") or ():
            if isinstance(run, dict) and isinstance(run.get("status"), str):
                conclusion = run.get("conclusion")
                runs.append(
                    WorkflowRun(
                        run["status"],
                        conclusion if isinstance(conclusion, str) else None,
                    )
                )
        return tuple(runs)


Get = Callable[[str, Mapping[str, str]], httpx.Response]


def _get(url: str, headers: Mapping[str, str]) -> httpx.Response:
    return httpx.get(url, headers=dict(headers), timeout=30.0, follow_redirects=False)


class PyPIHttp:
    """PyPI's JSON and integrity APIs, anonymously, from its one origin."""

    def __init__(self, get: Get = _get) -> None:
        self._get = get

    def _read(
        self, path: str, accept: str = "application/json"
    ) -> dict[str, Any] | None:
        try:
            response = self._get(f"{PYPI}{path}", {"Accept": accept})
        except httpx.HTTPError:
            raise TargetUnreadableError("PyPI could not be reached") from None
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise TargetUnreadableError(f"PyPI answered {response.status_code}")
        try:
            value = json.loads(response.content)
        except ValueError:
            raise TargetUnreadableError("PyPI answered with something else") from None
        if not isinstance(value, dict):
            raise TargetUnreadableError("PyPI answered with something else")
        return value

    def files(self, project: str, version: str) -> tuple[PyPIFile, ...] | None:
        answer = self._read(f"/pypi/{quote(project)}/{quote(version)}/json")
        if answer is None:
            return None
        try:
            return tuple(
                PyPIFile(
                    item["filename"], item["digests"]["sha256"], bool(item["yanked"])
                )
                for item in answer["urls"]
            )
        except KeyError, TypeError:
            raise TargetUnreadableError("PyPI's release answer is unreadable") from None

    def publishers(
        self, project: str, version: str, filename: str
    ) -> tuple[Mapping[str, Any], ...] | None:
        answer = self._read(
            f"/integrity/{quote(project)}/{quote(version)}/{quote(filename)}/provenance",
            "application/vnd.pypi.integrity.v1+json",
        )
        if answer is None:
            return None
        try:
            bundles = answer["attestation_bundles"]
            return tuple(bundle["publisher"] for bundle in bundles)
        except KeyError, TypeError:
            raise TargetUnreadableError(
                "PyPI's provenance answer is unreadable"
            ) from None
