"""The PyPI adapter's stages for ``pypi:zeocore`` (ZEOCORE-SOW-26).

``plan``, ``admit``, ``reconcile`` and ``verify`` have no effect. ``apply``
has the one effect: it creates the ``vX.Y.Z`` tag. The publish itself stays
``publish.yml``'s, behind the operator's approval in the ``pypi``
environment: the adapter never approves, never dispatches the workflow and
never holds a PyPI credential.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    ValidationError,
    model_validator,
)

from zeo_core.release.signing.journal import FAILED, INTENT, SIGNED, SigningJournal

from .reads import GitHub, PyPI, TagAnswerLostError, TargetUnreadableError

#: Who must approve every publish: the operator, as the environment's only
#: required reviewer, since any one listed reviewer can approve on GitHub.
OPERATOR_REVIEWER: Final = "rodriveracom"
#: The one target this adapter serves, pinned with zeocore's source. A local
#: manifest can't name another repository, workflow or environment.
TARGET: Final = {
    "project": "zeocore",
    "repository": "zeroemployeeorg/zeocore",
    "workflow": "publish.yml",
    "environment": "pypi",
}

Status = Literal["done", "waiting", "held", "refused", "invalid", "ambiguous"]


class PyPIManifest(BaseModel):
    """The ``pypi`` section of zeocore's ``release.yaml`` (SOW-26 §1)."""

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
    def _is_the_pinned_target(self) -> PyPIManifest:
        for name, pinned in TARGET.items():
            if getattr(self, name) != pinned:
                raise ValueError(f"{name} must be {pinned}, the reviewed target")
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


#: The only deployments the environment may allow: release tags.
RELEASE_TAG_POLICIES: Final = frozenset({("v*", "tag"), ("v*.*.*", "tag")})


def admit(manifest: PyPIManifest, github: GitHub) -> Outcome:
    """The operator approves every publish, and the tag's creator cannot.

    Checked at GitHub's own boundary (SOW-26 §2 and the Architect's reviews):
    the operator is the environment's only required reviewer, since any one
    listed reviewer can approve; the identity that will create the tag is
    not the operator, so it can't approve its own deployment, and it can't
    bypass the protection as an admin; and only release tags may deploy.
    Otherwise "never approves" would be a convention, not a control.
    """
    try:
        policy = github.environment(manifest.repository, manifest.environment)
        creator = github.identity(manifest.repository)
    except TargetUnreadableError as error:
        return _held_unreadable(error)
    env = manifest.environment
    if policy is None or policy.required_reviewers != {OPERATOR_REVIEWER}:
        return Outcome(
            "refused",
            "promotion_unbound",
            f"the {env} environment's only required reviewer must be the operator, "
            "because any listed reviewer can approve",
        )
    if creator.login == OPERATOR_REVIEWER:
        return Outcome(
            "refused",
            "creator_is_operator",
            "the tag must be created by a seat or App identity, not the operator's",
        )
    if creator.admin and policy.can_admins_bypass:
        return Outcome(
            "refused",
            "admin_bypass_possible",
            f"{creator.login} is an admin and the {env} environment lets admins "
            "bypass its protection",
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
    return Outcome(
        "done",
        "admitted",
        f"{OPERATOR_REVIEWER} approves the publish in {env}; {creator.login} cannot",
        {
            "required_reviewers": sorted(policy.required_reviewers),
            "creator": creator.login,
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
            "awaiting_operator_approval",
            f"the publish waits for the operator's approval in {manifest.environment}",
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


def apply(
    manifest: PyPIManifest,
    github: GitHub,
    pypi: PyPI,
    journal: SigningJournal,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Outcome:
    """The adapter's one effect: create ``vX.Y.Z`` at the reviewed commit.

    ``plan`` and ``admit`` run again first, at the boundary. The intent is on
    disk before the call, and the call is never retried: an answer that is
    lost leaves the intent open and the outcome ambiguous, for ``reconcile``.
    The journal's "signed" record here means "the tag landed".
    """
    key = f"pypi:{manifest.project}:{manifest.tag}:{manifest.commit}"
    planned = plan(manifest, github, pypi)
    if planned.status != "done":
        return planned
    if planned.facts["stage"] == "already-applied":
        if journal.is_open(key):
            journal.append(key, SIGNED, now())
        return Outcome(
            "done", "already_applied", f"{manifest.tag} is already at the commit"
        )
    admitted = admit(manifest, github)
    if admitted.status != "done":
        return admitted
    if journal.is_open(key):
        return Outcome(
            "ambiguous",
            "apply_unreconciled",
            "an earlier tag creation's answer was lost and the tag isn't there; "
            "it is not retried",
        )
    journal.append(key, INTENT, now())
    try:
        result = github.create_tag(manifest.repository, manifest.tag, manifest.commit)
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
        f"{manifest.tag} is at the commit; the publish now waits for the "
        "operator's approval",
        {"tag": manifest.tag, "commit": manifest.commit},
    )
