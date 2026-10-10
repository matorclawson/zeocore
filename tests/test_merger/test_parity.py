"""The port is held to ZEO-LEDGER's language-neutral parity fixtures.

``contracts/merger-v1/`` is committed byte for byte from records/zeo-ledger
(44d67dfe, pass v3 added), with its SHA256SUMS. Each decide case is one
candidate against GitHub's state; each pass case is one whole run over PR #85
at the decide fixture's healthy state ('allowed').
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from zeo_core.merger import (
    ATTEMPTED,
    NOT_SENT,
    Commit,
    Config,
    MergeNotSentError,
    MergeUnclearError,
    Pass,
    PullRequest,
    Receipt,
    Repo,
    Status,
    decide,
    parse_register,
)
from zeo_core.merger.run import PassError

FIXTURES = Path(__file__).resolve().parents[2] / "contracts" / "merger-v1"
OTHER_SHA = "1" * 40


def _load(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return loaded


def test_the_fixtures_are_the_published_bytes() -> None:
    for line in (FIXTURES / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split()
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest


def _config(raw: dict[str, Any], *, live: bool = False) -> Config:
    return Config(
        repos={
            name: Repo(id=repo["ID"], base_policy=repo["BasePolicy"])
            for name, repo in raw["repos"].items()
        },
        architect_user_id=raw["architect_user_id"],
        role_app_ids={account: seat for seat, account in raw["role_apps"].items()},
        merger_user_id=raw["merger_user_id"],
        merger_repo=raw["merger_repo"],
        protected_paths=tuple(raw.get("protected_paths") or ()),
        live=live,
    )


@dataclass
class FakeGitHub:
    prs: dict[int, PullRequest]
    statuses_by_sha: dict[str, list[Status]]
    commits_by_pr: dict[int, list[Commit]]
    veto_list: frozenset[str] = frozenset()
    veto_error: str = ""
    base_rules: dict[str, tuple[bool, bool]] = field(default_factory=dict)
    base_rules_error: str = ""
    merge_answer: str = "ok"
    merges: list[str] = field(default_factory=list)

    @classmethod
    def from_case(cls, case: dict[str, Any]) -> FakeGitHub:
        return cls(
            prs={
                int(number): PullRequest(
                    state=pr["State"],
                    merged=pr["Merged"],
                    merge_commit=pr["MergeCommit"],
                    draft=pr["Draft"],
                    head_sha=pr["HeadSHA"],
                    base_ref=pr["BaseRef"],
                    base_sha=pr["BaseSHA"],
                    base_repo_id=pr["BaseRepoID"],
                    author_id=pr["AuthorID"],
                    mergeable_state=pr["MergeableState"],
                    files=tuple(pr["Files"] or ()),
                )
                for number, pr in (case["prs"] or {}).items()
            },
            statuses_by_sha={
                sha: [
                    Status(s["Context"], s["State"], s["Description"], s["CreatorID"])
                    for s in statuses or ()
                ]
                for sha, statuses in (case["statuses"] or {}).items()
            },
            commits_by_pr={
                int(number): [
                    Commit(c["SHA"], c["AuthorID"], c["CommitterID"])
                    for c in commits or ()
                ]
                for number, commits in (case["commits"] or {}).items()
            },
            veto_list=frozenset(case.get("vetoes") or ()),
            veto_error=case.get("veto_error") or "",
            base_rules={
                key: (rule["required_checks_present"], rule["strict_or_merge_queue"])
                for key, rule in (case.get("base_rules") or {}).items()
            },
            base_rules_error=case.get("base_rules_error") or "",
        )

    def pull_request(self, repo: str, number: int) -> PullRequest:
        if number not in self.prs:
            raise LookupError("not found")
        return self.prs[number]

    def statuses(self, repo: str, sha: str) -> list[Status]:
        return self.statuses_by_sha.get(sha, [])

    def commits(self, repo: str, number: int) -> list[Commit]:
        return self.commits_by_pr.get(number, [])

    def required_checks(self, repo: str, branch: str) -> tuple[bool, bool]:
        if self.base_rules_error:
            raise RuntimeError(self.base_rules_error)
        return self.base_rules.get(f"{repo}@{branch}", (False, False))

    def vetoes(self) -> tuple[frozenset[str], str]:
        if self.veto_error:
            raise RuntimeError(self.veto_error)
        return self.veto_list, "veto-rev-1"

    def merge(self, repo: str, number: int, sha: str) -> str:
        self.merges.append(f"{repo}#{number}@{sha}")
        landed = self.merge_answer in ("ok", "unclear-landed")
        if landed:
            self.prs[number] = replace(
                self.prs[number], state="closed", merged=True, merge_commit=OTHER_SHA
            )
        if self.merge_answer == "not-sent":
            raise MergeNotSentError("merge call not sent: token for repo: locked")
        if self.merge_answer == "refused-409":
            raise RuntimeError("409 head sha didn't match")
        if self.merge_answer.startswith("unclear"):
            raise MergeUnclearError("timeout: merge outcome unclear")
        return OTHER_SHA


# v2 supersedes v1 for decide (ZEO-LEDGER SOW-152: Architect M1); pass v3
# supersedes v2 (ZEO-LEDGER SOW-156: not-sent, Architect P1 on #101).
DECIDE = _load("parity-decide-v2.json")
PASS = _load("parity-pass-v3.json")


def test_the_specs_are_the_ones_ported() -> None:
    assert DECIDE["spec"] == "zeomerger-parity/v2"
    assert PASS["spec"] == "zeomerger-parity-pass/v3"
    assert len(DECIDE["cases"]) == 29
    assert len(PASS["cases"]) == 11


@pytest.mark.parametrize("case", DECIDE["cases"], ids=lambda case: case["name"])
def test_decide_parity(case: dict[str, Any]) -> None:
    register = parse_register("\n".join(case["register_lines"]))
    (candidate,) = register.candidates()
    receipt = decide(_config(case["config"]), FakeGitHub.from_case(case), candidate)
    assert receipt.action == case["expect_action"], receipt.reasons
    assert case["expect_reason_contains"] in "; ".join(receipt.reasons)


@dataclass
class MemoryJournal:
    records: list[Receipt] = field(default_factory=list)
    fail_on: str = ""

    def append(self, receipt: Receipt) -> None:
        if receipt.action == self.fail_on:
            raise OSError("disk full")
        self.records.append(receipt)

    def attempted(self, receipt_id: str) -> bool:
        held = False
        for r in self.records:
            if r.receipt_id == receipt_id and r.action in (ATTEMPTED, NOT_SENT):
                held = r.action == ATTEMPTED
        return held


ALLOWED = next(case for case in DECIDE["cases"] if case["name"] == "allowed")


@pytest.mark.parametrize("case", PASS["cases"], ids=lambda case: case["name"])
def test_pass_parity(case: dict[str, Any]) -> None:
    github = FakeGitHub.from_case(ALLOWED)
    github.merge_answer = case["merge_answer"]
    journal = MemoryJournal(fail_on=case.get("journal_fails_on") or "")
    head = github.prs[85].head_sha
    prior = case["prior_journal_actions"] or []
    for action in prior:
        journal.records.append(
            Receipt(
                v=1,
                repo="zeroemployeeorg/zeocore",
                pr=85,
                head=head,
                base_ref="main",
                receipt_id=f"apr1-1337139177-85-{head}",
                register_line=1,
                action=action,
            )
        )
    loads = [parse_register("\n".join(lines)) for lines in case["register_loads"]]
    calls = {"n": 0}

    def load() -> Any:  # noqa: ANN401
        index = min(calls["n"], len(loads) - 1)
        if calls["n"] == 1 and case["remove_status_on_recheck"]:
            github.statuses_by_sha[head] = []
        calls["n"] += 1
        return loads[index]

    run = Pass(
        config=_config(ALLOWED["config"], live=case["live"]),
        github=github,
        journal=journal,
        load=load,
        now=lambda: datetime(1970, 1, 1, tzinfo=UTC),
    )
    if case["expect_run_error"]:
        with pytest.raises(PassError):
            run.run()
    else:
        run.run()
    actions = [r.action for r in journal.records][len(prior) :]
    assert actions == (case["expect_journal_actions"] or [])
    assert len(github.merges) == case["expect_merge_calls"]
    for merge in github.merges:
        assert merge.endswith("@" + head)  # always the approved head


REGISTER = _load("parity-register-v1.json")


@pytest.mark.parametrize("case", REGISTER["cases"], ids=lambda case: case["name"])
def test_register_parity(case: dict[str, Any]) -> None:
    from zeo_core.merger import RegisterError

    assert REGISTER["spec"] == "zeomerger-parity-register/v1"
    if case["expect_accept"]:
        register = parse_register(case["text"])
        assert len(register.events) == case.get("expect_events", 0)
    else:
        with pytest.raises(RegisterError):
            parse_register(case["text"])
