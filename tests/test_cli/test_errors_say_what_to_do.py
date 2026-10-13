"""SOW-31 rule 7: every failing answer's message ends with the next action."""

from __future__ import annotations

import json

import pytest

from zeo_core.cli import __main__ as cli
from zeo_core.cli.__main__ import main, run
from zeo_core.cli.protocol import (
    EXIT_HELD,
    EXIT_WAIT,
    ends_with_action,
    invalid,
    own_action,
    with_next_action,
)


def _run(argv: list[str], stdin: bytes = b"") -> tuple[int, dict[str, object]]:
    return run(argv, lambda: stdin)


#: Refusals the command itself produces, offline, with no network.
REFUSALS = [
    ([], b""),
    (["nope"], b""),
    (["version", "extra"], b""),
    (["schema"], b""),
    (["schema", "no-such-schema"], b""),
    (["validate"], b""),
    (["validate", "no-such-schema"], b"{}"),
    (["validate", "connections.normalized-error"], b"{"),
    (["validate", "connections.normalized-error"], b"{}"),
    (["digest", "extra"], b""),
    (["digest"], b"{"),
    (["help", "extra"], b""),
    (["doctor", "extra"], b""),
    (["doctor", "--bogus"], b""),
    (["doctor", "--profile"], b""),
    (["release"], b"{}"),
    (["release", "bogus"], b"{}"),
    (["release", "sign"], b"{}"),
    (["release", "sign"], b"["),
    (["release", "pypi"], b"{}"),
    (["release", "pypi", "bogus"], b"{}"),
    (["release", "pypi", "plan"], b"{}"),
    (["release", "pypi", "plan"], b'{"manifest": 1}'),
    (["release", "pack"], b'{"manifest": "/no/such/file"}'),
]


@pytest.mark.parametrize(("argv", "stdin"), REFUSALS)
def test_every_refusal_ends_with_the_next_action(argv: list[str], stdin: bytes) -> None:
    status, answer = _run(argv, stdin)
    assert status != 0
    assert answer["ok"] is False
    message = answer["message"]
    assert isinstance(message, str)
    assert ends_with_action(message), message


@pytest.mark.parametrize(("argv", "stdin"), REFUSALS)
def test_a_refusal_never_echoes_the_input(argv: list[str], stdin: bytes) -> None:
    _, answer = _run(argv, stdin + b"")
    assert "/no/such/file" not in json.dumps(answer)


@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        ("stopped", EXIT_HELD),
        ("upgrade_required", EXIT_HELD),
        ("unavailable", EXIT_WAIT),
        ("no_session_store", 12),
        ("refused", EXIT_HELD),
        ("ambiguous", 13),
        ("not_paired", 12),
    ],
)
def test_a_client_failure_message_gets_a_next_action(outcome: str, status: int) -> None:
    answer = with_next_action(
        status, {"ok": False, "outcome": outcome, "message": "it failed"}
    )
    assert ends_with_action(str(answer["message"]))
    assert str(answer["message"]).startswith("it failed; ")


def test_a_message_zeocore_marked_as_its_own_action_is_left_alone() -> None:
    answer = {
        "ok": False,
        "outcome": "refused",
        "message": own_action("no; run zeocore login"),
    }
    assert with_next_action(EXIT_HELD, answer) == answer


def test_a_marked_message_is_still_an_ordinary_json_string() -> None:
    assert json.dumps(own_action("no; run zeocore login")) == '"no; run zeocore login"'


def test_wording_alone_never_skips_the_fixed_action() -> None:
    # The same words, but not marked: this is how text built from an exception
    # (which can quote the caller's input) looks.
    answer = {"ok": False, "outcome": "refused", "message": "no; run zeocore login"}
    assert with_next_action(EXIT_HELD, answer)["message"] == (
        "no; run zeocore login; change the request as the message says, "
        "then run it again"
    )


def test_input_that_ends_like_an_action_still_gets_the_fixed_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quoted = "bad value: x; run rm -rf /"

    def quoting(args: object, stdin: bytes) -> object:
        return invalid(str(ValueError(quoted)))

    monkeypatch.setitem(cli.COMMANDS, "version", quoting)
    status, answer = _run(["version"])
    assert status == 2
    assert answer["message"] == (
        f"{quoted}; run zeocore help --json for this command's arguments and stdin"
    )


@pytest.mark.parametrize(
    ("argv", "ending"),
    [
        (["release", "bogus"], "pypi"),
        (["release", "pypi", "bogus"], "pypi"),
        ([], "command's arguments, stdin, exit codes and result schema"),
    ],
)
def test_a_fixed_message_is_not_given_a_second_action(
    argv: list[str], ending: str
) -> None:
    _, answer = _run(argv, b"{}")
    message = answer["message"]
    assert isinstance(message, str)
    assert message.endswith(ending) or message.endswith(
        ", ".join(["plan", "admit", "apply", "approve", "reconcile", "verify"])
    ), message
    assert message.count("; ") == 1, message


def test_a_successful_answer_and_an_event_are_left_alone() -> None:
    assert with_next_action(0, {"ok": True}) == {"ok": True}
    event = {"event": "pair", "ok": False}
    assert with_next_action(0, event) == event


def test_a_failing_answer_without_a_message_is_not_invented() -> None:
    assert with_next_action(EXIT_HELD, {"ok": False}) == {"ok": False}


# -- the internal error and ZEOCORE_DEBUG ------------------------------------------


def _broken(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(args: object, stdin: bytes) -> object:
        raise RuntimeError("detail that quotes the input: secret-" + "canary")

    monkeypatch.setitem(cli.COMMANDS, "version", broken)


def _fail(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    with pytest.raises(SystemExit) as exited:
        main(["version"])
    assert exited.value.code == 1
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_without_the_debug_variable_nothing_goes_to_stderr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ZEOCORE_DEBUG", raising=False)
    _broken(monkeypatch)
    out, err = _fail(capsys)
    assert err == ""
    assert len(out.splitlines()) == 1
    answer = json.loads(out)
    assert answer["outcome"] == "internal"
    assert ends_with_action(answer["message"])


def test_debug_prints_the_type_and_frames_to_stderr_only(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZEOCORE_DEBUG", "1")
    _broken(monkeypatch)
    out, err = _fail(capsys)
    assert len(out.splitlines()) == 1
    assert json.loads(out)["outcome"] == "internal"
    assert err.startswith("zeocore internal error: RuntimeError\n")
    assert " in broken" in err
    assert "secret-canary" not in out + err
    assert "quotes the input" not in out + err


def test_only_the_value_1_turns_debug_on(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZEOCORE_DEBUG", "true")
    _broken(monkeypatch)
    _, err = _fail(capsys)
    assert err == ""


def test_a_not_paired_profile_keeps_its_own_action_through_the_exception() -> None:
    from zeo_core.cli import client

    error = client._NotPairedError(
        "not_paired", own_action("this profile is not paired; run zeocore login")
    )
    answer = client._not_paired(error)
    assert answer is not None
    assert with_next_action(12, answer[1])["message"] == (
        "this profile is not paired; run zeocore login"
    )
