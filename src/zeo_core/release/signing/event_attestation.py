"""Authenticating a published release event by GitHub attestation (SOW-44 rev 2, M2).

``zeo.release-event/v1`` is the file ``release-event.json`` on a GitHub release.
``publish.yml`` attests the bytes that were uploaded; this module checks that
attestation and nothing else. It answers one question: did the ``publish.yml``
workflow of ``zeroemployeeorg/zeocore``, on ``main``, triggered by a push, on a
GitHub-hosted runner, produce exactly these bytes for the commit the event
names? It never answers whether the commit was reviewed (that is
``inventory.check_review`` and ``check_release_approval`` below) and never
whether an installed kit is signed. A successful answer here makes no build
eligible.

The identity, issuer, ref, trigger and predicate type are constants of this
module. They are not read from the event or from the bundle, and each was read
back from a real certificate (the publish run of zeocore 0.38.0 on commit
``6e100c61``). ``gh attestation verify`` is given every pin as a flag, and the
result it prints is then checked again here field by field, so a flag that is
missing or ineffective cannot let a wrong certificate through. Three outcomes
only: ``authenticated``; ``refused`` (a check ran and failed); ``held`` (the
check could not complete: no network, no attestation, no ``gh``). Nothing falls
back to an unauthenticated event.

Needs network access to GitHub and a current Sigstore trusted root, which
``gh`` fetches and refreshes through TUF itself; ``--custom-trusted-root`` is
deliberately not used. Public repositories only: on the free plan GitHub does
not store attestations for private ones.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, Protocol

from .event_auth import EventRefusalError, read_event
from .inventory import APPROVE_CONTEXT, APPROVER_USER_ID, Status

REPOSITORY: Final = "zeroemployeeorg/zeocore"
CERT_IDENTITY: Final = (
    f"https://github.com/{REPOSITORY}/.github/workflows/publish.yml@refs/heads/main"
)
OIDC_ISSUER: Final = "https://token.actions.githubusercontent.com"
SOURCE_REF: Final = "refs/heads/main"
TRIGGER: Final = "push"
RUNNER: Final = "github-hosted"
PREDICATE_TYPE: Final = "https://slsa.dev/provenance/v1"
_HEX40: Final = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class Expected:
    """What the caller asked for. At least one is required: the event itself has
    no freshness, so an old genuine event for another release would verify."""

    tag: str | None = None
    commit: str | None = None


@dataclass(frozen=True)
class GhResult:
    code: int
    stdout: str
    stderr: str


class GhRunner(Protocol):
    def __call__(self, argv: Sequence[str]) -> GhResult: ...


@dataclass(frozen=True)
class Authentication:
    status: Literal["authenticated", "refused", "held"]
    reason: str
    message: str
    tag: str | None = None
    commit: str | None = None


def _hex40(value: object) -> bool:
    return isinstance(value, str) and len(value) == 40 and set(value) <= _HEX40


def gh_arguments(path: Path, commit: str) -> list[str]:
    """The exact ``gh attestation verify`` command line for an event at ``commit``.

    Every pin is a flag. ``--signer-workflow`` is not used (a prefix match in
    gh 2.101.0) and ``--repo`` is never the only pin (it accepts any workflow in
    the repository): the exact certificate identity is.
    """
    return [
        "gh",
        "attestation",
        "verify",
        str(path),
        "--repo",
        REPOSITORY,
        "--cert-identity",
        CERT_IDENTITY,
        "--cert-oidc-issuer",
        OIDC_ISSUER,
        "--source-ref",
        SOURCE_REF,
        "--source-digest",
        commit,
        "--signer-digest",
        commit,
        "--deny-self-hosted-runners",
        "--predicate-type",
        PREDICATE_TYPE,
        "--format",
        "json",
    ]


def _failed_check(stderr: str) -> bool:
    """A verification that ran and failed, as ``gh`` words it (observed, gh 2.101.0).

    Anything else that exits non-zero is "could not complete": it is ``held``.
    Both are non-authenticated, so a message this misreads costs a label only.
    """
    first = stderr.strip().splitlines()[0] if stderr.strip() else ""
    return first.startswith(("Error: expected ", "Error: verifying with issuer"))


def _certificate_problem(entry: object, digest: str, commit: str) -> str | None:
    """The first way one verified attestation differs from the pins, or None."""
    if not isinstance(entry, dict):
        return "an attestation result is not an object"
    result = entry.get("verificationResult")
    if not isinstance(result, dict):
        return "an attestation has no verification result"
    signature = result.get("signature")
    certificate = signature.get("certificate") if isinstance(signature, dict) else None
    if not isinstance(certificate, dict):
        return "an attestation has no certificate"
    statement = result.get("statement")
    if not isinstance(statement, dict):
        return "an attestation has no statement"
    pins: dict[str, object] = {
        "subjectAlternativeName": CERT_IDENTITY,
        "issuer": OIDC_ISSUER,
        "sourceRepositoryRef": SOURCE_REF,
        "sourceRepositoryDigest": commit,
        "buildSignerDigest": commit,
        "runnerEnvironment": RUNNER,
        "buildTrigger": TRIGGER,
        "githubWorkflowTrigger": TRIGGER,
    }
    for field, wanted in pins.items():
        if certificate.get(field) != wanted:
            return f"the certificate's {field} is not the pinned value"
    if statement.get("predicateType") != PREDICATE_TYPE:
        return "the predicate type is not the pinned one"
    subjects = statement.get("subject")
    digests = (
        {
            s.get("digest", {}).get("sha256")
            for s in subjects
            if isinstance(s, dict) and isinstance(s.get("digest"), dict)
        }
        if isinstance(subjects, list)
        else set()
    )
    if digest not in digests:
        return "the attestation's subjects do not include these bytes"
    return None


def _held(message: str, tag: str | None, commit: str | None) -> Authentication:
    return Authentication(
        "held", "event_authentication_unavailable", message, tag=tag, commit=commit
    )


def _failed(message: str, tag: str | None, commit: str | None) -> Authentication:
    return Authentication(
        "refused", "event_authentication_failed", message, tag=tag, commit=commit
    )


def _identify(content: bytes, expected: Expected) -> tuple[str, str] | Authentication:
    """The event's (tag, commit), or the refusal: not this repository, not asked for."""
    try:
        event = json.loads(read_event(content).payload)
    except (EventRefusalError, ValueError) as error:
        return Authentication("refused", "event_invalid", str(error))
    tag, commit = event.get("tag"), event.get("commit")
    if event.get("repository") != REPOSITORY:
        return Authentication(
            "refused", "event_other_repository", f"the event is not for {REPOSITORY}"
        )
    if not isinstance(tag, str) or not _hex40(commit):
        return Authentication(
            "refused", "event_invalid", "the event's tag or commit is malformed"
        )
    if (expected.tag and expected.tag != tag) or (
        expected.commit and expected.commit != commit
    ):
        return Authentication(
            "refused", "event_not_expected", "the event is not the release asked for"
        )
    return tag, str(commit)


