"""The PyPI adapter's stages, for each target in ``targets.TARGETS``.

The operator delegated releases (2026-10-11): the ``zeo-release`` App creates
the ``vX.Y.Z`` tag, and matorclawson approves the ``pypi`` deployment. Two
distinct identities, so neither releases alone. ``plan``, ``admit``,
``reconcile`` and ``verify`` have no effect. ``apply`` creates the tag, as
the App, and ``approve`` approves the waiting deployment, as matorclawson,
once. The publish itself stays the workflow's, through trusted publishing:
the adapter never holds a PyPI credential and never dispatches a workflow.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    ValidationError,
    model_validator,
)

from zeo_core.release.signing.journal import FAILED, INTENT, SIGNED, SigningJournal

from .reads import (
    ApprovalAnswerLostError,
    GitHub,
    PyPI,
    TagAnswerLostError,
    TagResult,
    TargetUnreadableError,
)
from .targets import (
    APPROVER,
    RELEASE_APP_SLUG,
    RELEASE_TAG_POLICIES,
    RELEASE_TAG_REF,
    TARGETS,
)

Status = Literal["done", "waiting", "held", "refused", "invalid", "ambiguous"]


class PyPIManifest(BaseModel):
    """The ``pypi`` section of a target's ``release.yaml`` (SOW-26 §1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
    version: Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$")]
    repository: Annotated[
        str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    ]
    commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    workflow: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+\.ya?ml$")]
    environment: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]

    @model_validator(mode="after")
    def _is_a_reviewed_target(self) -> PyPIManifest:
        target = TARGETS.get(self.project)
        if target is None:
            raise ValueError(f"{self.project} is not a reviewed target")
        for name in ("repository", "workflow", "environment"):
            if getattr(self, name) != getattr(target, name):
                raise ValueError(
                    f"{name} must be {getattr(target, name)}, the reviewed target"
                )
        return self

    @property
    def tag(self) -> str:
        return f"v{self.version}"


class ManifestError(ValueError):
    """The manifest can't be used: invalid, before anything is read."""


def _section(content: bytes) -> dict[str, Any]:
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError:
        raise ManifestError("release.yaml is not valid YAML") from None
    section = document.get("pypi") if isinstance(document, dict) else None
    if not isinstance(section, dict):
        raise ManifestError("release.yaml has no pypi section")
    return section


def load(path: Path) -> PyPIManifest:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ManifestError(f"the manifest can't be read: {error.strerror}") from None
    try:
        return PyPIManifest.model_validate(_section(content))
    except ValidationError as error:
        raise ManifestError(f"the pypi section is invalid: {error}") from None


@dataclass(frozen=True)
class Outcome:
    status: Status
    reason: str
    message: str
    facts: dict[str, Any] = field(default_factory=dict)


def _held_unreadable(error: TargetUnreadableError) -> Outcome:
    return Outcome("held", "target_unreadable", str(error))


def _pyproject_version(content: bytes) -> str | None:
    try:
        value = tomllib.loads(content.decode("utf-8"))["project"]["version"]
    except UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError:
        return None
    return value if isinstance(value, str) else None


def _committed_manifest(content: bytes, commit: str) -> PyPIManifest | None:
    """``release.yaml``'s pypi section as committed, at ``commit``.

    A file can't name the commit that contains it, so the committed section
    has no ``commit``; every other field must equal the local manifest's.
    """
    try:
        section = _section(content)
        if "commit" in section:
            return None
        return PyPIManifest.model_validate({**section, "commit": commit})
    except ManifestError, ValidationError:
        return None


