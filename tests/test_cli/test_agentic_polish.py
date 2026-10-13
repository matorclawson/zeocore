"""Findings of the no-context agent test (SOW-31): nothing prompts, errors say why.

An agent learned zeocore from ``AGENTS.md`` and ``help --json`` alone. It hung on
``zeocore validate`` with no schema name, because stdin was read before the
command line was checked; it could not tell a bad schema name from a missing
one, or why stdin was refused.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from zeo_core.adapters.runtime_host.canonical import MAX_BYTES
from zeo_core.cli import __main__ as cli
from zeo_core.cli.__main__ import main, run
from zeo_core.cli.catalogue import catalogue
from zeo_core.cli.protocol import ends_with_action

SENTINEL = "sentinel-7f3a9c"


def _run(argv: list[str], stdin: bytes = b"") -> tuple[int, dict[str, Any]]:
    return run(argv, lambda: stdin)


# -- 1. no command waits on stdin it will refuse ----------------------------------

#: A command line that is wrong however stdin reads: it must answer at once.
BAD_COMMAND_LINES = [
    ["validate"],
    ["validate", "no-such-schema"],
    ["validate", "a", "b"],
    ["digest", "extra"],
    ["invoke", "x.y"],
    ["invoke", "x.y", "--connection", "c", "--wait-approval", "0"],
    ["invoke", "x.y", "--connection", "c", "--wait-approval", "3601"],
    ["artifact"],
    ["artifact", "get"],
    ["release"],
    ["release", "bogus"],
    ["release", "pypi"],
    ["release", "pypi", "bogus"],
]


def test_every_stdin_reading_command_checks_its_command_line_first() -> None:
    """A usage error answers without reading stdin at all."""
    for argv in BAD_COMMAND_LINES:

        def refuse_to_read() -> bytes:
            raise AssertionError("stdin was read before the command line was checked")

        status, answer = run(argv, refuse_to_read)
        assert status in (2, 20), argv  # 20: ``release`` without the github extra
        assert answer["ok"] is False, argv


@pytest.mark.parametrize("argv", BAD_COMMAND_LINES)
def test_a_bad_command_line_answers_at_once_with_an_open_stdin(
    argv: list[str],
) -> None:
    """The real process, stdin an open pipe nobody writes to or closes."""
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and module
        [sys.executable, "-m", "zeo_core.cli", *argv],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        process.wait(timeout=60)  # a hang would run into this timeout
        assert process.stdout is not None
        output = process.stdout.read()
    finally:
        if process.poll() is None:
            process.kill()
        if process.stdin is not None:
            process.stdin.close()
        process.wait()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    assert process.returncode in (2, 20), output
    assert '"ok": false' in output


def test_a_terminal_is_never_waited_on(monkeypatch: pytest.MonkeyPatch) -> None:
    read: list[int] = []

    class Terminal:
        @staticmethod
        def isatty() -> bool:
            return True

        class buffer:  # noqa: N801
            @staticmethod
            def read(size: int = -1) -> bytes:
                read.append(size)
                return b""

    monkeypatch.setattr("sys.stdin", Terminal)
    answer = _captured(monkeypatch, ["digest"])
    assert read == []
    assert answer[0] == 2
    assert answer[1]["reason"] == "empty"
    assert ends_with_action(answer[1]["message"])


def test_a_closed_stdin_is_refused_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin", None)
    status, answer = _captured(monkeypatch, ["digest"])
    assert status == 2
    assert answer["reason"] == "empty"


def _captured(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> tuple[int, dict[str, Any]]:
    """What ``main`` prints and exits with, without ending the test process."""
    printed: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "emit", printed.append)
    with pytest.raises(SystemExit) as exited:
        main(argv)
    assert isinstance(exited.value.code, int)
    return exited.value.code, printed[-1]


# -- 2. an unknown schema name is its own refusal ---------------------------------


@pytest.mark.parametrize(
    ("argv", "stdin"),
    [
        (["schema", SENTINEL], b""),
        (["validate", SENTINEL], b""),
        (["validate", SENTINEL], b"{}"),
    ],
)
def test_an_unknown_schema_name_says_so_and_never_echoes_it(
    argv: list[str], stdin: bytes
) -> None:
    status, answer = _run(argv, stdin)
    assert status == 2
    assert answer["message"] == "no such schema; run zeocore schema list"
    assert SENTINEL not in str(answer)


@pytest.mark.parametrize("argv", [["schema"], ["schema", "a", "b"], ["validate"]])
def test_a_wrong_number_of_schema_names_points_at_the_list(argv: list[str]) -> None:
    status, answer = _run(argv)
    assert status == 2
    assert "zeocore schema list" in answer["message"]
    assert ends_with_action(answer["message"])


# -- 3. stdin is refused with a reason category, never an echo --------------------

JSON_REASONS = [
    (b"", "empty"),
    (b"x" * (MAX_BYTES + 1), "too_large"),
    (b"\xff\xfe", "not_utf8"),
    (b"{", "syntax"),
    (
        b'{"' + SENTINEL.encode() + b'": 1, "' + SENTINEL.encode() + b'": 2}',
        "duplicate_key",
    ),
    (b'{"a": NaN}', "non_finite_number"),
    (b"[" * 100_000 + b"]" * 100_000, "too_deep"),
    (b'{"a": 12345678901234567890}', "outside_canonical_domain"),
]

#: Commands that read strict JSON on stdin after their command line is valid.
JSON_READERS = [
    ["digest"],
    ["validate", "connections.normalized-error"],
    ["invoke", "x.y", "--connection", "c"],
    ["artifact", "get", "--out", "/no/such/dir/x"],
    ["release", "sign"],
    ["release", "pypi", "plan"],
]


@pytest.mark.parametrize("argv", JSON_READERS)
@pytest.mark.parametrize(("stdin", "reason"), JSON_REASONS)
def test_refused_stdin_names_a_reason_and_the_fix(
    argv: list[str], stdin: bytes, reason: str
) -> None:
    status, answer = _run(argv, stdin)
    if answer.get("outcome") == "extra_missing":
        pytest.skip("release needs the github extra, which isn't installed here")
    assert status == 2
    assert answer["outcome"] == "invalid_request"
    assert answer["reason"] == reason
    message = answer["message"]
    assert f"({reason})" in message
    assert message.startswith("stdin is not valid strict JSON")
    assert ends_with_action(message)
    assert SENTINEL not in str(answer)


def test_arguments_that_are_not_an_object_say_so() -> None:
    status, answer = _run(["invoke", "x.y", "--connection", "c"], b"[1]")
    assert status == 2
    assert answer["reason"] == "not_an_object"
    assert ends_with_action(answer["message"])


# -- 4. a failed validation points at the schema ----------------------------------


def test_a_value_that_fails_its_schema_points_at_the_schema() -> None:
    status, answer = _run(["validate", "connections.normalized-error"], b'{"a": 1}')
    assert status == 20
    assert answer["outcome"] == "invalid"
    message = answer["message"]
    assert "run zeocore schema connections.normalized-error" in message
    assert "fields and types" in message
    assert ends_with_action(message)


def test_the_worked_example_in_agents_md_validates() -> None:
    status, answer = _run(
        ["validate", "connections.normalized-error"],
        b'{"code": "RATE_LIMITED", "message": "slow down"}',
    )
    assert status == 0
    assert answer["ok"] is True


# -- 5. help --json says where the contracts are ----------------------------------


def test_help_json_points_at_the_schema_list() -> None:
    notes = catalogue()["protocol"]
    assert "zeocore schema list" in notes["contracts"]
    assert "zeocore schema <name>" in notes["contracts"]
