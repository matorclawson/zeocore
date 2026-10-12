"""Release on merge: every merge to main is a release with a computed version.

The workflow's shape is checked from its YAML. The version script that runs
inline in release-on-merge.yml is extracted and run against throwaway git
repositories, with real merges, tags and changelog.d fragments.
"""

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> dict[Any, Any]:
    document: dict[Any, Any] = yaml.safe_load((WORKFLOWS / name).read_text())
    return document


def test_release_announcement_waits_for_installed_package_smoke() -> None:
    """A successful upload alone must never announce a verified release."""
    release = _workflow("publish.yml")["jobs"]["github-release"]
    assert {"publish-pypi", "smoke-test"} <= set(release["needs"])
    assert "needs.smoke-test.result == 'success'" in release["if"]
    assert "needs.publish-pypi.result == 'success'" in release["if"]


def test_a_release_runs_on_every_push_to_main_and_on_nothing_else() -> None:
    document = _workflow("publish.yml")
    # PyYAML reads the bare `on:` key as the boolean True.
    assert document[True] == {"push": {"branches": ["main"]}}


def test_every_publish_waits_on_the_main_only_pypi_environment() -> None:
    jobs = _workflow("publish.yml")["jobs"]
    environments = {
        job["environment"]["name"] for job in jobs.values() if "environment" in job
    }
    assert environments == {"pypi"}
    assert jobs["publish-pypi"]["needs"] == "release"


def test_the_tag_is_made_after_the_build_and_before_any_publish() -> None:
    caller = _workflow("publish.yml")["jobs"]["release"]
    assert caller["uses"] == "./.github/workflows/release-on-merge.yml"
    jobs = _workflow("release-on-merge.yml")["jobs"]
    assert set(jobs["tag"]["needs"]) == {"plan", "build"}
    assert jobs["tag"]["permissions"] == {"contents": "write"}
    assert "permissions" not in jobs["build"]
    # The reusable half never uploads to an index.
    used = {
        step.get("uses", "").split("@")[0]
        for job in jobs.values()
        for step in job.get("steps", [])
    }
    assert "pypa/gh-action-pypi-publish" not in used


def test_no_file_a_seat_edits_names_the_version() -> None:
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert "version" not in project
    assert project["dynamic"] == ["version"]
    assert "version" not in yaml.safe_load((ROOT / "release.yaml").read_text())["pypi"]


# --------------------------------------------------------------------------
# The version script
# --------------------------------------------------------------------------


