"""The PyPI adapter's stages (ZEOCORE-SOW-26), with fake GitHub and PyPI reads."""

from __future__ import annotations

import base64
import json
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from zeo_core.cli.release import make_command
from zeo_core.release.pypi import (
    GitHubCli,
    ManifestError,
    PyPIHttp,
    PyPIManifest,
    TargetUnreadableError,
    admit,
    apply,
    load,
    plan,
    reconcile,
    verify,
)
from zeo_core.release.pypi.reads import (
    EnvironmentPolicy,
    Identity,
    Provenance,
    PyPIFile,
    TagAnswerLostError,
    TagResult,
    WorkflowRun,
)
from zeo_core.release.signing import SigningJournal
from zeo_core.release.signing.toolchain import Completed

REPO = "zeroemployeeorg/zeocore"
COMMIT = "a" * 40
VERSION = "0.13.0"
PUBLISHER = {
    "kind": "GitHub",
    "repository": REPO,
    "workflow": "publish.yml",
    "environment": "pypi",
}


def _manifest(**changes: str) -> PyPIManifest:
    values = {
        "project": "zeocore",
        "version": VERSION,
        "repository": REPO,
        "commit": COMMIT,
        "workflow": "publish.yml",
        "environment": "pypi",
        **changes,
    }
    return PyPIManifest.model_validate(values)


def _section(version: str = VERSION) -> dict[str, str]:
    return {
        "project": "zeocore",
        "version": version,
        "repository": REPO,
        "commit": COMMIT,
        "workflow": "publish.yml",
        "environment": "pypi",
    }


def _committed(version: str = VERSION) -> dict[str, str]:
    """The section as committed: a file can't name its own commit."""
    section = _section(version)
    del section["commit"]
    return section


def _files_at(version: str = VERSION) -> dict[str, bytes]:
    pyproject = f'[project]\nname = "zeocore"\nversion = "{version}"\n'
    return {
        "pyproject.toml": pyproject.encode(),
        "release.yaml": yaml.safe_dump({"pypi": _committed(version)}).encode(),
        "CHANGELOG.md": f"# Changelog\n\n## [{version}] - 2026-10-11\n".encode(),
    }


GOOD_POLICY = EnvironmentPolicy(
    required_reviewers=frozenset({"rodriveracom"}),
    prevent_self_review=True,
    can_admins_bypass=False,
    custom_policies=(("v*", "tag"),),
)


@dataclass
class FakeGitHub:
    merged: bool = True
    files: dict[str, bytes] = field(default_factory=_files_at)
    tag: str | None = None
    policy: EnvironmentPolicy | None = field(default_factory=lambda: GOOD_POLICY)
    creator: Identity = field(default_factory=lambda: Identity("matorclawson", True))
    workflow_runs: tuple[WorkflowRun, ...] = ()
    broken: bool = False
    tag_result: TagResult | None = "created"  # None: the answer is lost
    created: list[str] = field(default_factory=list)

    def _check(self) -> None:
        if self.broken:
            raise TargetUnreadableError("GitHub could not be read")

    def default_branch(self, repo: str) -> str:
        self._check()
        return "main"

    def is_ancestor(self, repo: str, commit: str, branch: str) -> bool:
        return self.merged

    def file_at(self, repo: str, commit: str, path: str) -> bytes | None:
        return self.files.get(path)

    def tag_commit(self, repo: str, tag: str) -> str | None:
        self._check()
        return self.tag

    def environment(self, repo: str, environment: str) -> EnvironmentPolicy | None:
        self._check()
        return self.policy

    def identity(self, repo: str) -> Identity:
        self._check()
        return self.creator

    def create_tag(self, repo: str, tag: str, commit: str) -> TagResult:
        self.created.append(tag)
        if self.tag_result is None:
            raise TagAnswerLostError("lost")
        if self.tag_result == "created":
            self.tag = commit
        return self.tag_result

    def runs(self, repo: str, workflow: str, commit: str) -> tuple[WorkflowRun, ...]:
        return self.workflow_runs


