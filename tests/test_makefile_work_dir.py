"""ZEO_WORK_DIR moves a checkout's environment and output off the repository.

The operator, 2026-10-12: repositories on one volume, runs on another. With
ZEO_WORK_DIR set, the venv, docs site, build output, coverage and caches go
under $ZEO_WORK_DIR/<checkout name>/; unset, everything stays in the
checkout. `make -n` prints the commands without running them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="needs make")


#: Under `make verify ZEO_WORK_DIR=...`, make hands that assignment to child
#: processes through MAKEFLAGS, so a test run inherits it unless removed.
_MAKE_STATE = {"ZEO_WORK_DIR", "MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES"}


def _dry_run(target: str, *assignments: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in _MAKE_STATE}
    result = subprocess.run(  # noqa: S603 - make's dry run of a fixed target
        ["make", "-n", "-C", str(ROOT), target, *assignments],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


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
    work = tmp_path / ROOT.name
    commands = _dry_run(target, f"ZEO_WORK_DIR={tmp_path}")
    assert expected.format(work=work) in commands


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
