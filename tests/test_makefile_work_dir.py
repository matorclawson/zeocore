"""ZEO_WORK_DIR moves a checkout's environment and output off the repository.

The operator, 2026-10-12: repositories on one volume, runs on another. With
ZEO_WORK_DIR set, the venv, docs site, build output, coverage and caches go
under $ZEO_WORK_DIR/<checkout name>/; unset, everything stays in the
checkout. `make -n` prints the commands without running them.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="needs make")


#: Under `make verify ZEO_WORK_DIR=...`, make hands that assignment to child
#: processes through MAKEFLAGS, so a test run inherits it unless removed.
_MAKE_STATE = {"ZEO_WORK_DIR", "MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES"}


def _make(target: str, *assignments: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in _MAKE_STATE}
    return subprocess.run(  # noqa: S603 - make's dry run of a fixed target
        ["make", "-n", "-C", str(ROOT), target, *assignments],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _dry_run(target: str, *assignments: str) -> str:
    result = _make(target, *assignments)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _work(tmp_path: Path) -> Path:
    """The checkout's directory under ZEO_WORK_DIR: its name and a hash of its path."""
    commands = _dry_run("build", f"ZEO_WORK_DIR={tmp_path}")
    match = re.search(
        rf"{re.escape(str(tmp_path))}/({re.escape(ROOT.name)}-\d+)/", commands
    )
    assert match, commands
    return tmp_path / match.group(1)


@pytest.mark.parametrize(
    "target,expected",
    [
        ("build", "-m build --outdir {work}/dist"),
        ("docs-build", "mkdocs build --strict --site-dir {work}/site"),
        ("format-check", "{work}/venv/bin/python -m ruff format --check"),
        ("test-fast", "-o cache_dir={work}/cache/pytest"),
    ],
)
def test_zeo_work_dir_moves_output_off_the_checkout(
    tmp_path: Path, target: str, expected: str
) -> None:
    work = _work(tmp_path)
    commands = _dry_run(target, f"ZEO_WORK_DIR={tmp_path}")
    assert expected.format(work=work) in commands


def test_checkouts_with_one_name_get_their_own_environment(tmp_path: Path) -> None:
    # Same directory name, different paths: the work directories must differ.
    first = _work(tmp_path)
    other = tmp_path / "other" / ROOT.name
    other.mkdir(parents=True)
    for name in ("Makefile", "hygiene-grepbans.mk"):
        shutil.copy(ROOT / name, other / name)
    env = {k: v for k, v in os.environ.items() if k not in _MAKE_STATE}
    command = [
        "make",
        "-n",
        "-C",
        str(other),
        "build",
        f"ZEO_WORK_DIR={tmp_path}",
    ]
    result = subprocess.run(  # noqa: S603 - make's dry run in a same-named directory
        command, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert str(first) not in result.stdout
    assert f"{tmp_path}/{ROOT.name}-" in result.stdout


@pytest.mark.parametrize(
    "value",
    ["relative/path", "a b", str(ROOT), str(ROOT / "work"), str(ROOT / "..") + "/"],
)
def test_a_work_dir_that_would_land_in_the_checkout_is_refused(value: str) -> None:
    result = _make("build", f"ZEO_WORK_DIR={value}")
    if value.endswith("/"):
        # the checkout's parent is outside it: fine
        assert result.returncode == 0
        return
    assert result.returncode != 0
    assert "ZEO_WORK_DIR" in result.stderr


@pytest.mark.parametrize(
    "target,expected",
    [
        ("build", "-m build --outdir {root}/dist"),
        ("docs-build", "--site-dir {root}/site"),
        ("format-check", "{root}/.venv/bin/python -m ruff format --check"),
    ],
)
def test_unset_everything_stays_in_the_checkout(target: str, expected: str) -> None:
    commands = _dry_run(target)
    assert expected.format(root=ROOT) in commands
    assert "cache_dir=" not in commands