@dataclass
class FakePyPI:
    release: tuple[PyPIFile, ...] | None = None
    attested: dict[str, tuple[Provenance, ...] | None] = field(default_factory=dict)
    broken: bool = False

    def files(self, project: str, version: str) -> tuple[PyPIFile, ...] | None:
        if self.broken:
            raise TargetUnreadableError("PyPI could not be reached")
        return self.release

    def provenance(
        self, project: str, version: str, filename: str
    ) -> tuple[Provenance, ...] | None:
        return self.attested.get(filename)


WHEEL = PyPIFile(f"zeocore-{VERSION}-py3-none-any.whl", "1" * 64, False)
SDIST = PyPIFile(f"zeocore-{VERSION}.tar.gz", "2" * 64, False)


def _attested(item: PyPIFile, publisher: Mapping[str, Any] = PUBLISHER) -> Provenance:
    return Provenance(publisher, ((item.filename, item.sha256),))


def _published(**attested: tuple[Provenance, ...] | None) -> FakePyPI:
    default = {WHEEL.filename: (_attested(WHEEL),), SDIST.filename: (_attested(SDIST),)}
    return FakePyPI(release=(WHEEL, SDIST), attested={**default, **attested})


# -- plan ---------------------------------------------------------------------------


def test_plan_creates_the_tag_when_everything_agrees() -> None:
    outcome = plan(_manifest(), FakeGitHub(), FakePyPI())
    assert (outcome.status, outcome.reason) == ("done", "planned")
    assert outcome.facts == {"tag": "v0.13.0", "commit": COMMIT, "stage": "create-tag"}


def test_plan_sees_a_tag_already_at_the_commit_as_applied() -> None:
    outcome = plan(_manifest(), FakeGitHub(tag=COMMIT), FakePyPI())
    assert outcome.facts["stage"] == "already-applied"


@pytest.mark.parametrize(
    ("github", "pypi", "reason"),
    [
        (FakeGitHub(merged=False), FakePyPI(), "commit_unreviewed"),
        (FakeGitHub(files=_files_at("0.12.9")), FakePyPI(), "manifest_unreviewed"),
        (
            FakeGitHub(
                files={
                    **_files_at(),
                    "pyproject.toml": b'[project]\nversion = "0.12.9"\n',
                }
            ),
            FakePyPI(),
            "version_mismatch",
        ),
        (
            FakeGitHub(files={**_files_at(), "release.yaml": b"pypi: [1"}),
            FakePyPI(),
            "version_mismatch",
        ),
        (
            FakeGitHub(files={**_files_at(), "pyproject.toml": b"not toml ["}),
            FakePyPI(),
            "version_mismatch",
        ),
        (
            FakeGitHub(files={**_files_at(), "CHANGELOG.md": b"# nothing\n"}),
            FakePyPI(),
            "version_mismatch",
        ),
        (FakeGitHub(files={}), FakePyPI(), "version_mismatch"),
        (FakeGitHub(), _published(), "version_published"),
        (FakeGitHub(tag="b" * 40), FakePyPI(), "tag_mismatch"),
    ],
)
def test_plan_refuses(github: FakeGitHub, pypi: FakePyPI, reason: str) -> None:
    outcome = plan(_manifest(), github, pypi)
    assert (outcome.status, outcome.reason) == ("refused", reason)


@pytest.mark.parametrize(
    ("github", "pypi"),
    [(FakeGitHub(broken=True), FakePyPI()), (FakeGitHub(), FakePyPI(broken=True))],
)
def test_plan_holds_when_a_target_is_unreadable(
    github: FakeGitHub, pypi: FakePyPI
) -> None:
    outcome = plan(_manifest(), github, pypi)
    assert (outcome.status, outcome.reason) == ("held", "target_unreadable")


# -- admit --------------------------------------------------------------------------


def test_admit_when_only_the_operator_can_approve_and_only_tags_deploy() -> None:
    outcome = admit(_manifest(), FakeGitHub())
    assert (outcome.status, outcome.reason) == ("done", "admitted")
    assert outcome.facts["creator"] == "matorclawson"


