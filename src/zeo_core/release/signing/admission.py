"""What authorises an unattended signature on matormini (ZEOCORE-SOW-24 §1).

Every condition is checked before any signer is used. Only code merged to the
owner repo's default branch, read back from GitHub by matormini itself, is
signed, and only for the node, product and identifiers on the reviewed
allowlist below. This proves that the manifest was reviewed. It does not prove
that the bytes were built from that source: the build host is trusted for its
build.

The rule is the operator's to sign. It takes force with the code-signing
ruling that carries it, not with this file.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import quote

from .pack import OpenedRequest
from .toolchain import Runner, run_tool

#: The bounds in SOW-24 §1(f). Configuration may tighten them, never loosen
#: them: the ruling that carries the rule states these numbers.
MAX_EXECUTABLES: Final = 16
MAX_REQUESTS_PER_HOUR: Final = 12
MAX_EXECUTABLES_VARIABLE: Final = "ZEOCORE_SIGNING_MAX_EXECUTABLES"
MAX_REQUESTS_VARIABLE: Final = "ZEOCORE_SIGNING_MAX_REQUESTS_PER_HOUR"


@dataclass(frozen=True)
class Limits:
    max_executables: int = MAX_EXECUTABLES
    max_requests_per_hour: int = MAX_REQUESTS_PER_HOUR

    def __post_init__(self) -> None:
        if not 0 < self.max_executables <= MAX_EXECUTABLES:
            raise ValueError(f"max_executables must be 1 to {MAX_EXECUTABLES}")
        if not 0 < self.max_requests_per_hour <= MAX_REQUESTS_PER_HOUR:
            raise ValueError(
                f"max_requests_per_hour must be 1 to {MAX_REQUESTS_PER_HOUR}"
            )

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> Limits:
        """Read the tightened bounds; an unset variable keeps the ruling's."""
        values = {}
        for field, variable in (
            ("max_executables", MAX_EXECUTABLES_VARIABLE),
            ("max_requests_per_hour", MAX_REQUESTS_VARIABLE),
        ):
            if variable in environ:
                try:
                    values[field] = int(environ[variable])
                except ValueError:
                    raise ValueError(f"{variable} must be a whole number") from None
        return cls(**values)


@dataclass(frozen=True)
class Grant:
    """One product a node may ask for: its repo and its declared identifiers."""

    repo: str
    identifiers: frozenset[str]


#: node -> product -> grant. Reviewed with zeocore's source, never configured
#: on the host. A node not named here is refused.
ALLOWLIST: Final[Mapping[str, Mapping[str, Grant]]] = {
    "eng-uk-0006.tail29b7b0.ts.net": {
        "pomar": Grant(
            repo="zeroemployeeorg/pomar",
            identifiers=frozenset(
                f"ac.zeo.pomar.{name}"
                for name in (
                    "pomar",
                    "pomar-host",
                    "pomar-agent-host",
                    "pomar-agent-login",
                    "pomar-agent-export",
                    "pomar-hygiene",
                    "pomar-ci-caller",
                )
            ),
        )
    }
}


RULING_LIMITS: Final = Limits()


class ReviewUnreadableError(RuntimeError):
    """GitHub could not be read: the request is held, never assumed reviewed."""


class GitHubReads(Protocol):
    def default_branch(self, repo: str) -> str: ...

    def file_at(self, repo: str, commit: str, path: str) -> bytes | None:
        """The file's bytes at ``commit``, or None if it is not there."""
        ...

    def is_ancestor(self, repo: str, commit: str, branch: str) -> bool:
        """Whether ``commit`` is reachable from ``branch``: merged, so reviewed."""
        ...


