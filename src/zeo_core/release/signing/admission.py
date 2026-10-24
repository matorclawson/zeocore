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

import base64
import binascii
import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, cast
from urllib.parse import quote

from .inventory import (
    ChangedFile,
    EvidenceReads,
    ReviewReads,
    Status,
    check_review,
)
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
    """One product a node may ask for: its repo and its declared identifiers.

    ``identifiers`` are ``ac.zeo.<product>.<name>``, for executables and
    nested code. ``bundle_identifiers`` are the only other identifiers a
    product may sign, each an outer bundle's own ``CFBundleIdentifier``
    (SOW-32 §1.5), and each belongs to exactly one product.
    """

    repo: str
    identifiers: frozenset[str]
    bundle_identifiers: frozenset[str] = frozenset()


def check_allowlist(allowlist: Mapping[str, Mapping[str, Grant]]) -> None:
    """Raise ``ValueError`` unless every grant's identifiers are its own."""
    owners: dict[str, str] = {}
    for grants in allowlist.values():
        for product, grant in grants.items():
            prefix = f"ac.zeo.{product}."
            if not all(each.startswith(prefix) for each in grant.identifiers):
                raise ValueError(f"{product}: an identifier is not {prefix}<name>")
            for each in grant.bundle_identifiers:
                if each.startswith("ac.zeo."):
                    raise ValueError(f"{each}: a bundle identifier is not ac.zeo.")
                if owners.setdefault(each, product) != product:
                    raise ValueError(f"{each}: granted to two products")


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


check_allowlist(ALLOWLIST)

RULING_LIMITS: Final = Limits()


#: Bounds on what is read from GitHub: a repository path can be a huge file.
MAX_ANSWER: Final = 8 * 1024 * 1024
MAX_FILE: Final = 1024 * 1024
MAX_PAGES: Final = 30


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