def _policy(**changes: object) -> EnvironmentPolicy:
    return replace(GOOD_POLICY, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("github", "reason"),
    [
        (FakeGitHub(policy=None), "promotion_unbound"),
        (FakeGitHub(policy=_policy(required_reviewers=None)), "promotion_unbound"),
        (
            FakeGitHub(policy=_policy(required_reviewers=frozenset())),
            "promotion_unbound",
        ),
        (
            FakeGitHub(policy=_policy(required_reviewers=frozenset({"someone"}))),
            "promotion_unbound",
        ),
        (FakeGitHub(creator=Identity("rodriveracom", True)), "creator_is_operator"),
        (
            FakeGitHub(
                policy=_policy(
                    required_reviewers=frozenset({"rodriveracom", "matorclawson"})
                )
            ),
            "promotion_unbound",
        ),
        (FakeGitHub(policy=_policy(can_admins_bypass=True)), "admin_bypass_possible"),
        (
            FakeGitHub(policy=_policy(custom_policies=None)),
            "deployment_policy_unrestricted",
        ),
        (
            FakeGitHub(policy=_policy(custom_policies=())),
            "deployment_policy_unrestricted",
        ),
        (
            FakeGitHub(
                policy=_policy(custom_policies=(("v*", "tag"), ("main", "branch")))
            ),
            "deployment_policy_unrestricted",
        ),
        (
            FakeGitHub(policy=_policy(custom_policies=(("*", "tag"),))),
            "deployment_policy_unrestricted",
        ),
    ],
)
def test_admit_refuses_unless_the_approval_is_a_control(
    github: FakeGitHub, reason: str
) -> None:
    outcome = admit(_manifest(), github)
    assert (outcome.status, outcome.reason) == ("refused", reason)


def test_admit_allows_a_non_admin_creator_while_admins_may_bypass() -> None:
    github = FakeGitHub(
        policy=_policy(can_admins_bypass=True), creator=Identity("zeocore-bot", False)
    )
    assert admit(_manifest(), github).reason == "admitted"


def test_admit_holds_when_github_is_unreadable() -> None:
    assert admit(_manifest(), FakeGitHub(broken=True)).reason == "target_unreadable"


# -- apply ----------------------------------------------------------------------------


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[SigningJournal]:
    opened = SigningJournal(tmp_path / "journal.jsonl")
    yield opened
    opened.close()


def test_apply_creates_the_tag_once(journal: SigningJournal) -> None:
    github = FakeGitHub()
    first = apply(_manifest(), github, FakePyPI(), journal)
    again = apply(_manifest(), github, FakePyPI(), journal)
    assert (first.status, first.reason) == ("done", "tag_created")
    assert (again.status, again.reason) == ("done", "already_applied")
    assert github.created == ["v0.13.0"]


def test_apply_refuses_before_any_effect(journal: SigningJournal) -> None:
    for github in (
        FakeGitHub(merged=False),
        FakeGitHub(policy=_policy(can_admins_bypass=True)),
    ):
        outcome = apply(_manifest(), github, FakePyPI(), journal)
        assert outcome.status == "refused"
        assert github.created == []


def test_a_lost_answer_is_ambiguous_and_never_retried(journal: SigningJournal) -> None:
    github = FakeGitHub(tag_result=None)
    lost = apply(_manifest(), github, FakePyPI(), journal)
    assert (lost.status, lost.reason) == ("ambiguous", "apply_unreconciled")
    github.tag_result = "created"
    again = apply(_manifest(), github, FakePyPI(), journal)
    assert (again.status, again.reason) == ("ambiguous", "apply_unreconciled")
    assert github.created == ["v0.13.0"]


def test_a_lost_answer_that_landed_is_settled_by_readback(
    journal: SigningJournal,
) -> None:
    github = FakeGitHub(tag_result=None)
    apply(_manifest(), github, FakePyPI(), journal)
    github.tag = COMMIT  # the tag did land
    outcome = apply(_manifest(), github, FakePyPI(), journal)
    assert outcome.reason == "already_applied"
    assert not journal.is_open(f"pypi:zeocore:v0.13.0:{COMMIT}")