def plan(manifest: PyPIManifest, github: GitHub, pypi: PyPI) -> Outcome:
    """What applying would do, with no effect (SOW-26 §2)."""
    repo, commit = manifest.repository, manifest.commit
    try:
        branch = github.default_branch(repo)
        if not github.is_ancestor(repo, commit, branch):
            return Outcome(
                "refused", "commit_unreviewed", f"{commit} is not on {branch}"
            )
        workflow = f".github/workflows/{manifest.workflow}"
        if github.file_at(repo, commit, workflow) is None:
            # Under the v* ruleset a tag can't be deleted, so a tag with no
            # workflow to publish it would stay forever: refuse before it.
            return Outcome(
                "refused", "workflow_missing", f"{workflow} is not at the commit"
            )
        if pypi.files(manifest.project, manifest.version) is not None:
            return Outcome(
                "refused", "version_published", f"PyPI already has {manifest.version}"
            )
        pyproject = github.file_at(repo, commit, "pyproject.toml") or b""
        release = github.file_at(repo, commit, "release.yaml") or b""
        changelog = github.file_at(repo, commit, "CHANGELOG.md") or b""
        heading = re.compile(
            rb"^## \[" + re.escape(manifest.version.encode()) + rb"\]", re.MULTILINE
        )
        committed = _committed_manifest(release, commit)
        if committed is not None and committed != manifest:
            return Outcome(
                "refused",
                "manifest_unreviewed",
                "the local pypi section differs from release.yaml at the commit",
            )
        if (
            _pyproject_version(pyproject) != manifest.version
            or committed is None
            or not heading.search(changelog)
        ):
            return Outcome(
                "refused",
                "version_mismatch",
                f"{manifest.version} is not named by pyproject.toml, release.yaml "
                "and CHANGELOG.md at the commit",
            )
        tagged = github.tag_commit(repo, manifest.tag)
    except TargetUnreadableError as error:
        return _held_unreadable(error)
    if tagged is not None and tagged != commit:
        return Outcome(
            "refused", "tag_mismatch", f"{manifest.tag} exists at another commit"
        )
    stage = "already-applied" if tagged == commit else "create-tag"
    return Outcome(
        "done",
        "planned",
        f"apply would {'do nothing' if tagged else f'create {manifest.tag}'}",
        {"tag": manifest.tag, "commit": commit, "stage": stage},
    )


class TagCreator(Protocol):
    """The release App: who creates the tag, and the one call that does it."""

    app_id: int

    def identity(self) -> tuple[int, str]: ...

    def create_tag(self, repo: str, tag: str, commit: str) -> TagResult: ...


def admit(manifest: PyPIManifest, github: GitHub, app_id: int | None) -> Outcome:
    """Only the two delegated identities, together, can release.

    Read at GitHub's own boundary (the operator's delegation, relayed in
    msg-architect-zeocore-pypi-release-delegation-operator-20261011):
    matorclawson is the environment's only required reviewer, with self-review
    prevented and no admin bypass; only ``v*`` tags may deploy; an active
    ruleset on ``v*`` tags restricts creating, updating and deleting them, and
    matorclawson can't bypass it; and ``gh`` acts as matorclawson.

    The ruleset's exact bypass list isn't checked: GitHub shows it only to a
    repository Admin, and under ruling r7 no agent is one. The operator chose
    this (SOW-29, option C). The residual risk, an unseen actor creating a
    ``v*`` tag, can't publish on its own: the run still waits on matorclawson,
    and ``approve`` approves only a reviewed commit on the default branch.
    """
    repo, env = manifest.repository, manifest.environment
    if app_id is None:
        return Outcome(
            "refused",
            "release_app_unconfigured",
            "the release App's id isn't configured",
        )
    try:
        policy = github.environment(repo, env)
        rulesets = github.tag_rulesets(repo)
        approver = github.identity(repo)
    except TargetUnreadableError as error:
        return _held_unreadable(error)
    if policy is None or policy.required_reviewers != {APPROVER}:
        return Outcome(
            "refused",
            "promotion_unbound",
            f"the {env} environment's only required reviewer must be {APPROVER}",
        )
    if not policy.prevent_self_review:
        return Outcome(
            "refused",
            "self_review_allowed",
            f"the {env} environment allows self-review",
        )
    if policy.can_admins_bypass:
        return Outcome(
            "refused",
            "admin_bypass_possible",
            f"the {env} environment lets admins bypass its protection",
        )
    if (
        not policy.custom_policies
        or not set(policy.custom_policies) <= RELEASE_TAG_POLICIES
    ):
        return Outcome(
            "refused",
            "deployment_policy_unrestricted",
            f"the {env} environment must deploy only from v* tags",
        )
    guarding = [
        ruleset
        for ruleset in rulesets
        if ruleset.enforcement == "active"
        and RELEASE_TAG_REF in ruleset.includes
        and not ruleset.excludes
        and {"creation", "update", "deletion"} <= ruleset.rules
    ]
    if not guarding:
        return Outcome(
            "refused",
            "tag_ruleset_missing",
            f"no active ruleset on {RELEASE_TAG_REF}, with no exclusions, restricts "
            "creating, updating and deleting release tags",
        )
    if any(ruleset.current_user_can_bypass != "never" for ruleset in guarding):
        return Outcome(
            "refused",
            "approver_can_bypass",
            f"{APPROVER} can bypass the release tag ruleset; it must be 'never'",
        )
    if approver.login != APPROVER:
        return Outcome(
            "refused",
            "approver_mismatch",
            f"gh acts as {approver.login}, not {APPROVER}",
        )
    return Outcome(
        "done",
        "admitted",
        f"the release App tags and {APPROVER} alone approves in {env}",
        {
            "required_reviewers": sorted(policy.required_reviewers),
            "release_app_id": app_id,
            "tag_ruleset": guarding[0].id,
            "deployment_policies": sorted(
                f"{n} ({t})" for n, t in policy.custom_policies
            ),
        },
    )


