"""The PyPI adapter's stages for ``pypi:zeocore`` (ZEOCORE-SOW-26).

``plan``, ``admit``, ``reconcile`` and ``verify`` have no effect. The one
effect, creating the ``vX.Y.Z`` tag, isn't built until the Architect has
reviewed the design. The publish itself stays ``publish.yml``'s, behind the
operator's approval in the ``pypi`` environment: the adapter never approves,
never dispatches the workflow and never holds a PyPI credential.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from .reads import GitHub, PyPI, TargetUnreadableError

#: Who must approve every publish, as a required reviewer of the environment.
#: Reviewed with zeocore's source: the operator.
OPERATOR_REVIEWER: Final = "rodriveracom"

Status = Literal["done", "waiting", "held", "refused", "invalid"]


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


def _manifest_version(content: bytes) -> str | None:
    try:
        value = _section(content).get("version")
    except ManifestError:
        return None
    return value if isinstance(value, str) else None


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
        if (
            _pyproject_version(pyproject) != manifest.version
            or _manifest_version(release) != manifest.version
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


def admit(manifest: PyPIManifest, github: GitHub) -> Outcome:
    """The operator approves every publish, at GitHub's own boundary."""
    try:
        reviewers = github.required_reviewers(manifest.repository, manifest.environment)
    except TargetUnreadableError as error:
        return _held_unreadable(error)
    if reviewers is None or OPERATOR_REVIEWER not in reviewers:
        return Outcome(
            "refused",
            "promotion_unbound",
            f"the {manifest.environment} environment doesn't require the operator's "
            "approval, so a tag would publish without it",
        )
    return Outcome(
        "done",
        "admitted",
        f"{OPERATOR_REVIEWER} approves the publish in {manifest.environment}",
        {"required_reviewers": sorted(reviewers)},
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
    latest = runs[0]  # GitHub lists the newest run first
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
            publishers = pypi.publishers(
                manifest.project, manifest.version, item.filename
            )
            if not publishers or any(
                {key: publisher.get(key) for key in expected} != expected
                for publisher in publishers
            ):
                return Outcome(
                    "refused",
                    "provenance_mismatch",
                    f"{item.filename}'s recorded publisher isn't {manifest.workflow} "
                    f"in {manifest.repository}",
                )
            checked.append({"filename": item.filename, "sha256": item.sha256})
    except TargetUnreadableError as error:
        return _held_unreadable(error)
    return Outcome(
        "done",
        "publisher_recorded",
        "PyPI recorded the expected publisher for every file; the attestation "
        "signatures were not verified",
        {"files": checked, "publisher": expected},
    )