@pytest.mark.parametrize("result", ["not_sent", "rejected"])
def test_a_definite_failure_closes_the_intent(
    journal: SigningJournal, result: TagResult
) -> None:
    github = FakeGitHub(tag_result=result)
    outcome = apply(_manifest(), github, FakePyPI(), journal)
    assert (outcome.status, outcome.reason) == ("held", "tag_not_created")
    github.tag_result = "created"
    assert apply(_manifest(), github, FakePyPI(), journal).reason == "tag_created"


def test_an_existing_ref_is_read_back(journal: SigningJournal) -> None:
    class Racing(FakeGitHub):
        def create_tag(self, repo: str, tag: str, commit: str) -> TagResult:
            self.tag = self.race
            return "exists"

        race: str | None = None

    landed = Racing()
    landed.race = COMMIT
    assert apply(_manifest(), landed, FakePyPI(), journal).reason == "tag_created"
    elsewhere = Racing()
    elsewhere.race = "b" * 40
    outcome = apply(_manifest(), elsewhere, FakePyPI(), journal)
    assert outcome.reason == "tag_not_created"


def test_an_existing_ref_that_cant_be_read_is_ambiguous(
    journal: SigningJournal,
) -> None:
    class Unreadable(FakeGitHub):
        calls = 0

        def tag_commit(self, repo: str, tag: str) -> str | None:
            self.calls += 1
            if self.calls > 1:
                raise TargetUnreadableError("down")
            return None

        def create_tag(self, repo: str, tag: str, commit: str) -> TagResult:
            return "exists"

    assert apply(_manifest(), Unreadable(), FakePyPI(), journal).status == "ambiguous"


# -- reconcile ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("github", "status", "reason"),
    [
        (FakeGitHub(), "held", "tag_absent"),
        (FakeGitHub(tag="b" * 40), "refused", "tag_mismatch"),
        (FakeGitHub(tag=COMMIT), "waiting", "publish_pending"),
        (
            FakeGitHub(
                tag=COMMIT, workflow_runs=(WorkflowRun("waiting", None, "v0.13.0"),)
            ),
            "waiting",
            "awaiting_operator_approval",
        ),
        (
            FakeGitHub(
                tag=COMMIT, workflow_runs=(WorkflowRun("in_progress", None, "v0.13.0"),)
            ),
            "waiting",
            "publish_running",
        ),
        (
            FakeGitHub(
                tag=COMMIT,
                workflow_runs=(WorkflowRun("completed", "failure", "v0.13.0"),),
            ),
            "held",
            "publish_failed",
        ),
        (
            FakeGitHub(
                tag=COMMIT,
                workflow_runs=(WorkflowRun("completed", "success", "v0.13.0"),),
            ),
            "done",
            "published",
        ),
        (FakeGitHub(broken=True), "held", "target_unreadable"),
    ],
)
def test_reconcile_reads_the_tag_then_the_run(
    github: FakeGitHub, status: str, reason: str
) -> None:
    outcome = reconcile(_manifest(), github)
    assert (outcome.status, outcome.reason) == (status, reason)


# -- verify -------------------------------------------------------------------------


def test_verify_records_the_publisher_for_every_file() -> None:
    outcome = verify(_manifest(), _published())
    assert (outcome.status, outcome.reason) == ("done", "publisher_recorded")
    assert "not verified" in outcome.message
    assert [f["filename"] for f in outcome.facts["files"]] == [
        WHEEL.filename,
        SDIST.filename,
    ]