def reconcile(manifest: PyPIManifest, github: GitHub) -> Outcome:
    """Read-only: the tag, then the publish run for it."""
    repo = manifest.repository
    try:
        tagged = github.tag_commit(repo, manifest.tag)
        if tagged is None:
            return Outcome("held", "tag_absent", f"{manifest.tag} has not been created")
        if tagged != manifest.commit:
            return Outcome(
                "refused", "tag_mismatch", f"{manifest.tag} is at another commit"
            )
        runs = github.runs(repo, manifest.workflow, manifest.commit)
    except TargetUnreadableError as error:
        return _held_unreadable(error)
    if not runs:
        return Outcome(
            "waiting", "publish_pending", f"no {manifest.workflow} run for the tag yet"
        )
    for_tag = [run for run in runs if run.head_branch == manifest.tag]
    if not for_tag:
        return Outcome(
            "waiting", "publish_pending", f"no {manifest.workflow} run for the tag yet"
        )
    latest = for_tag[0]  # GitHub lists the newest run first
    if latest.status == "waiting":
        return Outcome(
            "waiting",
            "awaiting_approval",
            f"the publish waits for {APPROVER}'s approval in {manifest.environment}",
        )
    if latest.status != "completed":
        return Outcome("waiting", "publish_running", f"the publish is {latest.status}")
    if latest.conclusion != "success":
        return Outcome(
            "held", "publish_failed", f"the publish ended {latest.conclusion}"
        )
    return Outcome("done", "published", "the publish run succeeded")


def verify(manifest: PyPIManifest, pypi: PyPI) -> Outcome:
    """What PyPI shows for the release, file by file (SOW-26 §2 and §5).

    This checks each attestation's publisher binding, not its Sigstore
    signature: it claims "PyPI recorded this publisher", never "verified".
    """
    expected = {
        "kind": "GitHub",
        "repository": manifest.repository,
        "workflow": manifest.workflow,
        "environment": manifest.environment,
    }
    try:
        files = pypi.files(manifest.project, manifest.version)
        if files is None:
            return Outcome("held", "version_absent", f"PyPI has no {manifest.version}")
        kinds = {
            "wheel": [f for f in files if f.filename.endswith(".whl")],
            "sdist": [f for f in files if f.filename.endswith(".tar.gz")],
        }
        if not kinds["wheel"] or not kinds["sdist"]:
            return Outcome(
                "refused", "provenance_mismatch", "PyPI lacks a wheel or an sdist"
            )
        checked = []
        for item in files:
            if item.yanked:
                return Outcome(
                    "refused", "provenance_mismatch", f"{item.filename} is yanked"
                )
            attestations = pypi.provenance(
                manifest.project, manifest.version, item.filename
            )
            if not attestations or any(
                {key: found.publisher.get(key) for key in expected} != expected
                for found in attestations
            ):
                return Outcome(
                    "refused",
                    "provenance_mismatch",
                    f"{item.filename}'s recorded publisher isn't {manifest.workflow} "
                    f"in {manifest.repository}",
                )
            if any(
                found.subjects != ((item.filename, item.sha256),)
                for found in attestations
            ):
                return Outcome(
                    "refused",
                    "provenance_mismatch",
                    f"{item.filename}'s attestation names another file or digest",
                )
            checked.append({"filename": item.filename, "sha256": item.sha256})
    except TargetUnreadableError as error:
        return _held_unreadable(error)
    return Outcome(
        "done",
        "publisher_recorded",
        "PyPI recorded the expected publisher, and an attestation subject equal "
        "to each file's digest; the attestation signatures were not verified",
        {"files": checked, "publisher": expected},
    )


