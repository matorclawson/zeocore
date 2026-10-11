"""The PyPI adapter's stages (ZEOCORE-SOW-26), with fake GitHub and PyPI reads."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
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
    load,
    plan,
    reconcile,
    verify,
)
from zeo_core.release.pypi.reads import PyPIFile, WorkflowRun
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


def _files_at(version: str = VERSION) -> dict[str, bytes]:
    pyproject = f'[project]\nname = "zeocore"\nversion = "{version}"\n'
    return {
        "pyproject.toml": pyproject.encode(),
        "release.yaml": f"pypi:\n  version: {version}\n".encode(),
        "CHANGELOG.md": f"# Changelog\n\n## [{version}] - 2026-10-11\n".encode(),
    }


@dataclass
class FakeGitHub:
    merged: bool = True
    files: dict[str, bytes] = field(default_factory=_files_at)
    tag: str | None = None
    reviewers: frozenset[str] | None = frozenset({"rodriveracom"})
    workflow_runs: tuple[WorkflowRun, ...] = ()
    broken: bool = False

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

    def required_reviewers(self, repo: str, environment: str) -> frozenset[str] | None:
        self._check()
        return self.reviewers

    def runs(self, repo: str, workflow: str, commit: str) -> tuple[WorkflowRun, ...]:
        return self.workflow_runs


@dataclass
class FakePyPI:
    release: tuple[PyPIFile, ...] | None = None
    provenance: dict[str, tuple[Mapping[str, Any], ...] | None] = field(
        default_factory=dict
    )
    broken: bool = False

    def files(self, project: str, version: str) -> tuple[PyPIFile, ...] | None:
        if self.broken:
            raise TargetUnreadableError("PyPI could not be reached")
        return self.release

    def publishers(
        self, project: str, version: str, filename: str
    ) -> tuple[Mapping[str, Any], ...] | None:
        return self.provenance.get(filename)


WHEEL = PyPIFile(f"zeocore-{VERSION}-py3-none-any.whl", "1" * 64, False)
SDIST = PyPIFile(f"zeocore-{VERSION}.tar.gz", "2" * 64, False)


def _published(**provenance: tuple[Mapping[str, Any], ...] | None) -> FakePyPI:
    default = {WHEEL.filename: (PUBLISHER,), SDIST.filename: (PUBLISHER,)}
    return FakePyPI(release=(WHEEL, SDIST), provenance={**default, **provenance})


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
        (FakeGitHub(files=_files_at("0.12.9")), FakePyPI(), "version_mismatch"),
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


def test_admit_needs_the_operator_as_a_required_reviewer() -> None:
    assert admit(_manifest(), FakeGitHub()).reason == "admitted"


@pytest.mark.parametrize("reviewers", [None, frozenset(), frozenset({"someone-else"})])
def test_admit_refuses_a_publish_the_operator_wouldnt_approve(
    reviewers: frozenset[str] | None,
) -> None:
    outcome = admit(_manifest(), FakeGitHub(reviewers=reviewers))
    assert (outcome.status, outcome.reason) == ("refused", "promotion_unbound")


def test_admit_holds_when_github_is_unreadable() -> None:
    assert admit(_manifest(), FakeGitHub(broken=True)).reason == "target_unreadable"


# -- reconcile ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("github", "status", "reason"),
    [
        (FakeGitHub(), "held", "tag_absent"),
        (FakeGitHub(tag="b" * 40), "refused", "tag_mismatch"),
        (FakeGitHub(tag=COMMIT), "waiting", "publish_pending"),
        (
            FakeGitHub(tag=COMMIT, workflow_runs=(WorkflowRun("waiting", None),)),
            "waiting",
            "awaiting_operator_approval",
        ),
        (
            FakeGitHub(tag=COMMIT, workflow_runs=(WorkflowRun("in_progress", None),)),
            "waiting",
            "publish_running",
        ),
        (
            FakeGitHub(
                tag=COMMIT, workflow_runs=(WorkflowRun("completed", "failure"),)
            ),
            "held",
            "publish_failed",
        ),
        (
            FakeGitHub(
                tag=COMMIT, workflow_runs=(WorkflowRun("completed", "success"),)
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
        FakePyPI(release=(WHEEL,), provenance={WHEEL.filename: (PUBLISHER,)}),
        FakePyPI(release=(SDIST,), provenance={SDIST.filename: (PUBLISHER,)}),
        FakePyPI(
            release=(PyPIFile(WHEEL.filename, "1" * 64, True), SDIST),
            provenance={WHEEL.filename: (PUBLISHER,), SDIST.filename: (PUBLISHER,)},
        ),
        _published(**{WHEEL.filename: None}),
        _published(**{WHEEL.filename: ()}),
        _published(**{SDIST.filename: ({**PUBLISHER, "workflow": "other.yml"},)}),
        _published(**{SDIST.filename: ({**PUBLISHER, "repository": "fork/zeocore"},)}),
        _published(
            **{SDIST.filename: (PUBLISHER, {**PUBLISHER, "environment": "testpypi"})}
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
        ("admit", FakeGitHub(reviewers=None), FakePyPI(), 20, "promotion_unbound"),
        (
            "reconcile",
            FakeGitHub(tag=COMMIT, workflow_runs=(WorkflowRun("waiting", None),)),
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
    def __init__(self, *answers: Completed) -> None:
        self.answers = list(answers)
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> Completed:
        self.calls.append(list(argv))
        return self.answers.pop(0)


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


def test_github_reads_required_reviewers() -> None:
    gh = GitHubCli(
        Runner(
            ok(
                {
                    "protection_rules": [
                        {
                            "type": "required_reviewers",
                            "reviewers": [
                                {"reviewer": {"login": "rodriveracom"}},
                                {"reviewer": {}},
                            ],
                        }
                    ]
                }
            ),
            ok({"protection_rules": [{"type": "wait_timer"}]}),
            MISSING,
        )
    )
    assert gh.required_reviewers(REPO, "pypi") == {"rodriveracom"}
    assert gh.required_reviewers(REPO, "pypi") is None
    assert gh.required_reviewers(REPO, "absent") is None


def test_github_reads_runs_and_wraps_other_reads() -> None:
    runner = Runner(
        ok(
            {
                "workflow_runs": [
                    {"status": "completed", "conclusion": "success"},
                    {"status": 3},
                    "x",
                ]
            }
        ),
        MISSING,
        BROKEN,
        BROKEN,
        BROKEN,
    )
    gh = GitHubCli(runner)
    assert gh.runs(REPO, "publish.yml", COMMIT) == (
        WorkflowRun("completed", "success"),
    )
    assert "head_sha=" + COMMIT in runner.calls[0][2]
    assert gh.runs(REPO, "publish.yml", COMMIT) == ()
    for read in (
        lambda: gh.default_branch(REPO),
        lambda: gh.is_ancestor(REPO, COMMIT, "main"),
        lambda: gh.file_at(REPO, COMMIT, "release.yaml"),
    ):
        with pytest.raises(TargetUnreadableError):
            read()


def _http(status: int, body: object) -> httpx.Response:
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return httpx.Response(status, content=content)


def test_pypi_reads_files_and_publishers() -> None:
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
        _http(200, {"attestation_bundles": [{"publisher": PUBLISHER}]}),
        _http(404, b""),
    ]

    def get(url: str, headers: Mapping[str, str]) -> httpx.Response:
        urls.append(url)
        return answers.pop(0)

    index = PyPIHttp(get)
    assert index.files("zeocore", VERSION) == (WHEEL,)
    assert index.publishers("zeocore", VERSION, WHEEL.filename) == (PUBLISHER,)
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
        PyPIHttp(get).publishers("zeocore", VERSION, WHEEL.filename)
    assert (
        PyPIHttp(lambda u, h: _http(404, b"")).publishers("zeocore", VERSION, "x")
        is None
    )
