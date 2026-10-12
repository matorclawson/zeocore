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
_MAX_POLICY_PAGES: Final = 20


def _listed(value: object, what: str) -> list[Any]:
    """A JSON list GitHub sends, or unreadable: never iterate a scalar."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise TargetUnreadableError(f"the {what} are unreadable")
    return value


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
    head_branch: str | None = None  # for a tag push, the tag's name
    id: int | None = None


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
class TagRuleset:
    """One ruleset on tags, as GitHub reports its detail."""

    id: int
    enforcement: str
    includes: tuple[str, ...]
    excludes: tuple[str, ...]
    rules: frozenset[str]
    bypass: tuple[tuple[str, int | None, str], ...]  # (actor_type, actor_id, mode)


@dataclass(frozen=True)
class PendingDeployment:
    environment_id: int
    environment: str
    can_approve: bool


@dataclass(frozen=True)
class RunApproval:
    """One review recorded on a run: who, which way, for which environments."""

    state: str  # approved or rejected
    login: str
    environments: frozenset[str]


#: How an approval ended, when its answer came back.
ApprovalResult = Literal["approved", "not_sent", "rejected"]


class ApprovalAnswerLostError(RuntimeError):
    """The approval may have landed, but its answer was lost."""


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
        """The identity ``gh`` acts as (the approver), and its permission."""
        ...

    def tag_rulesets(self, repo: str) -> tuple[TagRuleset, ...]:
        """Every ruleset that targets tags, with its rules and bypass list."""
        ...

    def pending_deployments(
        self, repo: str, run_id: int
    ) -> tuple[PendingDeployment, ...]:
        """The deployments a run waits on, and whether ``gh``'s identity may approve."""
        ...

    def approve_deployment(
        self, repo: str, run_id: int, environment_id: int, comment: str
    ) -> ApprovalResult:
        """The approve stage's one effect. Raises ``ApprovalAnswerLostError``."""
        ...

    def run_approvals(self, repo: str, run_id: int) -> tuple[RunApproval, ...]:
        """The reviews GitHub recorded on a run's deployments."""
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
        for rule in _listed(answer.get("protection_rules"), "protection rules"):
            if isinstance(rule, dict) and rule.get("type") == "required_reviewers":
                reviewers = set() if reviewers is None else reviewers
                prevent_self_review = rule.get("prevent_self_review") is True
                for entry in _listed(rule.get("reviewers"), "required reviewers"):
                    reviewer = (
                        entry.get("reviewer") if isinstance(entry, dict) else None
                    )
                    login = (
                        reviewer.get("login") if isinstance(reviewer, dict) else None
                    )
                    if not isinstance(login, str):
                        # An unreadable reviewer may be anyone: never assume.
                        raise TargetUnreadableError("a required reviewer is unreadable")
                    reviewers.add(login)
        policy = answer.get("deployment_branch_policy")
        custom: tuple[tuple[str, str], ...] | None = None
        if isinstance(policy, dict) and policy.get("custom_branch_policies") is True:
            custom = self._deployment_policies(base)
        return EnvironmentPolicy(
            required_reviewers=None if reviewers is None else frozenset(reviewers),
            prevent_self_review=prevent_self_review,
            can_admins_bypass=answer.get("can_admins_bypass") is not False,
            custom_policies=custom,
        )

    def _deployment_policies(self, base: str) -> tuple[tuple[str, str], ...]:
        """Every page of the custom deployment policies, or unreadable.

        A policy never read is a policy never checked, so a short read fails
        closed: the count read must equal GitHub's ``total_count``.
        """
        policies: list[tuple[str, str]] = []
        for page in range(1, _MAX_POLICY_PAGES + 1):
            listed = self._object(
                f"{base}/deployment-branch-policies?per_page=100&page={page}"
            )
            total = (listed or {}).get("total_count")
            items = (listed or {}).get("branch_policies")
            if not isinstance(total, int) or not isinstance(items, list):
                raise TargetUnreadableError("the deployment policies are unreadable")
            for item in items:
                name = item.get("name") if isinstance(item, dict) else None
                kind = item.get("type", "branch") if isinstance(item, dict) else None
                if not isinstance(name, str) or not isinstance(kind, str):
                    raise TargetUnreadableError("a deployment policy is unreadable")
                policies.append((name, kind))
            if len(policies) >= total or not items:
                break
        if len(policies) != total:
            raise TargetUnreadableError(
                f"read {len(policies)} of {total} deployment policies"
            )
        return tuple(policies)

    def identity(self, repo: str) -> Identity:
        user = self._object("user") or {}
        permissions = (self._object(f"repos/{repo}") or {}).get("permissions")
        login = user.get("login")
        if not isinstance(login, str) or not isinstance(permissions, dict):
            raise TargetUnreadableError("the acting identity can't be read")
        return Identity(login, permissions.get("admin") is True)

    def _any(self, endpoint: str) -> Any | None:  # noqa: ANN401 - raw JSON
        """A JSON answer of any shape, or None on 404."""
        try:
            body = self._api([endpoint])
        except ReviewUnreadableError as error:
            raise TargetUnreadableError(str(error)) from None
        if body is None:
            return None
        try:
            return json.loads(body)
        except ValueError:
            raise TargetUnreadableError("GitHub answered with something else") from None

    def tag_rulesets(self, repo: str) -> tuple[TagRuleset, ...]:
        listed = self._any(f"repos/{repo}/rulesets?per_page=100&includes_parents=true")
        found = []
        for summary in _listed(listed, "rulesets"):
            if not isinstance(summary, dict) or not isinstance(summary.get("id"), int):
                raise TargetUnreadableError("a ruleset is unreadable")
            if summary.get("target") != "tag":
                continue
            detail = self._object(f"repos/{repo}/rulesets/{summary['id']}")
            if detail is None:
                raise TargetUnreadableError("a tag ruleset can't be read")
            found.append(_tag_ruleset(summary["id"], detail))
        if len(_listed(listed, "rulesets")) >= 100:
            # A page that is full may not be the last: never assume it is.
            raise TargetUnreadableError("more rulesets than one page")
        return tuple(found)

    def pending_deployments(
        self, repo: str, run_id: int
    ) -> tuple[PendingDeployment, ...]:
        listed = self._any(f"repos/{repo}/actions/runs/{run_id}/pending_deployments")
        pending = []
        for item in _listed(listed, "pending deployments"):
            environment = item.get("environment") if isinstance(item, dict) else None
            env_id = environment.get("id") if isinstance(environment, dict) else None
            name = environment.get("name") if isinstance(environment, dict) else None
            if not isinstance(env_id, int) or not isinstance(name, str):
                raise TargetUnreadableError("a pending deployment is unreadable")
            pending.append(
                PendingDeployment(
                    env_id, name, item.get("current_user_can_approve") is True
                )
            )
        return tuple(pending)

    def run_approvals(self, repo: str, run_id: int) -> tuple[RunApproval, ...]:
        listed = self._any(f"repos/{repo}/actions/runs/{run_id}/approvals")
        found = []
        for item in _listed(listed, "run approvals"):
            user = item.get("user") if isinstance(item, dict) else None
            login = user.get("login") if isinstance(user, dict) else None
            state = item.get("state") if isinstance(item, dict) else None
            if not isinstance(login, str) or not isinstance(state, str):
                raise TargetUnreadableError("a run approval is unreadable")
            names = set()
            for env in _listed(item.get("environments"), "approval environments"):
                name = env.get("name") if isinstance(env, dict) else None
                if not isinstance(name, str):
                    raise TargetUnreadableError("an approval environment is unreadable")
                names.add(name)
            found.append(RunApproval(state, login, frozenset(names)))
        return tuple(found)

    def approve_deployment(
        self, repo: str, run_id: int, environment_id: int, comment: str
    ) -> ApprovalResult:
        argv = [
            self._gh,
            "api",
            "-X",
            "POST",
            f"repos/{repo}/actions/runs/{run_id}/pending_deployments",
            "-F",
            f"environment_ids[]={environment_id}",
            "-f",
            "state=approved",
            "-f",
            f"comment={comment}",
        ]
        try:
            done = self._run(argv)
        except OSError:
            return "not_sent"
        except subprocess.SubprocessError as error:
            raise ApprovalAnswerLostError("gh's answer was lost") from error
        if done.returncode == 0:
            return "approved"
        if re.search(rb"HTTP 4\d\d", done.stderr):
            return "rejected"
        raise ApprovalAnswerLostError("GitHub's answer to the approval is unknown")

    def runs(self, repo: str, workflow: str, commit: str) -> tuple[WorkflowRun, ...]:
        endpoint = (
            f"repos/{repo}/actions/workflows/{quote(workflow)}/runs"
            f"?head_sha={commit}&event=push"
        )
        answer = self._object(endpoint)
        if answer is None:
            raise TargetUnreadableError(f"{workflow} is not a workflow of {repo}")
        runs = []
        for run in answer.get("workflow_runs") or ():
            if not isinstance(run, dict) or not isinstance(run.get("status"), str):
                raise TargetUnreadableError("a workflow run is unreadable")
            conclusion, branch = run.get("conclusion"), run.get("head_branch")
            run_id = run.get("id")
            runs.append(
                WorkflowRun(
                    run["status"],
                    conclusion if isinstance(conclusion, str) else None,
                    branch if isinstance(branch, str) else None,
                    run_id if isinstance(run_id, int) else None,
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
                    publisher = bundle["publisher"]
                    if not isinstance(publisher, dict):
                        raise TypeError
                    found.append(Provenance(publisher, subjects))
            return tuple(found)
        except KeyError, TypeError, ValueError:
            raise TargetUnreadableError(
                "PyPI's provenance answer is unreadable"
            ) from None


def _tag_ruleset(ruleset_id: int, detail: Mapping[str, Any]) -> TagRuleset:
    conditions = detail.get("conditions") or {}
    ref_name = conditions.get("ref_name") if isinstance(conditions, dict) else None
    if not isinstance(ref_name, dict):
        ref_name = {}
    includes = _listed(ref_name.get("include"), "ruleset includes")
    excludes = _listed(ref_name.get("exclude"), "ruleset excludes")
    rules = []
    for rule in _listed(detail.get("rules"), "ruleset rules"):
        kind = rule.get("type") if isinstance(rule, dict) else None
        if not isinstance(kind, str):
            raise TargetUnreadableError("a ruleset rule is unreadable")
        rules.append(kind)
    bypass = []
    for actor in _listed(detail.get("bypass_actors"), "bypass actors"):
        if not isinstance(actor, dict) or not isinstance(actor.get("actor_type"), str):
            raise TargetUnreadableError("a bypass actor is unreadable")
        actor_id = actor.get("actor_id")
        bypass.append(
            (
                actor["actor_type"],
                actor_id if isinstance(actor_id, int) else None,
                str(actor.get("bypass_mode", "always")),
            )
        )
    enforcement = detail.get("enforcement")
    if not isinstance(enforcement, str) or not all(
        isinstance(item, str) for item in (*includes, *excludes)
    ):
        raise TargetUnreadableError("a tag ruleset is unreadable")
    return TagRuleset(
        ruleset_id,
        enforcement,
        tuple(includes),
        tuple(excludes),
        frozenset(rules),
        tuple(bypass),
    )
