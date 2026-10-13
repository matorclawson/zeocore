"""``zeocore release tree-digest`` needs no ``github`` extra; every other verb does.

A consumer on a build host installed plain ``zeocore`` and got ``extra_missing``
for ``release tree-digest``, although it only walks a directory. The proof here
runs the real command in a subprocess in which the extra's module (``requests``)
cannot be imported, as on a plain install.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from zeo_core.cli import __main__ as cli
from zeo_core.cli import release, release_usage
from zeo_core.release.signing.tree import digest, listing

#: Runs ``zeocore`` as ``main`` does, with the github extra's module unimportable.
_PLAIN_INSTALL = (
    "import sys\n"
    "sys.modules['requests'] = None\n"
    "from zeo_core.cli.__main__ import main\n"
    "main(sys.argv[1:])\n"
)


def _plain(args: list[str], stdin: bytes = b"") -> tuple[int, dict[str, object], str]:
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _PLAIN_INSTALL, *args],
        input=stdin,
        capture_output=True,
        timeout=60,
        check=False,
    )
    lines = done.stdout.decode().strip().splitlines()
    return done.returncode, json.loads(lines[-1]), done.stderr.decode()


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "Demo.app"
    (root / "Contents").mkdir(parents=True)
    (root / "Contents" / "note.txt").write_text("hello\n")
    return root


def test_the_plain_install_really_cannot_import_the_extra() -> None:
    """The negative control: the blocked module is what makes ``release`` fail."""
    status, answer, _ = _plain(["release", "sign"], b"{}")
    assert status == 20
    assert answer["outcome"] == "extra_missing"


def test_tree_digest_works_on_a_plain_install(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    status, answer, stderr = _plain(
        ["release", "tree-digest"], json.dumps({"path": str(root)}).encode()
    )
    assert status == 0, (answer, stderr)
    assert answer["ok"] is True
    assert answer["outcome"] == "digested"
    assert answer["unsigned_tree_sha256"] == digest(listing(root))
    assert answer["entries"] == len(listing(root))


def test_the_other_verbs_still_need_the_extra() -> None:
    for verb in ("sign", "verify", "pack", "receive", "send", "serve"):
        status, answer, _ = _plain(["release", verb], b"{}")
        assert (status, answer["outcome"]) == (20, "extra_missing"), verb
    status, answer, _ = _plain(["release", "pypi", "plan"], b"{}")
    assert (status, answer["outcome"]) == (20, "extra_missing")


def test_a_bad_tree_digest_request_is_refused_without_the_extra(
    tmp_path: Path,
) -> None:
    status, answer, _ = _plain(["release", "tree-digest"], b'{"path": "a", "x": "b"}')
    assert status == 2
    assert answer["outcome"] == "invalid_request"
    assert "path" in str(answer["message"])
    status, answer, _ = _plain(["release", "tree-digest", "extra"], b"")
    assert status == 2
    assert "one verb" in str(answer["message"])


def test_a_missing_directory_is_refused_not_a_crash(tmp_path: Path) -> None:
    status, answer, _ = _plain(
        ["release", "tree-digest"], json.dumps({"path": str(tmp_path / "no")}).encode()
    )
    assert status != 0
    assert answer["ok"] is False


def test_tree_digest_does_not_import_the_extra(tmp_path: Path) -> None:
    """Even when ``requests`` is importable, ``tree-digest`` leaves it alone."""
    code = (
        "import sys, json\n"
        "from zeo_core.cli.__main__ import run\n"
        "request = json.dumps({'path': sys.argv[1]}).encode()\n"
        "run(['release', 'tree-digest'], lambda: request)\n"
        "heavy = [m for m in ('requests', 'zeo_core.cli.release', "
        "'zeo_core.integrations.github') if m in sys.modules]\n"
        "print(json.dumps(heavy))\n"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code, str(_bundle(tmp_path))],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr.decode()
    assert json.loads(done.stdout.decode().strip().splitlines()[-1]) == []


def test_the_usage_check_of_tree_digest_asks_for_no_extra() -> None:
    assert cli._release_arguments(["tree-digest"]) is None
    problem = cli._release_arguments(["tree-digest", "x"])
    assert problem is not None
    assert problem[0] == 2


def test_one_table_of_verbs_serves_both_modules() -> None:
    """``release.py`` and the no-extra path read the same verbs, so they can't drift."""
    assert release._FIELDS is release_usage.FIELDS
    assert release._PYPI_STAGES is release_usage.PYPI_STAGES
    assert release.argument_problem is release_usage.argument_problem
    assert release_usage.NO_EXTRA_VERB in release_usage.FIELDS


@pytest.mark.parametrize("args", [[], ["nope"], ["pypi"], ["pypi", "nope"]])
def test_the_other_usage_errors_are_unchanged(args: list[str]) -> None:
    problem = release_usage.argument_problem(args)
    assert problem is not None
    assert problem[0] == 2