class GhCli:
    """GitHub reads through the ``gh`` CLI, so no token passes through zeocore."""

    def __init__(self, run: Runner = run_tool, gh: str = "gh") -> None:
        self._run = run
        self._gh = gh

    def _api(self, argv: Sequence[str]) -> bytes | None:
        """The answer's body, or None when GitHub says it does not exist."""
        try:
            done = self._run([self._gh, "api", *argv])
        except (OSError, subprocess.SubprocessError) as error:
            raise ReviewUnreadableError("gh could not be run") from error
        if done.returncode == 0:
            return done.stdout
        if b"HTTP 404" in done.stderr:
            return None
        raise ReviewUnreadableError("GitHub could not be read")

    def _json(self, endpoint: str) -> dict[str, object] | None:
        body = self._api([endpoint])
        if body is None:
            return None
        try:
            value = json.loads(body)
        except ValueError:
            raise ReviewUnreadableError("GitHub answered with something else") from None
        if not isinstance(value, dict):
            raise ReviewUnreadableError("GitHub answered with something else")
        return value

    def default_branch(self, repo: str) -> str:
        answer = self._json(f"repos/{repo}") or {}
        branch = answer.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise ReviewUnreadableError("the repo has no default branch")
        return branch

    def file_at(self, repo: str, commit: str, path: str) -> bytes | None:
        return self._api(
            [
                "-H",
                "Accept: application/vnd.github.raw",
                f"repos/{repo}/contents/{quote(path)}?ref={commit}",
            ]
        )

    def is_ancestor(self, repo: str, commit: str, branch: str) -> bool:
        answer = self._json(f"repos/{repo}/compare/{quote(branch)}...{commit}")
        if answer is None:
            return False  # the commit is not in this repo
        status = answer.get("status")
        if status not in ("identical", "behind", "ahead", "diverged"):
            raise ReviewUnreadableError("GitHub's comparison is unreadable")
        return status in ("identical", "behind")


@dataclass(frozen=True)
class Refusal:
    status: str  # "refused" or "held"
    reason: str
    message: str


def admit(
    opened: OpenedRequest,
    node: str,
    github: GitHubReads,
    allowlist: Mapping[str, Mapping[str, Grant]] = ALLOWLIST,
    limits: Limits = RULING_LIMITS,
) -> Refusal | None:
    """None when the request may be signed; otherwise why not (SOW-24 §1)."""
    request, manifest = opened.request, opened.manifest.signing
    grant = allowlist.get(node, {}).get(request.product)
    if grant is None:
        return Refusal(
            "refused", "caller_unadmitted", "this node may not ask for this product"
        )
    # A bundle counts once for itself and once for each nested item (SOW-32 §4).
    signed = [
        *((item.identifier, item.audience) for item in manifest.executables),
        *(
            (identifier, bundle.audience)
            for bundle in manifest.bundles
            for identifier in (
                bundle.identifier,
                *(item.identifier for item in bundle.nested),
            )
        ),
    ]
    if len(signed) > limits.max_executables:
        return Refusal(
            "refused", "request_invalid", "too many executables in one request"
        )
    for identifier, audience in signed:
        if audience != "internal":
            return Refusal(
                "refused",
                "audience_not_unattended",
                "only internal releases are signed unattended",
            )
        if identifier not in grant.identifiers:
            return Refusal(
                "refused",
                "identifier_undeclared",
                f"{identifier} is not declared for {request.product}",
            )
    # The repo is the allowlist's, never the request's.
    if request.repo != grant.repo:
        return Refusal(
            "refused", "manifest_unreviewed", "the request names another repo"
        )
    try:
        branch = github.default_branch(grant.repo)
        reviewed = github.file_at(grant.repo, request.commit, request.manifest_path)
        merged = github.is_ancestor(grant.repo, request.commit, branch)
    except ReviewUnreadableError:
        return Refusal("held", "review_unreadable", "GitHub could not be read")
    if reviewed != opened.manifest_bytes or not merged:
        return Refusal(
            "refused",
            "manifest_unreviewed",
            "the manifest is not the one merged to the default branch",
        )
    return None