@pytest.mark.parametrize(
    "pypi",
    [
        FakePyPI(release=(WHEEL,), attested={WHEEL.filename: (_attested(WHEEL),)}),
        FakePyPI(release=(SDIST,), attested={SDIST.filename: (_attested(SDIST),)}),
        FakePyPI(
            release=(PyPIFile(WHEEL.filename, "1" * 64, True), SDIST),
            attested={
                WHEEL.filename: (_attested(WHEEL),),
                SDIST.filename: (_attested(SDIST),),
            },
        ),
        _published(**{WHEEL.filename: None}),
        _published(**{WHEEL.filename: ()}),
        _published(
            **{SDIST.filename: (_attested(SDIST, {**PUBLISHER, "workflow": "x.yml"}),)}
        ),
        _published(
            **{
                SDIST.filename: (
                    _attested(SDIST, {**PUBLISHER, "repository": "fork/z"}),
                )
            }
        ),
        _published(
            **{
                SDIST.filename: (
                    _attested(SDIST),
                    _attested(SDIST, {**PUBLISHER, "environment": "testpypi"}),
                )
            }
        ),
        # The publisher is right, but the attestation is for other bytes.
        _published(
            **{SDIST.filename: (Provenance(PUBLISHER, ((SDIST.filename, "9" * 64),)),)}
        ),
        _published(
            **{
                SDIST.filename: (
                    Provenance(PUBLISHER, ((WHEEL.filename, WHEEL.sha256),)),
                )
            }
        ),
        _published(
            **{
                SDIST.filename: (
                    Provenance(
                        PUBLISHER,
                        (
                            (SDIST.filename, SDIST.sha256),
                            (WHEEL.filename, WHEEL.sha256),
                        ),
                    ),
                )
            }
        ),
    ],
)
def test_verify_refuses_anything_but_the_expected_publisher(pypi: FakePyPI) -> None:
    outcome = verify(_manifest(), pypi)
    assert (outcome.status, outcome.reason) == ("refused", "provenance_mismatch")


@pytest.mark.parametrize(
    ("pypi", "reason"),
    [(FakePyPI(), "version_absent"), (FakePyPI(broken=True), "target_unreadable")],
)
def test_verify_holds_without_a_release(pypi: FakePyPI, reason: str) -> None:
    outcome = verify(_manifest(), pypi)
    assert (outcome.status, outcome.reason) == ("held", reason)


# -- the manifest -------------------------------------------------------------------


def _text(**changes: object) -> str:
    section = {
        "project": "zeocore",
        "version": "0.13.0",
        "repository": REPO,
        "commit": COMMIT,
        "workflow": "publish.yml",
        "environment": "pypi",
        **changes,
    }
    return yaml.safe_dump({"pypi": section, "signing": {}})


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "release.yaml"
    path.write_text(text)
    return path


def test_the_manifest_loads(tmp_path: Path) -> None:
    text = _text()
    assert load(_write(tmp_path, text)).tag == "v0.13.0"


@pytest.mark.parametrize(
    "text",
    [
        "{not yaml",
        "[]",
        "signing: {}\n",
        "pypi:\n  project: zeocore\n",
        _text(version="v1"),
    ],
)
def test_a_bad_manifest_is_invalid(tmp_path: Path, text: str) -> None:
    with pytest.raises(ManifestError):
        load(_write(tmp_path, text))
    with pytest.raises(ManifestError, match="can't be read"):
        load(tmp_path / "absent.yaml")


# -- the command ----------------------------------------------------------------------


def _command(
    github: FakeGitHub, pypi: FakePyPI
) -> Callable[[Sequence[str], bytes], tuple[int, dict[str, Any]]]:
    return make_command(pypi_reads=lambda: (github, pypi))  # type: ignore[arg-type,return-value]


def _manifest_file(tmp_path: Path) -> bytes:
    text = _text()
    return json.dumps({"manifest": str(_write(tmp_path, text))}).encode()


@pytest.mark.parametrize(
    ("stage", "github", "pypi", "status", "outcome"),
    [
        ("plan", FakeGitHub(), FakePyPI(), 0, "planned"),
        ("admit", FakeGitHub(policy=None), FakePyPI(), 20, "promotion_unbound"),
        (
            "reconcile",
            FakeGitHub(
                tag=COMMIT, workflow_runs=(WorkflowRun("waiting", None, "v0.13.0"),)
            ),
            FakePyPI(),
            11,
            "awaiting_operator_approval",
        ),
        ("verify", FakeGitHub(), _published(), 0, "publisher_recorded"),
    ],
)
def test_the_pypi_command(
    tmp_path: Path,
    stage: str,
    github: FakeGitHub,
    pypi: FakePyPI,
    status: int,
    outcome: str,
) -> None:
    got, answer = _command(github, pypi)(["pypi", stage], _manifest_file(tmp_path))
    assert (got, answer["outcome"], answer["target"]) == (
        status,
        outcome,
        "pypi:zeocore",
    )