def _ref(side: object, key: str) -> object:
    """``head.sha`` or ``base.ref`` of a pull request, or None."""
    return side.get(key) if isinstance(side, dict) else None


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
            if len(done.stdout) > MAX_ANSWER:
                raise ReviewUnreadableError("GitHub's answer is larger than read")
            return done.stdout
        if b"HTTP 404" in done.stderr:
            return None
        raise ReviewUnreadableError("GitHub could not be read")

    def _list(self, body: bytes | None) -> list[object]:
        """A JSON list from an answer; a missing answer is an empty list."""
        if body is None:
            return []
        try:
            value = json.loads(body)
        except ValueError:
            raise ReviewUnreadableError("GitHub answered with something else") from None
        if not isinstance(value, list):
            raise ReviewUnreadableError("GitHub answered with something else")
        return value

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
        """The file's bytes at ``commit``, or None; never more than 1 MiB.

        The JSON form of the contents read carries the size and, up to 1 MiB,
        the content itself, so a huge file is refused from a small answer
        instead of being fetched whole.
        """
        answer = self._json(f"repos/{repo}/contents/{quote(path)}?ref={commit}")
        if answer is None:
            return None
        size = answer.get("size")
        content = answer.get("content")
        if (
            answer.get("type") != "file"
            or answer.get("encoding") != "base64"
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size > MAX_FILE
            or not isinstance(content, str)
        ):
            raise ReviewUnreadableError("the file is not readable as a small file")
        try:
            data = base64.b64decode(content, validate=False)
        except (binascii.Error, ValueError):
            raise ReviewUnreadableError("the file's content is unreadable") from None
        if len(data) != size:
            raise ReviewUnreadableError("the file's content and size disagree")
        return data

    def approval_statuses(self, repo: str, commit: str, context: str) -> list[Status]:
        """``context``'s statuses on ``commit``, newest first, exactly named.

        Only the statuses for exactly this context are returned; the caller
        decides from the first. A creator id that is not an integer is None
        and matches nobody. A full page is held as unreadable.
        """
        statuses = self._list(
            self._api([f"repos/{repo}/commits/{commit}/statuses?per_page=100"])
        )
        if len(statuses) >= 100:
            raise ReviewUnreadableError("GitHub's statuses are unreadable")
        found: list[Status] = []
        for status in statuses:
            if not isinstance(status, dict) or status.get("context") != context:
                continue
            creator = status.get("creator")
            identifier = creator.get("id") if isinstance(creator, dict) else None
            valid = isinstance(identifier, int) and not isinstance(identifier, bool)
            found.append(
                Status(
                    state=str(status.get("state")),
                    creator=identifier if valid else None,
                )
            )
        return found

    def merged_pr_files(
        self, repo: str, head: str, branch: str
    ) -> list[ChangedFile] | None:
        """The files of the pull request merged into ``branch`` whose head is ``head``.

        ``head`` is the commit the council approved; the merge commit the merger
        makes is another commit and is never the one asked about. None when
        there is not exactly one such pull request. The files are read page by
        page, and a pull request too large to read in full is held as
        unreadable rather than judged from part of it.
        """
        pulls = self._list(
            self._api([f"repos/{repo}/commits/{head}/pulls?per_page=100"])
        )
        if len(pulls) >= 100:
            raise ReviewUnreadableError("GitHub's pull requests are unreadable")
        merged = [
            p
            for p in pulls
            if isinstance(p, dict)
            and p.get("merged_at")
            and _ref(p.get("head"), "sha") == head
            and _ref(p.get("base"), "ref") == branch
        ]
        if len(merged) != 1 or not isinstance(merged[0], dict):
            return None
        number = merged[0].get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise ReviewUnreadableError("GitHub's pull request has no number")
        files: list[ChangedFile] = []
        for page in range(1, MAX_PAGES + 1):
            chunk = self._list(
                self._api(
                    [f"repos/{repo}/pulls/{number}/files?per_page=100&page={page}"]
                )
            )
            for item in chunk:
                files.append(self._changed(item))
            if len(chunk) < 100:
                return files
        raise ReviewUnreadableError("the pull request has too many files to read")

    @staticmethod
    def _changed(item: object) -> ChangedFile:
        if not isinstance(item, dict):
            raise ReviewUnreadableError("GitHub's file list is unreadable")
        name, status, sha = item.get("filename"), item.get("status"), item.get("sha")
        if not (
            isinstance(name, str) and isinstance(status, str) and isinstance(sha, str)
        ):
            raise ReviewUnreadableError("GitHub's file list is unreadable")
        return ChangedFile(name, status, sha)

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
    # Only an outer bundle's identifier is looked up among the bundle
    # identifiers; everything else must be one of the product's ac.zeo ones.
    signed = [
        *(
            (item.identifier, item.audience, grant.identifiers)
            for item in manifest.executables
        ),
        *(
            (identifier, bundle.audience, declared)
            for bundle in manifest.bundles
            for identifier, declared in (
                (bundle.identifier, grant.bundle_identifiers),
                *((item.identifier, grant.identifiers) for item in bundle.nested),
            )
        ),
    ]
    if len(signed) > limits.max_executables:
        return Refusal(
            "refused", "request_invalid", "too many executables in one request"
        )
    for identifier, audience, declared in signed:
        if audience != "internal":
            return Refusal(
                "refused",
                "audience_not_unattended",
                "only internal releases are signed unattended",
            )
        if identifier not in declared:
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
    return _inventory_refusal(opened, grant.repo, github)


def _inventory_refusal(
    opened: OpenedRequest, repo: str, github: GitHubReads
) -> Refusal | None:
    """None unless the request's inventory lacks the review it must have.

    Keyed on the manifest's own mode: ``pins: inventory`` needs the inventory,
    its path and its version, all three; any other manifest takes none. The
    inventory is held to the same standard as the manifest: GitHub is read for
    it, never the caller (ZEOCORE-SOW-44 rev 2, D1, and the council's review).
    """
    request = opened.request
    wants = opened.manifest.signing.pins == "inventory"
    inventory = opened.inventory_bytes
    present = [
        x is not None for x in (inventory, request.inventory_path, request.version)
    ]
    if (wants and not all(present)) or (not wants and any(present)):
        return Refusal(
            "refused",
            "request_invalid",
            "the inventory, its path and its version go with pins: inventory",
        )
    if not wants or inventory is None:
        return None
    if request.inventory_path is None or request.version is None:
        return None  # unreachable: `present` covered it
    if not isinstance(github, EvidenceReads):
        return Refusal("held", "review_unreadable", "approvals can't be read here")
    try:
        why = check_review(
            cast(ReviewReads, github),
            repo=repo,
            commit=request.commit,
            path=request.inventory_path,
            version=request.version,
            content=inventory,
        )
    except ReviewUnreadableError:
        return Refusal("held", "review_unreadable", "GitHub could not be read")
    if why is not None:
        return Refusal("refused", "inventory_unreviewed", why)
    return None
