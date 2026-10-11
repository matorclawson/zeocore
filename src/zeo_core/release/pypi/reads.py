"""What the PyPI adapter reads: GitHub through ``gh``, PyPI anonymously.

No token passes through zeocore. GitHub is read through the ``gh`` CLI, as
the signing service's admission does, and PyPI is read over HTTPS from its
one fixed origin, with no redirects followed.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol
from urllib.parse import quote

import httpx

from zeo_core.release.signing.admission import GhCli, ReviewUnreadableError

PYPI: Final = "https://pypi.org"


class TargetUnreadableError(RuntimeError):
    """GitHub or PyPI could not be read: the stage holds, never assumes."""


class TagAnswerLostError(RuntimeError):
    """The tag's creation may have landed, but its answer was lost."""


#: How a tag creation ended, when its answer came back.
#: "not_sent": gh never started. "rejected": GitHub refused it (4xx other than
#: 422). "exists": GitHub answered 422, so the ref may already exist.
TagResult = Literal["created", "not_sent", "rejected", "exists"]


@dataclass(frozen=True)
class WorkflowRun:
    status: str  # queued, in_progress, waiting, requested, pending, completed
    conclusion: str | None  # success, failure, cancelled, ... once completed


@dataclass(frozen=True)
class EnvironmentPolicy:
    """What an environment's protection allows, as GitHub reports it."""

    required_reviewers: frozenset[str] | None  # None: no required-reviewer rule
    prevent_self_review: bool
    can_admins_bypass: bool
    # The deployment policies when restricted to custom ones, as (name, type);
    # None when any branch or tag may deploy, or only protected branches.
    custom_policies: tuple[tuple[str, str], ...] | None


@dataclass(frozen=True)
class Identity:
    """Who the adapter acts as, and whether GitHub treats it as an admin."""

    login: str
    admin: bool


@dataclass(frozen=True)
class Provenance:
    """One attestation: its publisher, and the subjects its statement names."""

    publisher: Mapping[str, Any]
    subjects: tuple[tuple[str, str], ...]  # (name, sha256)


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

    def environment(self, repo: str, environment: str) -> EnvironmentPolicy | None:
        """The environment's protection, or None if there is no environment."""
        ...

    def identity(self, repo: str) -> Identity:
        """The identity ``gh`` acts as, and its permission on ``repo``."""
        ...

    def create_tag(self, repo: str, tag: str, commit: str) -> TagResult:
        """The adapter's one effect. Raises ``TagAnswerLostError`` if unknown."""
        ...

    def runs(
        self, repo: str, workflow: str, commit: str
    ) -> tuple[WorkflowRun, ...]: ...


class PyPI(Protocol):
    def files(self, project: str, version: str) -> tuple[PyPIFile, ...] | None:
        """The release's files, or None if PyPI has no such release."""
        ...

    def provenance(
        self, project: str, version: str, filename: str
    ) -> tuple[Provenance, ...] | None:
        """Each attestation's publisher and subjects, or None if there is none."""
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

    def environment(self, repo: str, environment: str) -> EnvironmentPolicy | None:
        base = f"repos/{repo}/environments/{quote(environment)}"
        answer = self._object(base)
        if answer is None:
            return None
        reviewers: set[str] | None = None
        prevent_self_review = False
        for rule in answer.get("protection_rules") or ():
            if isinstance(rule, dict) and rule.get("type") == "required_reviewers":
                reviewers = set() if reviewers is None else reviewers
                prevent_self_review = rule.get("prevent_self_review") is True
                for entry in rule.get("reviewers") or ():
                    login = (entry.get("reviewer") or {}).get("login")
                    if isinstance(login, str):
                        reviewers.add(login)
        policy = answer.get("deployment_branch_policy")
        custom: tuple[tuple[str, str], ...] | None = None
        if isinstance(policy, dict) and policy.get("custom_branch_policies") is True:
            listed = self._object(f"{base}/deployment-branch-policies") or {}
            custom = tuple(
                (str(item.get("name")), str(item.get("type", "branch")))
                for item in listed.get("branch_policies") or ()
                if isinstance(item, dict)
            )
        return EnvironmentPolicy(
            required_reviewers=None if reviewers is None else frozenset(reviewers),
            prevent_self_review=prevent_self_review,
            can_admins_bypass=answer.get("can_admins_bypass") is not False,
            custom_policies=custom,
        )

    def identity(self, repo: str) -> Identity:
        user = self._object("user") or {}
        permissions = (self._object(f"repos/{repo}") or {}).get("permissions")
        login = user.get("login")
        if not isinstance(login, str) or not isinstance(permissions, dict):
            raise TargetUnreadableError("the acting identity can't be read")
        return Identity(login, permissions.get("admin") is True)

    def create_tag(self, repo: str, tag: str, commit: str) -> TagResult:
        argv = [
            self._gh,
            "api",
            "-X",
            "POST",
            f"repos/{repo}/git/refs",
            "-f",
            f"ref=refs/tags/{tag}",
            "-f",
            f"sha={commit}",
        ]
        try:
            done = self._run(argv)
        except OSError:
            return "not_sent"
        except subprocess.SubprocessError as error:
            raise TagAnswerLostError("gh's answer was lost") from error
        if done.returncode == 0:
            return "created"
        if b"HTTP 422" in done.stderr:
            return "exists"
        if re.search(rb"HTTP 4\d\d", done.stderr):
            return "rejected"
        # A 5xx, a network failure or anything else: it may have landed.
        raise TagAnswerLostError("GitHub's answer to the tag creation is unknown")

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

    def provenance(
        self, project: str, version: str, filename: str
    ) -> tuple[Provenance, ...] | None:
        answer = self._read(
            f"/integrity/{quote(project)}/{quote(version)}/{quote(filename)}/provenance",
            "application/vnd.pypi.integrity.v1+json",
        )
        if answer is None:
            return None
        try:
            found = []
            for bundle in answer["attestation_bundles"]:
                for attestation in bundle["attestations"]:
                    statement = json.loads(
                        base64.b64decode(attestation["envelope"]["statement"])
                    )
                    subjects = tuple(
                        (str(item["name"]), str(item["digest"]["sha256"]))
                        for item in statement["subject"]
                    )
                    found.append(Provenance(bundle["publisher"], subjects))
            return tuple(found)
        except KeyError, TypeError, ValueError:
            raise TargetUnreadableError(
                "PyPI's provenance answer is unreadable"
            ) from None