def _verdict(stdout: str, digest: str, tag: str, commit: str) -> Authentication:
    """Check what ``gh`` printed again, field by field, against the pins."""
    try:
        entries = json.loads(stdout)
    except ValueError:
        return _held("gh printed no readable verification result", tag, commit)
    if not isinstance(entries, list) or not entries:
        return _held("gh reported no verified attestation", tag, commit)
    problems = [_certificate_problem(entry, digest, commit) for entry in entries]
    if None in problems:
        return Authentication(
            "authenticated",
            "event_authenticated",
            "publish.yml on main produced exactly these bytes for this commit; "
            "this is not release authorization",
            tag=tag,
            commit=commit,
        )
    return _failed(next(p for p in problems if p), tag, commit)


def authenticate_published_event(
    path: Path, expected: Expected, run: GhRunner
) -> Authentication:
    """Authenticate the event file at ``path`` against its GitHub attestation."""
    if not expected.tag and not expected.commit:
        return Authentication(
            "refused",
            "expected_release_missing",
            "name the tag or the commit you expect; the event has no freshness",
        )
    try:
        content = path.read_bytes()
    except OSError as error:
        return _held(f"cannot read the event: {error}", None, None)
    identified = _identify(content, expected)
    if isinstance(identified, Authentication):
        return identified
    tag, commit = identified
    try:
        result = run(gh_arguments(path, commit))
    except OSError as error:
        return _held(f"gh could not be run: {error}", tag, commit)
    if result.code != 0:
        first = (result.stderr.strip().splitlines() or ["gh failed"])[0]
        if _failed_check(result.stderr):
            return _failed(first, tag, commit)
        return _held(first, tag, commit)
    try:
        unchanged = path.read_bytes() == content
    except OSError:
        unchanged = False
    if not unchanged:
        return Authentication(
            "refused", "event_changed", "the event file changed while it was verified"
        )
    return _verdict(result.stdout, hashlib.sha256(content).hexdigest(), tag, commit)


