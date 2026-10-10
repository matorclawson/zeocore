"""The merger's GitHub adapter, under ZEO-LEDGER's adapter rules (SOW-151)."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from zeo_core.merger import MergeNotSentError, MergeUnclearError
from zeo_core.merger.github import GITHUB_API, GitHubClient, GitHubError, parse_vetoes

REPO = "zeroemployeeorg/zeocore"
HEAD = "c6a0b8cb1e9ad9aa6b3115bbf49f2fdc135568b0"


class Tokens:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def token(self, repository: str) -> str:
        self.asked.append(repository)
        return f"tok-{repository}"


Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> tuple[GitHubClient, Tokens, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    tokens = Tokens()
    client = GitHubClient(
        tokens=tokens,
        veto_repo="rodriveracom/org-zeroemployeeorg",
        veto_path="operator/veto-list.md",
        http_client=httpx.Client(
            base_url=GITHUB_API, transport=httpx.MockTransport(record)
        ),
    )
    return client, tokens, seen


PR_BODY: dict[str, Any] = {
    "state": "open",
    "merged": False,
    "merge_commit_sha": None,
    "draft": False,
    "mergeable_state": "clean",
    "user": {"id": 9001},
    "head": {"sha": HEAD},
    "base": {"ref": "main", "sha": "3" * 40, "repo": {"id": 1337139177}},
}


def test_a_pr_lists_every_file_and_the_name_a_rename_came_from() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files"):
            page = int(request.url.params["page"])
            if page == 1:
                files = [{"filename": f"f{i}"} for i in range(99)]
                files.append(
                    {"filename": "new.txt", "previous_filename": ".zeomerger/x"}
                )
                return httpx.Response(200, json=files)
            return httpx.Response(200, json=[{"filename": "last"}])
        return httpx.Response(200, json=PR_BODY)

    client, tokens, seen = _client(handler)
    pr = client.pull_request(REPO, 85)
    assert (pr.head_sha, pr.base_repo_id, pr.author_id) == (HEAD, 1337139177, 9001)
    assert ".zeomerger/x" in pr.files and "last" in pr.files
    assert set(tokens.asked) == {REPO}
    assert seen[0].headers["Authorization"] == f"Bearer tok-{REPO}"


def test_more_than_3000_files_holds() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=[{"filename": "x"}] * 100)
        return httpx.Response(200, json=PR_BODY)

    client, _, _ = _client(handler)
    with pytest.raises(GitHubError, match="3000"):
        client.pull_request(REPO, 85)


@pytest.mark.parametrize("count", [250, 260])
def test_250_or_more_commits_holds_because_github_truncates(count: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        left = max(0, min(100, count - (page - 1) * 100))
        return httpx.Response(
            200, json=[{"sha": "a" * 40, "author": {"id": 1}, "committer": None}] * left
        )

    client, _, _ = _client(handler)
    with pytest.raises(GitHubError, match="250"):
        client.commits(REPO, 85)


def test_commits_carry_account_ids_and_zero_when_unlinked() -> None:
    client, _, _ = _client(
        lambda r: httpx.Response(
            200, json=[{"sha": "a" * 40, "author": {"id": 7}, "committer": None}]
        )
    )
    (commit,) = client.commits(REPO, 85)
    assert (commit.author_id, commit.committer_id) == (7, 0)


def test_the_veto_list_is_read_from_main_and_its_blob_sha_is_the_revision() -> None:
    text = "# Vetoes\n- zeroemployeeorg/zeocore#85 not yet\nprose\n- bad\n"
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "sha": "blob1",
                "encoding": "base64",
                "content": base64.b64encode(text.encode()).decode(),
            },
        )

    client, tokens, _ = _client(handler)
    vetoes, revision = client.vetoes()
    assert vetoes == frozenset({"zeroemployeeorg/zeocore#85"}) and revision == "blob1"
    assert seen_paths[0].endswith("/contents/operator/veto-list.md?ref=main")
    assert tokens.asked == ["rodriveracom/org-zeroemployeeorg"]


def test_a_missing_veto_list_raises_so_the_merger_holds() -> None:
    client, _, _ = _client(lambda r: httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(GitHubError, match="404"):
        client.vetoes()


def test_required_checks_reads_the_active_rules() -> None:
    rules = [
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [{"context": "verify"}],
                "strict_required_status_checks_policy": False,
            },
        },
        {"type": "merge_queue", "parameters": {}},
    ]
    client, _, _ = _client(lambda r: httpx.Response(200, json=rules))
    assert client.required_checks(REPO, "main") == (True, True)
    client, _, _ = _client(lambda r: httpx.Response(200, json=[]))
    assert client.required_checks(REPO, "main") == (False, False)


def test_a_merge_sends_sha_and_the_merge_method_once() -> None:
    client, _, seen = _client(
        lambda r: httpx.Response(200, json={"sha": "m" * 40, "merged": True})
    )
    assert client.merge(REPO, 85, HEAD) == "m" * 40
    (request,) = seen
    assert (
        request.method == "PUT" and request.url.path == f"/repos/{REPO}/pulls/85/merge"
    )
    assert json.loads(request.content) == {"sha": HEAD, "merge_method": "merge"}


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(502),
        httpx.Response(200, json={"merged": False}),
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("dropped"),
    ],
)
def test_an_unknown_merge_outcome_is_unclear_and_sent_once(
    answer: httpx.Response | Exception,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(answer, Exception):
            raise answer
        return answer

    client, _, seen = _client(handler)
    with pytest.raises(MergeUnclearError):
        client.merge(REPO, 85, HEAD)
    assert len(seen) == 1


@pytest.mark.parametrize(
    "failure", [httpx.ConnectError("refused"), httpx.ConnectTimeout("no route")]
)
def test_a_merge_that_never_connected_is_not_sent(failure: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    client, _, _ = _client(handler)
    with pytest.raises(MergeNotSentError):
        client.merge(REPO, 85, HEAD)


def test_a_merge_without_a_token_is_not_sent() -> None:
    class NoTokens:
        def token(self, repository: str) -> str:
            raise RuntimeError("keychain locked")

    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"merged": True, "sha": "1" * 40})

    client = GitHubClient(
        tokens=NoTokens(),
        veto_repo="rodriveracom/org-zeroemployeeorg",
        veto_path="operator/veto-list.md",
        http_client=httpx.Client(
            base_url=GITHUB_API, transport=httpx.MockTransport(record)
        ),
    )
    with pytest.raises(MergeNotSentError, match="keychain locked"):
        client.merge(REPO, 85, HEAD)
    assert seen == []


@pytest.mark.parametrize("status", [405, 409, 422])
def test_a_4xx_merge_answer_is_a_refusal(status: int) -> None:
    client, _, _ = _client(
        lambda r: httpx.Response(status, json={"message": "head sha didn't match"})
    )
    with pytest.raises(GitHubError) as caught:
        client.merge(REPO, 85, HEAD)
    assert not isinstance(caught.value, MergeUnclearError)
    assert caught.value.status == status


def test_an_injected_client_that_follows_redirects_is_refused() -> None:
    with pytest.raises(ValueError, match="must not follow redirects"):
        GitHubClient(
            tokens=Tokens(),
            veto_repo="rodriveracom/org-zeroemployeeorg",
            veto_path="operator/veto-list.md",
            http_client=httpx.Client(base_url=GITHUB_API, follow_redirects=True),
        )


def test_redirects_are_never_followed_and_bodies_are_bounded() -> None:
    client, _, seen = _client(
        lambda r: httpx.Response(302, headers={"Location": "https://evil.example"})
    )
    with pytest.raises(GitHubError, match="302"):
        client.statuses(REPO, HEAD)
    assert len(seen) == 1
    client, _, _ = _client(
        lambda r: httpx.Response(200, content=b"[" + b" " * (8 << 20) + b"]")
    )
    with pytest.raises(GitHubError, match="8 MiB"):
        client.statuses(REPO, HEAD)


def test_a_bad_repository_name_is_refused_before_any_call() -> None:
    client, tokens, seen = _client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(GitHubError, match="bad repository"):
        client.pull_request("owner/repo?x=1", 1)
    assert seen == [] and tokens.asked == []


def test_parse_vetoes_takes_only_entries() -> None:
    assert parse_vetoes("- a/b#1\n-c/d#2\n- e/f\n  - g/h#3 reason\n") == frozenset(
        {"a/b#1", "g/h#3"}
    )