@pytest.mark.parametrize(
    ("args", "stdin"),
    [
        (["pypi"], b"{}"),
        (["pypi", "apply"], b"{}"),
        (["pypi", "plan"], b"not json"),
        (["pypi", "plan"], b'{"manifest": 3}'),
        (["pypi", "plan"], b'{"manifest": "/absent.yaml"}'),
    ],
)
def test_the_pypi_command_refuses_a_bad_request(args: list[str], stdin: bytes) -> None:
    status, answer = _command(FakeGitHub(), FakePyPI())(args, stdin)
    assert (status, answer["outcome"]) == (2, "invalid_request")


# -- the real readers, with fake transports -------------------------------------


class Runner:
    def __init__(self, *answers: Completed | BaseException) -> None:
        self.answers = list(answers)
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> Completed:
        self.calls.append(list(argv))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def ok(body: object) -> Completed:
    return Completed(0, json.dumps(body).encode(), b"")


MISSING = Completed(1, b"", b"gh: Not Found (HTTP 404)")
BROKEN = Completed(1, b"", b"HTTP 502")


def test_github_reads_a_lightweight_and_an_annotated_tag() -> None:
    gh = GitHubCli(
        Runner(
            ok({"object": {"type": "commit", "sha": COMMIT}}),
            ok({"object": {"type": "tag", "sha": "t" * 40}}),
            ok({"object": {"type": "commit", "sha": "c" * 40}}),
            MISSING,
        )
    )
    assert gh.tag_commit(REPO, "v1") == COMMIT
    assert gh.tag_commit(REPO, "v2") == "c" * 40
    assert gh.tag_commit(REPO, "v3") is None


@pytest.mark.parametrize(
    "answers",
    [
        (ok({"object": "nope"}),),
        (ok({"object": {"type": "tree", "sha": "x"}}),),
        (ok({"object": {"type": "tag", "sha": "t"}}), MISSING),
        tuple(ok({"object": {"type": "tag", "sha": "t"}}) for _ in range(5)),
        (BROKEN,),
    ],
)
def test_github_unreadable_tags(answers: tuple[Completed, ...]) -> None:
    with pytest.raises(TargetUnreadableError):
        GitHubCli(Runner(*answers)).tag_commit(REPO, "v1")


def test_github_reads_an_environment_policy() -> None:
    runner = Runner(
        ok(
            {
                "can_admins_bypass": False,
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                },
                "protection_rules": [
                    {
                        "type": "required_reviewers",
                        "prevent_self_review": True,
                        "reviewers": [
                            {"reviewer": {"login": "rodriveracom"}},
                        ],
                    }
                ],
            }
        ),
        ok({"branch_policies": [{"name": "v*", "type": "tag"}, "x"]}),
        ok({"protection_rules": [{"type": "wait_timer"}]}),
        MISSING,
    )
    gh = GitHubCli(runner)
    assert gh.environment(REPO, "pypi") == GOOD_POLICY
    assert runner.calls[1][2].endswith("environments/pypi/deployment-branch-policies")
    assert gh.environment(REPO, "pypi") == EnvironmentPolicy(None, False, True, None)
    assert gh.environment(REPO, "absent") is None


def test_github_reads_the_acting_identity() -> None:
    gh = GitHubCli(
        Runner(
            ok({"login": "matorclawson"}),
            ok({"permissions": {"admin": True}}),
            ok({"login": 3}),
            ok({}),
        )
    )
    assert gh.identity(REPO) == Identity("matorclawson", True)
    with pytest.raises(TargetUnreadableError):
        gh.identity(REPO)


