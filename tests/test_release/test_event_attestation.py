"""Authenticating the published release event by GitHub attestation (SOW-44, M2).

``gh`` is replaced by a runner that returns the JSON shape and the failure
messages observed from gh 2.101.0 (the shape was taken from a real
``gh attestation verify --format json`` run, the identity values from a real
publish.yml certificate). Nothing here needs the network.

Each pin is shown load-bearing twice: its flag is in the command line, and a
verification result that violates it is refused even when the (fake) ``gh``
ignores that flag, because the result is checked again.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from zeo_core.release.signing.event_attestation import (
    CERT_IDENTITY,
    OIDC_ISSUER,
    PREDICATE_TYPE,
    REPOSITORY,
    Authentication,
    Expected,
    GhResult,
    authenticate_published_event,
    check_release_approval,
    gh_arguments,
)
from zeo_core.release.signing.event_auth import EVENT_SCHEMA, canonical
from zeo_core.release.signing.inventory import (
    APPROVER_USER_ID,
    ChangedFile,
    Status,
)

COMMIT = "a" * 40
TAG = "v9.9.9"
EXPECTED_TAG = Expected(tag=TAG)


def _event(**changes: Any) -> bytes:  # noqa: ANN401
    body: dict[str, Any] = {
        "schema": EVENT_SCHEMA,
        "repository": REPOSITORY,
        "tag": TAG,
        "tag_object": "1" * 40,
        "commit": COMMIT,
        "tree": "3" * 40,
        "notes_sha256": "4" * 64,
        "artifacts": [{"path": "z.whl", "size": 5, "sha256": "5" * 64}],
    }
    body.update(changes)
    digest = hashlib.sha256(canonical(body)).hexdigest()
    return json.dumps({**body, "event_sha256": digest}, indent=2).encode()


def _entry(content: bytes, **certificate: object) -> dict[str, Any]:
    """One verified attestation as ``gh attestation verify --format json`` prints it."""
    cert: dict[str, object] = {
        "subjectAlternativeName": CERT_IDENTITY,
        "issuer": OIDC_ISSUER,
        "githubWorkflowTrigger": "push",
        "buildTrigger": "push",
        "buildSignerDigest": COMMIT,
        "runnerEnvironment": "github-hosted",
        "sourceRepositoryDigest": COMMIT,
        "sourceRepositoryRef": "refs/heads/main",
    }
    cert.update(certificate)
    return {
        "attestation": {},
        "verificationResult": {
            "signature": {"certificate": cert},
            "statement": {
                "predicateType": PREDICATE_TYPE,
                "subject": [
                    {
                        "name": "release-event.json",
                        "digest": {"sha256": hashlib.sha256(content).hexdigest()},
                    }
                ],
            },
        },
    }


class FakeGh:
    """Prints a fixed result and records the command line it was given."""

    def __init__(self, result: GhResult) -> None:
        self.result = result
        self.calls: list[Sequence[str]] = []

    def __call__(self, argv: Sequence[str]) -> GhResult:
        self.calls.append(list(argv))
        return self.result


def _ok(*entries: dict[str, Any]) -> GhResult:
    return GhResult(0, json.dumps(list(entries)), "")


def _run(
    tmp_path: Path,
    gh: FakeGh,
    expected: Expected = EXPECTED_TAG,
    content: bytes | None = None,
) -> Authentication:
    path = tmp_path / "release-event.json"
    path.write_bytes(_event() if content is None else content)
    return authenticate_published_event(path, expected, gh)


def test_the_command_line_has_every_pin(tmp_path: Path) -> None:
    argv = gh_arguments(tmp_path / "e.json", COMMIT)
    assert argv == [
        "gh",
        "attestation",
        "verify",
        str(tmp_path / "e.json"),
        "--repo",
        "zeroemployeeorg/zeocore",
        "--cert-identity",
        "https://github.com/zeroemployeeorg/zeocore/.github/workflows/publish.yml@refs/heads/main",
        "--cert-oidc-issuer",
        "https://token.actions.githubusercontent.com",
        "--source-ref",
        "refs/heads/main",
        "--source-digest",
        COMMIT,
        "--signer-digest",
        COMMIT,
        "--deny-self-hosted-runners",
        "--predicate-type",
        "https://slsa.dev/provenance/v1",
        "--format",
        "json",
    ]
    assert "--signer-workflow" not in argv


def test_a_matching_attestation_authenticates_and_says_what_that_means(
    tmp_path: Path,
) -> None:
    content = _event()
    gh = FakeGh(_ok(_entry(content)))
    result = _run(tmp_path, gh, content=content)
    assert (result.status, result.reason) == ("authenticated", "event_authenticated")
    assert (result.tag, result.commit) == (TAG, COMMIT)
    assert "not release authorization" in result.message
    assert gh.calls and gh.calls[0][:3] == ["gh", "attestation", "verify"]


def test_one_good_attestation_among_others_is_enough(tmp_path: Path) -> None:
    content = _event()
    bad = _entry(
        content,
        subjectAlternativeName="https://github.com/x/y/.github/workflows/p.yml@refs/heads/main",
    )
    assert _run(
        tmp_path, FakeGh(_ok(bad, _entry(content))), content=content
    ).status == ("authenticated")


# One mutant per pin: gh ignores the flag (as if it were missing), the result
# violates the pin, and the second check must refuse it.
VIOLATIONS: dict[str, dict[str, object]] = {
    "cert-identity": {
        "subjectAlternativeName": "https://github.com/zeroemployeeorg/zeocore/.github/workflows/other.yml@refs/heads/main"
    },
    "cert-identity-prefix": {"subjectAlternativeName": CERT_IDENTITY + "-evil"},
    "cert-oidc-issuer": {"issuer": "https://accounts.example.com"},
    "source-ref": {"sourceRepositoryRef": "refs/heads/feature"},
    "source-digest": {"sourceRepositoryDigest": "b" * 40},
    "signer-digest": {"buildSignerDigest": "b" * 40},
    "deny-self-hosted-runners": {"runnerEnvironment": "self-hosted"},
    "trigger-build": {"buildTrigger": "workflow_dispatch"},
    "trigger-workflow": {"githubWorkflowTrigger": "pull_request"},
}


@pytest.mark.parametrize("pin", sorted(VIOLATIONS))
def test_a_result_that_violates_a_pin_is_refused_even_if_gh_ignored_the_flag(
    tmp_path: Path, pin: str
) -> None:
    content = _event()
    gh = FakeGh(_ok(_entry(content, **VIOLATIONS[pin])))
    result = _run(tmp_path, gh, content=content)
    assert (result.status, result.reason) == ("refused", "event_authentication_failed")


def test_a_wrong_predicate_type_is_refused(tmp_path: Path) -> None:
    content = _event()
    entry = copy.deepcopy(_entry(content))
    entry["verificationResult"]["statement"]["predicateType"] = "https://example.com/p"
    result = _run(tmp_path, FakeGh(_ok(entry)), content=content)
    assert result.status == "refused"


def test_an_attestation_for_other_bytes_is_refused(tmp_path: Path) -> None:
    other = _event(tag="v0.0.1")
    result = _run(tmp_path, FakeGh(_ok(_entry(other))))
    assert (result.status, result.reason) == ("refused", "event_authentication_failed")


@pytest.mark.parametrize(
    "stderr",
    [
        "Error: expected SourceRepositoryDigest to be 0000, got 1111",
        'Error: verifying with issuer "sigstore.dev"',
    ],
)
def test_a_failed_check_reported_by_gh_is_refused(tmp_path: Path, stderr: str) -> None:
    result = _run(tmp_path, FakeGh(GhResult(1, "", stderr + "\n")))
    assert (result.status, result.reason) == ("refused", "event_authentication_failed")


@pytest.mark.parametrize(
    "stderr",
    [
        "Error: no attestations found",
        "Error: dial tcp: lookup api.github.com: no such host",
        "",
    ],
)
def test_a_check_that_could_not_complete_is_held_never_authenticated(
    tmp_path: Path, stderr: str
) -> None:
    result = _run(tmp_path, FakeGh(GhResult(1, "", stderr)))
    assert (result.status, result.reason) == (
        "held",
        "event_authentication_unavailable",
    )


def test_gh_missing_is_held(tmp_path: Path) -> None:
    def missing(argv: Sequence[str]) -> GhResult:
        raise FileNotFoundError("gh")

    path = tmp_path / "e.json"
    path.write_bytes(_event())
    result = authenticate_published_event(path, Expected(tag=TAG), missing)
    assert (result.status, result.reason) == (
        "held",
        "event_authentication_unavailable",
    )


@pytest.mark.parametrize("stdout", ["", "not json", "[]", "{}"])
def test_unreadable_or_empty_output_after_success_is_held(
    tmp_path: Path, stdout: str
) -> None:
    result = _run(tmp_path, FakeGh(GhResult(0, stdout, "")))
    assert result.status == "held"


def test_an_unreadable_event_file_is_held(tmp_path: Path) -> None:
    result = authenticate_published_event(
        tmp_path / "missing.json", Expected(tag=TAG), FakeGh(_ok())
    )
    assert (result.status, result.reason) == (
        "held",
        "event_authentication_unavailable",
    )


def test_it_refuses_without_an_expected_tag_or_commit(tmp_path: Path) -> None:
    gh = FakeGh(_ok(_entry(_event())))
    result = _run(tmp_path, gh, expected=Expected())
    assert (result.status, result.reason) == ("refused", "expected_release_missing")
    assert gh.calls == []


@pytest.mark.parametrize(
    "expected",
    [
        Expected(tag="v0.0.1"),
        Expected(commit="c" * 40),
        Expected(tag=TAG, commit="c" * 40),
    ],
)
def test_an_event_that_is_not_the_release_asked_for_is_refused(
    tmp_path: Path, expected: Expected
) -> None:
    gh = FakeGh(_ok(_entry(_event())))
    result = _run(tmp_path, gh, expected=expected)
    assert (result.status, result.reason) == ("refused", "event_not_expected")
    assert gh.calls == []


def test_an_expected_commit_alone_is_enough(tmp_path: Path) -> None:
    content = _event()
    result = _run(
        tmp_path, FakeGh(_ok(_entry(content))), Expected(commit=COMMIT), content
    )
    assert result.status == "authenticated"


def test_an_event_for_another_repository_is_refused(tmp_path: Path) -> None:
    gh = FakeGh(_ok())
    result = _run(tmp_path, gh, content=_event(repository="org/other"))
    assert (result.status, result.reason) == ("refused", "event_other_repository")
    assert gh.calls == []


def test_an_event_that_is_not_the_canonical_document_is_refused(tmp_path: Path) -> None:
    broken = json.loads(_event())
    broken["event_sha256"] = "0" * 64
    gh = FakeGh(_ok())
    result = _run(tmp_path, gh, content=json.dumps(broken).encode())
    assert (result.status, result.reason) == ("refused", "event_invalid")
    assert gh.calls == []


def test_a_malformed_commit_is_refused(tmp_path: Path) -> None:
    result = _run(tmp_path, FakeGh(_ok()), content=_event(commit="not-a-sha"))
    assert (result.status, result.reason) == ("refused", "event_invalid")


def test_a_file_changed_during_verification_is_refused(tmp_path: Path) -> None:
    content = _event()
    path = tmp_path / "e.json"
    path.write_bytes(content)

    def swapping(argv: Sequence[str]) -> GhResult:
        path.write_bytes(content + b" ")
        return _ok(_entry(content))

    result = authenticate_published_event(path, Expected(tag=TAG), swapping)
    assert (result.status, result.reason) == ("refused", "event_changed")


# --- the release commit is a merge commit; approval is on the PR head (M2-6) ---


class FakeGitHub:
    def __init__(
        self, head: str | None, statuses: list[Status], ancestor: bool = True
    ) -> None:
        self.head, self.statuses, self.ancestor = head, statuses, ancestor
        self.status_asked: list[str] = []

    def default_branch(self, repo: str) -> str:
        return "main"

    def is_ancestor(self, repo: str, commit: str, branch: str) -> bool:
        return self.ancestor

    def pr_head_of_merge_commit(self, repo: str, commit: str) -> str | None:
        return self.head

    def approval_statuses(self, repo: str, commit: str, context: str) -> list[Status]:
        self.status_asked.append(commit)
        return self.statuses

    def merged_pr_files(
        self, repo: str, head: str, branch: str
    ) -> list[ChangedFile] | None:
        return None


APPROVED = [Status("success", APPROVER_USER_ID)]


def test_approval_is_read_at_the_pull_request_head_not_the_merge_commit() -> None:
    github = FakeGitHub("h" * 40, APPROVED)
    assert (
        check_release_approval(github, repo=REPOSITORY, release_commit=COMMIT) is None
    )
    assert github.status_asked == ["h" * 40]


@pytest.mark.parametrize(
    "github",
    [
        FakeGitHub(None, APPROVED),
        FakeGitHub("h" * 40, []),
        FakeGitHub("h" * 40, [Status("success", 1)]),
        FakeGitHub("h" * 40, [Status("failure", APPROVER_USER_ID)]),
        FakeGitHub("h" * 40, APPROVED, ancestor=False),
    ],
)
def test_an_unapproved_or_unmapped_release_commit_is_not_approved(
    github: FakeGitHub,
) -> None:
    assert check_release_approval(github, repo=REPOSITORY, release_commit=COMMIT)


def test_authenticity_alone_never_approves() -> None:
    # The two checks are separate functions with separate inputs: an
    # authenticated event carries no approval, and the approval check takes no
    # event. This pins that nothing in Authentication says "approved".
    assert not hasattr(Authentication("authenticated", "r", "m"), "approved")


def test_a_failing_gh_is_never_authenticated_even_with_a_good_looking_result(
    tmp_path: Path,
) -> None:
    content = _event()
    for stderr in ("Error: no attestations found", "Error: expected X to be Y"):
        gh = FakeGh(GhResult(1, json.dumps([_entry(content)]), stderr))
        assert _run(tmp_path, gh, content=content).status != "authenticated"


# --- the verb: zeocore release verify-event ---


def _verb(tmp_path: Path, gh: FakeGh, **request: str) -> tuple[int, dict[str, Any]]:
    from zeo_core.cli.release import make_command

    path = tmp_path / "release-event.json"
    path.write_bytes(_event())
    body = json.dumps({"event": str(path), **request}).encode()
    status, answer = make_command(gh=gh)(["verify-event"], body)
    return status, dict(answer)


def test_the_verb_answers_done_only_when_authenticated(tmp_path: Path) -> None:
    from zeo_core.cli.protocol import EXIT_DONE

    status, answer = _verb(tmp_path, FakeGh(_ok(_entry(_event()))), tag=TAG)
    assert (status, answer["ok"], answer["status"]) == (
        EXIT_DONE,
        True,
        "authenticated",
    )
    assert (answer["tag"], answer["commit"]) == (TAG, COMMIT)


@pytest.mark.parametrize(
    "gh,request_fields,outcome",
    [
        (
            FakeGh(GhResult(1, "", "Error: no attestations found")),
            {"tag": TAG},
            "event_authentication_unavailable",
        ),
        (
            FakeGh(GhResult(1, "", 'Error: verifying with issuer "x"')),
            {"tag": TAG},
            "event_authentication_failed",
        ),
        (FakeGh(_ok()), {}, "expected_release_missing"),
    ],
)
def test_the_verb_holds_or_refuses_with_a_non_zero_exit(
    tmp_path: Path, gh: FakeGh, request_fields: dict[str, str], outcome: str
) -> None:
    from zeo_core.cli.protocol import EXIT_HELD

    status, answer = _verb(tmp_path, gh, **request_fields)
    assert (status, answer["ok"], answer["outcome"]) == (EXIT_HELD, False, outcome)


def test_the_verb_refuses_other_fields(tmp_path: Path) -> None:
    from zeo_core.cli.protocol import EXIT_INVALID

    status, _ = _verb(tmp_path, FakeGh(_ok()), tag=TAG, key="secret")
    assert status == EXIT_INVALID