def _script() -> str:
    text = (WORKFLOWS / "release-on-merge.yml").read_text()
    found = re.search(
        r"python3 - release-notes\.md <<'PY' >> \"\$GITHUB_OUTPUT\"\n(.*?)\n\s*PY\n",
        text,
        re.DOTALL,
    )
    assert found, "release-on-merge.yml no longer runs the inline version script"
    return textwrap.dedent(found.group(1))


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        # Never inherit GIT_DIR, GIT_INDEX_FILE or GIT_WORK_TREE: a git hook
        # (pre-push runs make verify) sets them, and git would then act on
        # the real repository instead of this throwaway one.
        self.env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        self.git("init", "-q", "-b", "main")
        resolved = Path(self.git("rev-parse", "--absolute-git-dir")).resolve()
        assert resolved == (root / ".git").resolve(), f"git resolved {resolved}"
        self.write("pyproject.toml", '[project]\nrequires-python = ">=3.14"\n')
        self.commit("start")

    def git(self, *args: str) -> str:
        return subprocess.run(  # noqa: S603 - fixed git in a throwaway repository
            ["git", *args],  # noqa: S607
            cwd=self.root,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def merge(self, branch: str, **files: str) -> None:
        """Merge a PR branch that writes ``files`` into main, with a merge commit."""
        self.git("checkout", "-q", "-b", branch)
        for path, text in files.items():
            self.write(path.replace("__", "/"), text)
        self.commit(branch)
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", f"Merge {branch}", branch)

    def release(self) -> tuple[int, dict[str, str], str, str]:
        notes = self.root / "notes.md"
        result = subprocess.run(  # noqa: S603 - the extracted workflow script
            [sys.executable, "-", str(notes)],
            input=_script(),
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        outputs = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        text = notes.read_text() if notes.exists() else ""
        return result.returncode, outputs, text, result.stderr


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    made = Repo(tmp_path)
    made.git("tag", "v1.2.3")
    return made


def test_a_merge_with_no_fragment_is_a_patch(repo: Repo) -> None:
    repo.merge("fix", **{"src.py": "x = 1\n"})
    status, outputs, notes, _ = repo.release()
    assert status == 0
    assert outputs == {"version": "1.2.4", "floor": "3.14"}
    assert "patch release" in notes


@pytest.mark.parametrize(
    "fragments,version",
    [
        ({"changelog.d__a.md": "bump: patch\nFixed a thing.\n"}, "1.2.4"),
        ({"changelog.d__a.md": "bump: minor\nAdded a thing.\n"}, "1.3.0"),
        (
            {
                "changelog.d__a.md": "bump: patch\nFixed.\n",
                "changelog.d__b.md": "bump: major\nBroke an API.\n",
            },
            "2.0.0",
        ),
    ],
)
def test_the_highest_declared_level_of_the_added_fragments_wins(
    repo: Repo, fragments: dict[str, str], version: str
) -> None:
    repo.merge("feature", **fragments)
    status, outputs, notes, stderr = repo.release()
    assert status == 0, stderr
    assert outputs["version"] == version
    for text in fragments.values():
        assert text.split("\n", 1)[1].strip() in notes


def test_an_old_fragment_on_main_is_never_counted_again(repo: Repo) -> None:
    repo.merge("one", **{"changelog.d__a.md": "bump: major\nBig.\n"})
    repo.git("tag", "v2.0.0")
    repo.merge("two", **{"src.py": "x = 2\n"})
    status, outputs, _, stderr = repo.release()
    assert status == 0, stderr
    assert outputs["version"] == "2.0.1"


@pytest.mark.parametrize(
    "fragment", ["Fixed a thing.\n", "bump: huge\nFixed.\n", "bump:\n"]
)
def test_a_fragment_without_a_valid_bump_line_fails(repo: Repo, fragment: str) -> None:
    repo.merge("bad", **{"changelog.d__a.md": fragment})
    status, _, _, stderr = repo.release()
    assert status == 1
    assert "first line must be" in stderr


def test_changing_a_merged_fragment_fails(repo: Repo) -> None:
    repo.merge("one", **{"changelog.d__a.md": "bump: patch\nFixed.\n"})
    repo.git("tag", "v1.2.4")
    repo.merge("two", **{"changelog.d__a.md": "bump: major\nRewritten.\n"})
    status, _, _, stderr = repo.release()
    assert status == 1
    assert "changed or deleted" in stderr


def test_the_fragment_readme_may_change(repo: Repo) -> None:
    repo.merge("docs", **{"changelog.d__README.md": "How fragments work.\n"})
    status, outputs, _, stderr = repo.release()
    assert status == 0, stderr
    assert outputs["version"] == "1.2.4"


def test_a_commit_that_already_carries_its_tag_is_a_re_run(repo: Repo) -> None:
    repo.merge("feature", **{"changelog.d__a.md": "bump: minor\nAdded.\n"})
    repo.git("tag", "v1.3.0")
    status, outputs, _, stderr = repo.release()
    assert status == 0, stderr
    assert outputs["version"] == "1.3.0"
    assert "a re-run" in stderr


def test_a_computed_tag_already_at_another_commit_fails(repo: Repo) -> None:
    repo.git("checkout", "-q", "-b", "elsewhere")
    repo.write("other.py", "y = 1\n")
    repo.commit("elsewhere")
    repo.git("tag", "v1.2.4")
    repo.git("checkout", "-q", "main")
    repo.merge("fix", **{"src.py": "x = 1\n"})
    status, _, _, stderr = repo.release()
    assert status == 1
    assert "already exists at another commit" in stderr


def test_with_no_release_tag_to_count_from_it_fails(tmp_path: Path) -> None:
    repo = Repo(tmp_path)
    repo.merge("fix", **{"src.py": "x = 1\n"})
    status, _, _, stderr = repo.release()
    assert status == 1
    assert "no vX.Y.Z tag" in stderr