class GitHubReadError(RuntimeError):
    """A GitHub read could not be completed or was not what was expected."""


@dataclass(frozen=True)
class MergedPull:
    """The one pull request merged AS a release commit."""

    number: int
    head: str
    merged_at: str  # RFC 3339 UTC, as GitHub prints it
    base_ref: str  # the branch it was merged into
    base_repo_id: int  # the repository it was merged into


class MergeReads(Protocol):
    """GitHub reads that map a release commit to the pull request it merged."""

    def default_branch(self, repo: str) -> str: ...

    def repo_id(self, repo: str) -> int: ...

    def is_ancestor(self, repo: str, commit: str, branch: str) -> bool: ...

    def merged_pull(self, repo: str, commit: str) -> MergedPull | None:
        """The one pull request merged as ``commit`` into the default branch;
        None when there is not exactly one distinct such pull request."""
        ...

    def approval_statuses(self, repo: str, commit: str, context: str) -> list[Status]:
        """``context``'s statuses on ``commit``, NEWEST FIRST."""
        ...


DEFAULT_BRANCH: Final = "main"
_UTC_SECOND: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)


def parse_utc(text: object) -> datetime | None:
    """A strict RFC 3339 UTC time at second precision (``YYYY-MM-DDTHH:MM:SSZ``),
    or None for anything else, the empty string included. Times are compared as
    times, never as text."""
    if not isinstance(text, str) or _UTC_SECOND.fullmatch(text) is None:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def approval_description(repo_id: int, pull: MergedPull) -> str:
    """What the approver's status says it approved: this repository, pull request
    and head (``apr1-<repo id>-<pull number>-<head>``)."""
    return f"apr1-{repo_id}-{pull.number}-{pull.head}"


def check_release_approval(
    github: MergeReads,
    *,
    repo: str,
    release_commit: str,
    approver: int = APPROVER_USER_ID,
) -> str | None:
    """None when the release commit was approved; otherwise the reason it was not.

    The release commit is a merge commit made by the merger; the council's
    approval is on the pull request's HEAD, never on the merge commit, so the
    pull request is found first. The newest ``council/approve`` status on that
    head must be a success by the adopted approver, must say it approved this
    repository, pull request and head, and must not be later than the merge.
    Authenticity of the event does not imply any of this.
    """
    try:
        branch = github.default_branch(repo)
        if branch != DEFAULT_BRANCH:
            return f"the default branch is not {DEFAULT_BRANCH}"
        if not github.is_ancestor(repo, release_commit, branch):
            return "the release commit is not on the default branch"
        pull = github.merged_pull(repo, release_commit)
        if pull is None:
            return "no single merged pull request has this commit as its merge commit"
        repo_id = github.repo_id(repo)
        statuses: list[Status] = github.approval_statuses(
            repo, pull.head, APPROVE_CONTEXT
        )
    except GitHubReadError as error:
        return f"a GitHub read did not complete, so nothing is approved: {error}"
    newest = statuses[0] if statuses else None
    if newest is None or (newest.state, newest.creator) != ("success", approver):
        return (
            f"no {APPROVE_CONTEXT} by the adopted approver on the pull request's head"
        )
    if newest.description != approval_description(repo_id, pull):
        return (
            f"the {APPROVE_CONTEXT} status does not name this repository, "
            "pull request and head"
        )
    if (pull.base_ref, pull.base_repo_id) != (DEFAULT_BRANCH, repo_id):
        return (
            f"the pull request was not merged into {DEFAULT_BRANCH} of this repository"
        )
    approved, merged = parse_utc(newest.created_at), parse_utc(pull.merged_at)
    if approved is None or merged is None or approved > merged:
        return f"the {APPROVE_CONTEXT} status is not dated, or is later than the merge"
    return None


__all__ = [
    "CERT_IDENTITY",
    "OIDC_ISSUER",
    "PREDICATE_TYPE",
    "REPOSITORY",
    "RUNNER",
    "SOURCE_REF",
    "TRIGGER",
    "Authentication",
    "Expected",
    "GhResult",
    "GhRunner",
    "DEFAULT_BRANCH",
    "GitHubReadError",
    "MergeReads",
    "MergedPull",
    "approval_description",
    "parse_utc",
    "authenticate_published_event",
    "check_release_approval",
    "gh_arguments",
]
