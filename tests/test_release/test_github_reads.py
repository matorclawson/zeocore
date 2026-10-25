"""The real GitHub reader for the release-approval check (SOW-44, W3).

``gh`` is replaced by a runner that answers each ``gh api`` path with canned
JSON in the shapes GitHub returned (taken from real calls on zeocore's #188 and
#190). The check is the one in ``event_attestation``; here the reader and the
verb are pinned, and every way a read can fail leaves the commit not approved.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from zeo_core.release.signing.event_attestation import (
    REPOSITORY,
    GhResult,
    check_release_approval,
)
from zeo_core.release.signing.github_reads import GhApiReader
from zeo_core.release.signing.inventory import APPROVER_USER_ID

MERGE = "a" * 40
HEAD = "b" * 40
OTHER = "c" * 40
NUMBER = 1


def _pull(**changes: Any) -> dict[str, Any]:  # noqa: ANN401
    pull: dict[str, Any] = {
        "number": NUMBER,
        "merged_at": "2026-10-25T00:00:00Z",
        "merge_commit_sha": MERGE,
        "base": {"ref": "main"},
        "head": {"sha": HEAD},
    }
    pull.update(changes)
    return pull


def _status(
    state: str = "success",
    creator: int | None = APPROVER_USER_ID,
    context: str = "council/approve",
) -> dict[str, Any]:
    return {"context": context, "state": state, "creator": {"id": creator}}


class FakeApi:
    """Answers ``gh api <path>`` from a table keyed by path prefix."""

    def __init__(self, **answers: object) -> None:
        self.answers: dict[str, object] = {
            "default": {"default_branch": "main"},
            "compare": {"status": "ahead"},
            "pulls": [[_pull()]],
            "statuses": [[_status()]],
        }
        self.answers.update(answers)
        self.calls: list[Sequence[str]] = []

    def __call__(self, argv: Sequence[str]) -> GhResult:
        self.calls.append(list(argv))
        path = argv[2]
        if path.endswith(f"repos/{REPOSITORY}"):
            key = "default"
        elif "/compare/" in path:
            key = "compare"
        elif path.split("?")[0].endswith("/pulls"):
            key = "pulls"
        elif "/statuses" in path:
            key = "statuses"
        else:
            return GhResult(1, "", "Error: unexpected path")
        answer = self.answers[key]
        if isinstance(answer, GhResult):
            return answer
        return GhResult(0, json.dumps(answer), "")


def _check(api: FakeApi) -> str | None:
    try:
        return check_release_approval(
            GhApiReader(api), repo=REPOSITORY, release_commit=MERGE
        )
    except Exception as error:  # noqa: BLE001 -- the check must answer, never raise
        pytest.fail(f"the check raised {type(error).__name__} instead of answering")


def test_a_merge_commit_whose_pull_request_head_is_approved_is_approved() -> None:
    api = FakeApi()
    assert _check(api) is None
    statuses_path = next(c[2] for c in api.calls if "/statuses" in c[2])
    assert f"/commits/{HEAD}/statuses" in statuses_path
    assert any(f"/commits/{MERGE}/pulls?" in c[2] for c in api.calls)


def test_the_approval_is_read_at_the_head_never_at_the_merge_commit() -> None:
    api = FakeApi()
    _check(api)
    assert not any(f"commits/{MERGE}/statuses" in c[2] for c in api.calls)


@pytest.mark.parametrize(
    "pulls",
    [
        [[]],
        [],
        [[_pull(merge_commit_sha=OTHER)]],
        [[_pull(merged_at=None)]],
        [[_pull(base={"ref": "other"})]],
        [[_pull(), _pull(head={"sha": OTHER}, number=2)]],
        # two distinct pull requests that share ONE head are still two
        [[_pull(), _pull(number=2)]],
        # a second qualifying pull request on a LATER page is still a second one
        [[_pull()], [_pull(number=2, head={"sha": OTHER})]],
        [[_pull(head={})]],
        [[{"number": 3}]],
        [[_pull(number=True)]],
        [[_pull(number="1")]],
        # a page that is not a list, whether first or later, fails closed
        ["not a page"],
        [[_pull()], "not a page"],
        "not pages",
    ],
)
def test_without_exactly_one_pull_request_merged_as_this_commit_it_is_not_approved(
    pulls: object,
) -> None:
    assert _check(FakeApi(pulls=pulls))


def test_two_listings_of_the_same_pull_request_are_one_pull_request() -> None:
    assert _check(FakeApi(pulls=[[_pull(), _pull()]])) is None
    assert _check(FakeApi(pulls=[[_pull()], [_pull()]])) is None


def test_only_one_qualifying_pull_request_among_other_pages_is_enough() -> None:
    other = _pull(number=9, merge_commit_sha=OTHER)
    assert _check(FakeApi(pulls=[[other], [_pull()]])) is None


def test_the_pull_request_list_is_read_over_every_page() -> None:
    api = FakeApi()
    _check(api)
    pulls_call = next(c for c in api.calls if c[2].split("?")[0].endswith("/pulls"))
    assert "--paginate" in pulls_call and "--slurp" in pulls_call


@pytest.mark.parametrize(
    "statuses",
    [
        [[]],
        [[_status(context="ci/other")]],
        [[_status(creator=1)]],
        [[_status(creator=None)]],
        [[_status(state="failure")]],
        [[_status(state="success", creator=1), _status()]],
        [[_status(state="failure"), _status()]],
    ],
)
def test_only_the_newest_approval_by_the_adopted_approver_counts(
    statuses: object,
) -> None:
    assert _check(FakeApi(statuses=statuses))


def test_the_newest_status_decides_across_pages() -> None:
    older_failure = [[_status()], [_status(state="failure")]]
    assert _check(FakeApi(statuses=older_failure)) is None
    newer_failure = [[_status(state="failure")], [_status()]]
    assert _check(FakeApi(statuses=newer_failure))


@pytest.mark.parametrize("status", ["behind", "diverged"])
def test_a_commit_not_on_the_default_branch_is_not_approved(status: str) -> None:
    assert "default branch" in (_check(FakeApi(compare={"status": status})) or "")


def test_an_identical_commit_is_on_the_branch() -> None:
    assert _check(FakeApi(compare={"status": "identical"})) is None


@pytest.mark.parametrize(
    "broken",
    [
        {"default": GhResult(1, "", "Error: HTTP 404")},
        {"compare": GhResult(1, "", "Error: HTTP 500")},
        {"pulls": GhResult(1, "", "Error: gh: not logged in")},
        {"statuses": GhResult(1, "", "Error: timeout")},
        {"default": GhResult(0, "not json", "")},
        {"default": {"default_branch": ""}},
        {"compare": {"status": "weird"}},
        {"statuses": {"message": "no"}},
    ],
)
def test_a_read_that_fails_leaves_the_commit_not_approved(
    broken: dict[str, object],
) -> None:
    reason = _check(FakeApi(**broken))
    assert reason is not None
    assert "GitHub read" in reason or "default branch" in reason or "approved" in reason


def test_gh_missing_leaves_it_not_approved() -> None:
    def missing(argv: Sequence[str]) -> GhResult:
        raise FileNotFoundError("gh")

    reason = check_release_approval(
        GhApiReader(missing), repo=REPOSITORY, release_commit=MERGE
    )
    assert reason is not None and "gh could not be run" in reason


def test_the_reader_only_reads() -> None:
    api = FakeApi()
    _check(api)
    for argv in api.calls:
        assert list(argv[:2]) == ["gh", "api"]
        assert "-X" not in argv and "--method" not in argv and "-f" not in argv


# --- the verb: zeocore release check-approval ---


def _verb(api: FakeApi, commit: str = MERGE) -> tuple[int, dict[str, Any]]:
    from zeo_core.cli.release import make_command

    status, answer = make_command(gh=api)(
        ["check-approval"], json.dumps({"commit": commit}).encode()
    )
    return status, dict(answer)


def test_the_verb_answers_done_only_for_an_approved_commit() -> None:
    from zeo_core.cli.protocol import EXIT_DONE

    status, answer = _verb(FakeApi())
    assert (status, answer["ok"], answer["status"]) == (EXIT_DONE, True, "approved")
    assert answer["outcome"] == "release_approved"


def test_the_verb_refuses_an_unapproved_commit_with_the_reason() -> None:
    from zeo_core.cli.protocol import EXIT_HELD

    status, answer = _verb(FakeApi(statuses=[[]]))
    assert (status, answer["ok"], answer["status"]) == (EXIT_HELD, False, "refused")
    assert answer["outcome"] == "release_not_approved"
    assert "council/approve" in answer["message"]


@pytest.mark.parametrize("commit", ["main", "A" * 40, "a" * 39, "g" * 40])
def test_the_verb_refuses_a_commit_that_is_not_a_full_lowercase_sha(
    commit: str,
) -> None:
    from zeo_core.cli.protocol import EXIT_INVALID

    api = FakeApi()
    status, _ = _verb(api, commit)
    assert status == EXIT_INVALID and api.calls == []


def test_the_verb_takes_no_other_field() -> None:
    from zeo_core.cli.protocol import EXIT_INVALID
    from zeo_core.cli.release import make_command

    status, _ = make_command(gh=FakeApi())(
        ["check-approval"], json.dumps({"commit": MERGE, "repo": "x/y"}).encode()
    )
    assert status == EXIT_INVALID