def _settle_open(
    manifest: PyPIManifest,
    github: GitHub,
    journal: SigningJournal,
    key: str,
    now: Callable[[], datetime],
) -> Outcome:
    """An earlier attempt is settled before plan or admit could hide it.

    Only the tag read back at the commit closes it. Anything else stays
    ambiguous, and the tag is never created again.
    """
    try:
        tagged = github.tag_commit(manifest.repository, manifest.tag)
    except TargetUnreadableError:
        tagged = None
    if tagged != manifest.commit:
        return Outcome(
            "ambiguous",
            "apply_unreconciled",
            "an earlier tag creation's answer was lost and the tag isn't at the "
            "commit; it is not retried",
        )
    journal.append(key, SIGNED, now())
    return Outcome(
        "done", "already_applied", f"{manifest.tag} is already at the commit"
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _creator_refusal(app: TagCreator) -> Outcome | None:
    """None when the key signs for the release App itself."""
    try:
        identity = app.identity()
    except TargetUnreadableError as error:
        return _held_unreadable(error)
    if identity != (app.app_id, RELEASE_APP_SLUG):
        return Outcome(
            "refused",
            "creator_not_release_app",
            f"the key signs for {identity[1]} ({identity[0]}), "
            f"not {RELEASE_APP_SLUG} ({app.app_id})",
        )
    return None


def journal_key(manifest: PyPIManifest) -> str:
    """One release, one key: the repository and the version, never the commit."""
    return f"pypi:{manifest.repository}:{manifest.version}"


def apply(
    manifest: PyPIManifest,
    github: GitHub,
    pypi: PyPI,
    app: TagCreator,
    journal: SigningJournal,
    now: Callable[[], datetime] = _now,
) -> Outcome:
    """Create ``vX.Y.Z`` at the reviewed commit, as the release App.

    An open intent from an earlier attempt is settled first, by reading the
    tag back. Then ``plan`` and ``admit`` run again, at the boundary. The
    intent is on disk before the call, and the call is never retried: an
    answer that is lost leaves the intent open and the outcome ambiguous.
    The creator is checked to be the release App itself before the call.
    The journal's "signed" record here means "the tag landed".
    """
    key = journal_key(manifest)
    if journal.is_open(key):
        return _settle_open(manifest, github, journal, key, now)
    planned = plan(manifest, github, pypi)
    if planned.status != "done":
        return planned
    if planned.facts["stage"] == "already-applied":
        return Outcome(
            "done", "already_applied", f"{manifest.tag} is already at the commit"
        )
    admitted = admit(manifest, github, app.app_id)
    if admitted.status != "done":
        return admitted
    creator = _creator_refusal(app)
    if creator is not None:
        return creator
    journal.append(key, INTENT, now())
    try:
        result = app.create_tag(manifest.repository, manifest.tag, manifest.commit)
    except TagAnswerLostError:
        return Outcome(
            "ambiguous",
            "apply_unreconciled",
            "the tag creation's answer was lost; run reconcile, never apply again",
        )
    if result == "exists":
        try:
            tagged = github.tag_commit(manifest.repository, manifest.tag)
        except TargetUnreadableError:
            return Outcome(
                "ambiguous", "apply_unreconciled", "the tag can't be read back"
            )
        result = "created" if tagged == manifest.commit else "rejected"
    if result != "created":
        journal.append(key, FAILED, now())
        return Outcome(
            "held", "tag_not_created", f"GitHub did not create {manifest.tag}"
        )
    journal.append(key, SIGNED, now())
    return Outcome(
        "done",
        "tag_created",
        f"{manifest.tag} is at the commit; the publish now waits for "
        f"{APPROVER}'s approval",
        {"tag": manifest.tag, "commit": manifest.commit},
    )


def approve(
    manifest: PyPIManifest,
    github: GitHub,
    pypi: PyPI,
    app_id: int | None,
    journal: SigningJournal,
    now: Callable[[], datetime] = _now,
) -> Outcome:
    """Approve the waiting ``pypi`` deployment, as matorclawson, exactly once.

    Only for the one run of this exact tag and commit, waiting on exactly
    this environment, after ``plan`` and ``admit`` pass again. An approval
    that landed is never sent again, and a lost answer is ambiguous.
    """
    key = f"approve:{manifest.repository}:{manifest.version}"
    if journal.has_landed(key):
        return Outcome("done", "already_approved", f"{manifest.tag} was approved once")
    waiting = _waiting_run(manifest, github)
    if journal.is_open(key):
        return _settle_approval(manifest, github, journal, key, waiting, now)
    planned = plan(manifest, github, pypi)
    if planned.status != "done":
        return planned
    if planned.facts["stage"] != "already-applied":
        return Outcome("held", "tag_absent", f"{manifest.tag} has not been created")
    admitted = admit(manifest, github, app_id)
    if admitted.status != "done":
        return admitted
    if isinstance(waiting, Outcome):
        return waiting
    run_id, environment_id = waiting
    journal.append(key, INTENT, now())
    try:
        result = github.approve_deployment(
            manifest.repository,
            run_id,
            environment_id,
            f"zeocore release pypi approve: {manifest.tag} at {manifest.commit}",
        )
    except ApprovalAnswerLostError:
        return Outcome(
            "ambiguous",
            "approve_unreconciled",
            "the approval's answer was lost; it is not sent again",
        )
    if result != "approved":
        journal.append(key, FAILED, now())
        return Outcome("held", "approval_not_sent", "GitHub did not take the approval")
    journal.append(key, SIGNED, now())
    return Outcome(
        "done",
        "approved",
        f"{APPROVER} approved the {manifest.environment} deployment of {manifest.tag}",
        {"run_id": run_id, "environment_id": environment_id},
    )


def _settle_approval(
    manifest: PyPIManifest,
    github: GitHub,
    journal: SigningJournal,
    key: str,
    waiting: tuple[int, int] | Outcome,
    now: Callable[[], datetime],
) -> Outcome:
    """An earlier approval's answer was lost: read back what GitHub recorded.

    It is closed as approved only when GitHub shows matorclawson's approval of
    this environment on the tag's run; as refused when it shows a rejection.
    Anything else stays ambiguous, and nothing is sent again.
    """
    unsettled = Outcome(
        "ambiguous",
        "approve_unreconciled",
        "an earlier approval's answer was lost and GitHub shows no approval "
        f"by {APPROVER} of {manifest.environment}; it is not sent again",
    )
    if not isinstance(waiting, Outcome) or waiting.reason != "run_not_waiting":
        return unsettled
    try:
        reviews = [
            review
            for run_id in waiting.facts["run_ids"]
            for review in github.run_approvals(manifest.repository, run_id)
            if manifest.environment in review.environments
        ]
    except TargetUnreadableError:
        return unsettled
    if any(
        review.state == "approved" and review.login == APPROVER for review in reviews
    ):
        journal.append(key, SIGNED, now())
        return Outcome(
            "done", "already_approved", f"{APPROVER} approved {manifest.tag}"
        )
    if any(review.state == "rejected" for review in reviews):
        journal.append(key, FAILED, now())
        return Outcome(
            "held", "approval_rejected", f"the {manifest.tag} deployment was rejected"
        )
    return unsettled


def _waiting_run(manifest: PyPIManifest, github: GitHub) -> tuple[int, int] | Outcome:
    """The one run of this tag and commit waiting on this environment, if any."""
    try:
        runs = [
            run
            for run in github.runs(
                manifest.repository, manifest.workflow, manifest.commit
            )
            if run.head_branch == manifest.tag
        ]
        waiting = [run for run in runs if run.status == "waiting"]
        if not waiting:
            if runs:
                return Outcome(
                    "held",
                    "run_not_waiting",
                    f"the {manifest.tag} run isn't waiting for an approval",
                    {"run_ids": [run.id for run in runs if run.id is not None]},
                )
            return Outcome(
                "waiting", "publish_pending", f"no run for {manifest.tag} yet"
            )
        if len(waiting) != 1 or waiting[0].id is None:
            return Outcome(
                "refused", "approval_unexpected", "more than one run waits for the tag"
            )
        pending = github.pending_deployments(manifest.repository, waiting[0].id)
    except TargetUnreadableError as error:
        return _held_unreadable(error)
    if len(pending) != 1 or pending[0].environment != manifest.environment:
        return Outcome(
            "refused",
            "approval_unexpected",
            f"the run must wait on {manifest.environment} alone",
        )
    if not pending[0].can_approve:
        return Outcome(
            "refused",
            "approver_cannot_approve",
            f"gh's identity can't approve {manifest.environment}",
        )
    return waiting[0].id, pending[0].environment_id