@pytest.mark.parametrize(
    ("answer", "result"),
    [
        (ok({"ref": "refs/tags/v0.13.0"}), "created"),
        (Completed(1, b"", b"gh: Reference already exists (HTTP 422)"), "exists"),
        (Completed(1, b"", b"gh: Resource not accessible (HTTP 403)"), "rejected"),
        (OSError("no gh"), "not_sent"),
    ],
)
def test_github_creates_a_lightweight_tag(
    answer: Completed | OSError, result: str
) -> None:
    runner = Runner(answer)
    assert GitHubCli(runner).create_tag(REPO, "v0.13.0", COMMIT) == result
    assert runner.calls[0][2:] == [
        "-X",
        "POST",
        f"repos/{REPO}/git/refs",
        "-f",
        "ref=refs/tags/v0.13.0",
        "-f",
        f"sha={COMMIT}",
    ]


@pytest.mark.parametrize(
    "answer",
    [
        Completed(1, b"", b"HTTP 502"),
        Completed(1, b"", b"connection reset"),
        subprocess.TimeoutExpired("gh", 1),
    ],
)
def test_github_tag_answer_lost(answer: Completed | Exception) -> None:
    with pytest.raises(TagAnswerLostError):
        GitHubCli(Runner(answer)).create_tag(REPO, "v0.13.0", COMMIT)


def test_github_reads_runs_and_wraps_other_reads() -> None:
    run = {"status": "completed", "conclusion": "success", "head_branch": "v0.13.0"}
    runner = Runner(
        ok({"workflow_runs": [run]}),
        ok({"workflow_runs": [{"status": "completed"}]}),
        BROKEN,
        BROKEN,
        BROKEN,
    )
    gh = GitHubCli(runner)
    assert gh.runs(REPO, "publish.yml", COMMIT) == (
        WorkflowRun("completed", "success", "v0.13.0"),
    )
    assert "head_sha=" + COMMIT in runner.calls[0][2]
    assert gh.runs(REPO, "publish.yml", COMMIT) == (
        WorkflowRun("completed", None, None),
    )
    for read in (
        lambda: gh.default_branch(REPO),
        lambda: gh.is_ancestor(REPO, COMMIT, "main"),
        lambda: gh.file_at(REPO, COMMIT, "release.yaml"),
    ):
        with pytest.raises(TargetUnreadableError):
            read()


@pytest.mark.parametrize(
    "answer",
    [
        MISSING,  # no such workflow: never "waiting" forever
        ok({"workflow_runs": [{"status": 3}]}),
        ok({"workflow_runs": ["x"]}),
    ],
)
def test_github_unreadable_runs(answer: Completed) -> None:
    with pytest.raises(TargetUnreadableError):
        GitHubCli(Runner(answer)).runs(REPO, "publish.yml", COMMIT)


@pytest.mark.parametrize("entry", [{"reviewer": {}}, {"reviewer": "x"}, "x"])
def test_an_unreadable_reviewer_is_never_assumed(entry: object) -> None:
    answer = ok(
        {"protection_rules": [{"type": "required_reviewers", "reviewers": [entry]}]}
    )
    with pytest.raises(TargetUnreadableError):
        GitHubCli(Runner(answer)).environment(REPO, "pypi")


def test_a_non_dict_publisher_is_unreadable() -> None:
    bundle = _bundle(PUBLISHER, WHEEL)
    bundle["attestation_bundles"][0]["publisher"] = "GitHub"
    with pytest.raises(TargetUnreadableError):
        PyPIHttp(lambda u, h: _http(200, bundle)).provenance("zeocore", VERSION, "x")


def test_reconcile_ignores_runs_for_other_refs() -> None:
    github = FakeGitHub(
        tag=COMMIT, workflow_runs=(WorkflowRun("completed", "success", "main"),)
    )
    outcome = reconcile(_manifest(), github)
    assert (outcome.status, outcome.reason) == ("waiting", "publish_pending")


@pytest.mark.parametrize(
    "changes",
    [
        {"repository": "fork/zeocore"},
        {"workflow": "other.yml"},
        {"environment": "testpypi"},
        {"project": "other"},
    ],
)
def test_the_target_is_pinned(changes: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="reviewed target"):
        _manifest(**changes)


def _release_at(section: dict[str, str]) -> FakeGitHub:
    content = yaml.safe_dump({"pypi": section}).encode()
    return FakeGitHub(files={**_files_at(), "release.yaml": content})


def test_a_local_section_unlike_the_committed_one_is_unreviewed() -> None:
    # The committed file names another version; locally it says 0.13.0.
    outcome = plan(_manifest(), _release_at(_committed("0.13.5")), FakePyPI())
    assert (outcome.status, outcome.reason) == ("refused", "manifest_unreviewed")


def test_a_committed_section_naming_a_commit_is_not_accepted() -> None:
    outcome = plan(_manifest(), _release_at(_section()), FakePyPI())
    assert (outcome.status, outcome.reason) == ("refused", "version_mismatch")


def _http(status: int, body: object) -> httpx.Response:
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return httpx.Response(status, content=content)


def _bundle(publisher: Mapping[str, Any], item: PyPIFile) -> dict[str, Any]:
    statement = {
        "subject": [{"name": item.filename, "digest": {"sha256": item.sha256}}]
    }
    encoded = base64.b64encode(json.dumps(statement).encode()).decode()
    return {
        "attestation_bundles": [
            {
                "publisher": publisher,
                "attestations": [{"envelope": {"statement": encoded}}],
            }
        ]
    }


def test_pypi_reads_files_and_provenance() -> None:
    urls: list[str] = []
    answers = [
        _http(
            200,
            {
                "urls": [
                    {
                        "filename": WHEEL.filename,
                        "digests": {"sha256": "1" * 64},
                        "yanked": False,
                    }
                ]
            },
        ),
        _http(200, _bundle(PUBLISHER, WHEEL)),
        _http(404, b""),
    ]

    def get(url: str, headers: Mapping[str, str]) -> httpx.Response:
        urls.append(url)
        return answers.pop(0)

    index = PyPIHttp(get)
    assert index.files("zeocore", VERSION) == (WHEEL,)
    assert index.provenance("zeocore", VERSION, WHEEL.filename) == (_attested(WHEEL),)
    assert index.files("zeocore", "9.9.9") is None
    assert urls[0] == f"https://pypi.org/pypi/zeocore/{VERSION}/json"
    assert urls[1].startswith("https://pypi.org/integrity/zeocore/")


@pytest.mark.parametrize(
    "response",
    [
        _http(500, b""),
        _http(200, b"not json"),
        _http(200, {"urls": [{"filename": "x"}]}),
        httpx.ConnectError("down"),
    ],
)
def test_pypi_unreadable_answers(response: httpx.Response | Exception) -> None:
    def get(url: str, headers: Mapping[str, str]) -> httpx.Response:
        if isinstance(response, Exception):
            raise response
        return response

    with pytest.raises(TargetUnreadableError):
        PyPIHttp(get).files("zeocore", VERSION)


def test_pypi_unreadable_provenance() -> None:
    def get(url: str, headers: Mapping[str, str]) -> httpx.Response:
        return _http(200, {"attestation_bundles": [{}]})

    with pytest.raises(TargetUnreadableError):
        PyPIHttp(get).provenance("zeocore", VERSION, WHEEL.filename)
    assert (
        PyPIHttp(lambda u, h: _http(404, b"")).provenance("zeocore", VERSION, "x")
        is None
    )
    bad_statement = {
        "attestation_bundles": [
            {
                "publisher": PUBLISHER,
                "attestations": [{"envelope": {"statement": "!!"}}],
            }
        ]
    }
    with pytest.raises(TargetUnreadableError):
        PyPIHttp(lambda u, h: _http(200, bad_statement)).provenance(
            "zeocore", VERSION, "x"
        )
    with pytest.raises(TargetUnreadableError):
        PyPIHttp(lambda u, h: _http(200, [1])).files("zeocore", VERSION)
