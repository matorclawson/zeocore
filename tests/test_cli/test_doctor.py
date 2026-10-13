"""``zeocore doctor``: one local, read-only check per line, each naming its fix."""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import sys
import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.test_integrations.hosted.test_pairing import (
    CANARY_ACCESS,
    CANARY_REFRESH,
    NOW,
    session,
)
from zeo_core.cli import client, doctor
from zeo_core.cli.__main__ import main, run
from zeo_core.cli.catalogue import ENTRIES
from zeo_core.connections.adapters.subprocess_runner import RealSubprocessRunner
from zeo_core.integrations.hosted import InMemorySecureSessionStore, SecureStoreError
from zeo_core.integrations.hosted.file_store import SESSION_FILE_ENV

_original_extras = doctor._extras


class Transport:
    closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch) -> InMemorySecureSessionStore:
    """A ready machine: a paired store, a good origin, the github extra present."""
    store = InMemorySecureSessionStore()
    store.save(session(NOW))
    monkeypatch.setattr(client, "make_store", lambda profile, **_: store)
    monkeypatch.setattr(client, "make_transport", lambda store: Transport())
    monkeypatch.setattr(client, "now", lambda: NOW)
    monkeypatch.setattr(
        doctor, "_extras", lambda task=None: _original_extras(lambda name: None, task)
    )
    for name in (
        client.PROFILE_ENV,
        SESSION_FILE_ENV,
        "ZEOCONNECT_URL",
        "ZEOCONNECT_DEVELOPMENT",
    ):
        monkeypatch.delenv(name, raising=False)
    return store


def _doctor(
    *args: str, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    status, answer = doctor.doctor(list(args), b"", out=seen.append)
    assert all(line["event"] == "check" for line in seen)
    assert [line["check"] for line in seen] == [c["check"] for c in answer["checks"]]
    assert capsys.readouterr().out == ""
    return status, answer


def _by_name(answer: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {check["check"]: check for check in answer["checks"]}


def test_a_ready_machine_passes_every_check(
    machine: InMemorySecureSessionStore, capsys: pytest.CaptureFixture[str]
) -> None:
    status, answer = _doctor(capsys=capsys)
    assert status == 0
    assert answer["ok"] is True
    assert answer["outcome"] == "ready"
    assert answer["failing"] == []
    assert list(_by_name(answer)) == [
        "python",
        "zeoconnect_url",
        "session_store",
        "pairing",
        "extra:github",
    ]
    assert _by_name(answer)["zeoconnect_url"]["origin"] == "production"
    assert all("next" not in check for check in answer["checks"])


def test_an_unpaired_profile_names_login_and_the_person_who_approves(
    machine: InMemorySecureSessionStore, capsys: pytest.CaptureFixture[str]
) -> None:
    machine.delete()
    status, answer = _doctor(capsys=capsys)
    assert (status, answer["outcome"], answer["failing"]) == (
        20,
        "not_ready",
        ["pairing"],
    )
    pairing = _by_name(answer)["pairing"]
    assert pairing["next"] == "zeocore login"
    assert "approves" in pairing["who"]


def test_an_expired_pairing_names_login(
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(client, "now", lambda: NOW + timedelta(days=3650))
    status, answer = _doctor(capsys=capsys)
    assert (status, answer["failing"]) == (20, ["pairing"])
    assert _by_name(answer)["pairing"]["next"] == "zeocore login"
    assert "expired" in _by_name(answer)["pairing"]["message"]


@pytest.mark.parametrize("session_file", [None, "/private/not-echoed/session.json"])
def test_an_unusable_session_store_fails_the_store_and_the_pairing(
    session_file: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    machine: InMemorySecureSessionStore,
) -> None:
    def broken(profile: str | None, **_: object) -> InMemorySecureSessionStore:
        raise SecureStoreError("secure session storage is unavailable")

    monkeypatch.setattr(client, "make_store", broken)
    if session_file:
        monkeypatch.setenv(SESSION_FILE_ENV, session_file)
    status, answer = _doctor(capsys=capsys)
    assert (status, answer["failing"]) == (20, ["session_store", "pairing"])
    checks = _by_name(answer)
    assert checks["session_store"]["store"] == ("file" if session_file else "keychain")
    assert checks["session_store"]["next"]
    assert "not checked" in checks["pairing"]["message"]
    # What names a path or the environment is never echoed.
    assert "not-echoed" not in str(answer)


def test_a_bad_origin_fails_and_names_the_two_ways_out(
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refused(store: object) -> Transport:
        raise ValueError("origin")

    monkeypatch.setattr(client, "make_transport", refused)
    monkeypatch.setattr(client, "origin", lambda: "https://evil.example/secret")
    status, answer = _doctor(capsys=capsys)
    assert (status, answer["failing"]) == (20, ["zeoconnect_url", "pairing"])
    check = _by_name(answer)["zeoconnect_url"]
    assert check["origin"] == "custom"
    assert "ZEOCONNECT_DEVELOPMENT=1" in check["next"]
    assert "evil.example" not in str(answer)


def _no_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def load(name: str) -> None:
        raise ModuleNotFoundError("no requests", name="requests")

    monkeypatch.setattr(
        doctor, "_extras", lambda task=None: _original_extras(load, task)
    )


def test_a_missing_extra_is_optional_unless_the_job_is_release(
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_requests(monkeypatch)
    status, answer = _doctor(capsys=capsys)
    assert (status, answer["ok"], answer["outcome"], answer["failing"]) == (
        0,
        True,
        "ready",
        [],
    )
    assert "for" not in answer
    check = _by_name(answer)["extra:github"]
    assert (check["ok"], check["optional"], check["needed_for"]) == (
        True,
        True,
        ["release"],
    )
    assert check["next"] == "pip install 'zeocore[github]'"
    assert "optional" in check["message"]


def test_a_missing_extra_fails_under_for_release_and_names_the_install(
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_requests(monkeypatch)
    status, answer = _doctor("--for", "release", capsys=capsys)
    assert (status, answer["outcome"], answer["failing"]) == (
        20,
        "not_ready",
        ["extra:github"],
    )
    assert answer["for"] == "release"
    check = _by_name(answer)["extra:github"]
    assert (check["ok"], check["optional"], check["needed_for"]) == (
        False,
        False,
        ["release"],
    )
    assert check["next"] == "pip install 'zeocore[github]'"


def test_an_import_error_inside_zeocore_is_not_an_extra_problem() -> None:
    def load(name: str) -> None:
        raise ModuleNotFoundError("zeocore bug", name="zeo_core.cli.nowhere")

    with pytest.raises(ModuleNotFoundError):
        doctor._extras(load)


def test_python_below_the_floor_fails_and_names_the_install(
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "version_info", (3, 13, 0, "final", 0))
    status, answer = _doctor(capsys=capsys)
    assert (status, answer["failing"]) == (20, ["python"])
    assert _by_name(answer)["python"]["next"] == "uv tool install --python 3.14 zeocore"


def test_the_python_floor_is_the_packages(machine: object) -> None:
    pyproject = tomllib.loads(
        (Path(__file__).parents[2] / "pyproject.toml").read_text("utf-8")
    )
    assert pyproject["project"]["requires-python"] == (
        ">=" + ".".join(str(part) for part in doctor.PYTHON_FLOOR)
    )


def test_every_failing_check_names_a_next_command(
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    machine.delete()
    monkeypatch.setattr(sys, "version_info", (3, 13, 0, "final", 0))
    monkeypatch.setattr(
        client, "make_transport", lambda store: (_ for _ in ()).throw(ValueError())
    )
    status, answer = _doctor(capsys=capsys)
    assert status == 20
    failing = [check for check in answer["checks"] if not check["ok"]]
    assert len(failing) >= 3
    assert all(check["next"] for check in failing)


def test_the_profile_is_chosen_by_the_flag_then_the_environment(
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[str | None] = []

    def make_store(profile: str | None, **_: object) -> InMemorySecureSessionStore:
        seen.append(profile)
        return machine

    monkeypatch.setattr(client, "make_store", make_store)
    monkeypatch.setenv(client.PROFILE_ENV, "studio")
    _doctor(capsys=capsys)
    _doctor("--profile", "other", capsys=capsys)
    # Each run asks twice: once up front to refuse a bad profile, once for the check.
    assert seen == ["studio", "studio", "other", "other"]


def test_a_bad_profile_or_flag_is_invalid_input_not_a_diagnosis(
    machine: InMemorySecureSessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(profile: str | None, **_: object) -> InMemorySecureSessionStore:
        raise SecureStoreError("session profile name is invalid")

    monkeypatch.setattr(client, "make_store", refuse)
    assert (
        doctor.doctor(["--profile", "Not A Name"], b"", out=lambda line: None)[0] == 2
    )
    assert doctor.doctor(["--nope"], b"", out=lambda line: None)[0] == 2


def test_no_token_or_environment_value_is_ever_printed(
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ZEOCONNECT_URL", "https://broker.example/secret-path")
    _, answer = _doctor(capsys=capsys)
    text = str(answer)
    for secret in (CANARY_ACCESS, CANARY_REFRESH, "secret-path"):
        assert secret not in text


def test_it_runs_through_the_dispatcher_and_names_its_schema(
    machine: InMemorySecureSessionStore, capsys: pytest.CaptureFixture[str]
) -> None:
    status, answer = run(["doctor"], lambda: b"")
    assert status == 0
    assert answer["schema"] == "zeocore.doctor/v1"
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {event["check"] for event in events} >= {"python", "pairing"}
    assert all(event["event"] == "check" for event in events)


def test_the_catalogue_arguments_match_the_parser() -> None:
    entry = next(e for e in ENTRIES if e.name == "doctor")
    tree = ast.parse(inspect.getsource(doctor.doctor))
    declared = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        and isinstance(node.args[0], ast.Constant)
    }
    assert {a.name for a in entry.arguments} == declared
    assert entry.stdin is None


def test_a_bad_origin_checks_first_and_pairing_names_the_url_not_a_person(
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refused(store: object) -> Transport:
        raise ValueError("origin")

    monkeypatch.setattr(client, "make_transport", refused)
    machine.delete()
    seen: list[dict[str, Any]] = []
    status, answer = doctor.doctor([], b"", out=seen.append)
    assert status == 20
    assert [line["check"] for line in seen][:2] == ["python", "zeoconnect_url"]
    pairing = _by_name(answer)["pairing"]
    assert "not checked" in pairing["message"]
    assert "ZEOCONNECT_URL" in pairing["next"]
    # Login would fail on the same origin, so no person is named yet.
    assert "who" not in pairing
    assert capsys.readouterr().out == ""


def test_a_keychain_that_does_not_answer_fails_the_store_and_says_so(
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runners: list[object] = []
    monkeypatch.setattr(sys, "platform", "darwin")

    def slow(
        profile: str | None, *, runner: object = None
    ) -> InMemorySecureSessionStore:
        runners.append(runner)
        raise subprocess.TimeoutExpired(["security", "private-argv"], 5)

    monkeypatch.setattr(client, "make_store", slow)
    status, answer = _doctor(capsys=capsys)
    assert (status, answer["failing"]) == (20, ["session_store", "pairing"])
    store = _by_name(answer)["session_store"]
    assert "did not answer" in store["message"]
    assert "private-argv" not in str(answer)
    # The runner it was given is bounded.
    assert isinstance(runners[0], RealSubprocessRunner)
    assert runners[0]._timeout == doctor.KEYCHAIN_TIMEOUT


def test_the_runner_passes_its_timeout_to_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    RealSubprocessRunner(timeout=2.5).run(["true"])
    assert seen["timeout"] == 2.5
    RealSubprocessRunner().run(["true"])
    assert seen["timeout"] is None


@pytest.mark.parametrize(
    "error", [ValueError("ZEO_INTEGRATION_X=secret"), OSError("/p")]
)
def test_a_store_that_raises_unexpectedly_is_a_fixed_failure_not_an_internal_error(
    error: Exception,
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(profile: str | None, **_: object) -> InMemorySecureSessionStore:
        raise error

    monkeypatch.setattr(client, "make_store", boom)
    status, answer = _doctor(capsys=capsys)
    assert (status, answer["failing"]) == (20, ["session_store", "pairing"])
    assert "secret" not in str(answer)
    assert "/p" not in str(answer)


@pytest.mark.parametrize(
    ("reason", "step"),
    [
        ("the session file was paired with another Broker", "ZEOCONNECT_URL"),
        ("the session file is invalid", "delete"),
        ("the session file must allow no group or other access", "chmod 600"),
        ("the session file's directory is missing", "mode 0700"),
        ("the session file must be an absolute path", "absolute path"),
    ],
)
def test_each_session_file_refusal_gets_its_own_advice(
    reason: str,
    step: str,
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(profile: str | None, **_: object) -> InMemorySecureSessionStore:
        raise SecureStoreError(reason)

    monkeypatch.setattr(client, "make_store", refuse)
    monkeypatch.setenv(SESSION_FILE_ENV, "/private/not-echoed/session.json")
    _, answer = _doctor(capsys=capsys)
    store = _by_name(answer)["session_store"]
    assert step in store["next"]
    assert reason in store["message"]
    assert "not-echoed" not in str(answer)


def test_only_the_extras_own_packages_are_called_a_missing_extra(
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def load(name: str) -> None:
        raise ModuleNotFoundError("no httpx", name="httpx")

    monkeypatch.setattr(
        doctor, "_extras", lambda task=None: _original_extras(load, task)
    )
    _, answer = _doctor(capsys=capsys)
    check = _by_name(answer)["extra:github"]
    assert check["next"] == "uv tool install --reinstall zeocore"
    assert "github" not in check["next"]


def test_a_real_import_failure_names_the_extra_when_its_package_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "fake_release_module.py").write_text("import requests\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(sys.modules, "requests", None)
    monkeypatch.setattr(
        doctor,
        "EXTRAS",
        (("release", "fake_release_module", "github", ("requests",)),),
    )
    check = _original_extras(task="release")[0]
    assert check["ok"] is False
    assert check["next"] == "pip install 'zeocore[github]'"
    optional = _original_extras()[0]
    assert (optional["ok"], optional["optional"]) == (True, True)


def test_the_real_store_and_dispatcher_echo_no_path_or_profile_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in (client.PROFILE_ENV, "ZEOCONNECT_URL", "ZEOCONNECT_DEVELOPMENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(SESSION_FILE_ENV, str(tmp_path / "path-sentinel" / "s.json"))
    status, answer = run(["doctor"], lambda: b"")
    assert (status, answer["outcome"]) == (20, "not_ready")
    store = _by_name(answer)["session_store"]
    assert store["store"] == "file"
    assert "directory is missing" in store["message"]
    monkeypatch.setenv(client.PROFILE_ENV, "profile-sentinel")
    status, refused = run(["doctor"], lambda: b"")
    assert status == 2
    out = capsys.readouterr().out
    for text in (out, str(answer), str(refused)):
        assert "path-sentinel" not in text
        assert "profile-sentinel" not in text


def test_a_bad_profile_is_refused_before_any_check_prints(
    machine: InMemorySecureSessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(profile: str | None, **_: object) -> InMemorySecureSessionStore:
        raise SecureStoreError("session profile name is invalid")

    monkeypatch.setattr(client, "make_store", refuse)
    seen: list[dict[str, Any]] = []
    status, answer = doctor.doctor(["--profile", "Not A Name"], b"", out=seen.append)
    assert status == 2
    assert seen == []
    assert "checks" not in answer


def test_a_profile_with_a_session_file_is_refused_before_any_check_prints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in (client.PROFILE_ENV, "ZEOCONNECT_URL", "ZEOCONNECT_DEVELOPMENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(SESSION_FILE_ENV, str(tmp_path / "s.json"))
    seen: list[dict[str, Any]] = []
    status, _ = doctor.doctor(["--profile", "studio"], b"", out=seen.append)
    assert (status, seen) == (2, [])
    monkeypatch.setenv(client.PROFILE_ENV, "studio")
    seen.clear()
    assert (doctor.doctor([], b"", out=seen.append)[0], seen) == (2, [])


@pytest.mark.parametrize(("platform", "bounded"), [("darwin", True), ("linux", False)])
def test_the_keychain_runner_is_only_passed_on_a_mac(
    platform: str,
    bounded: bool,  # noqa: FBT001
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runners: list[object] = []

    def make_store(
        profile: str | None, *, runner: object = None
    ) -> InMemorySecureSessionStore:
        runners.append(runner)
        return machine

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(client, "make_store", make_store)
    _doctor(capsys=capsys)
    assert runners
    for runner in runners:
        assert isinstance(runner, RealSubprocessRunner) is bounded
        assert (runner is None) is (not bounded)


def test_an_invalid_keychain_entry_says_logout_not_unlock(
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(profile: str | None, **_: object) -> InMemorySecureSessionStore:
        raise SecureStoreError("secure session storage is invalid")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(client, "make_store", refuse)
    _, answer = _doctor(capsys=capsys)
    step = _by_name(answer)["session_store"]["next"]
    assert "zeocore logout" in step
    assert "unlock" not in step


def test_a_locked_or_missing_keychain_says_unlock(
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(profile: str | None, **_: object) -> InMemorySecureSessionStore:
        raise SecureStoreError("secure session storage is unavailable")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(client, "make_store", refuse)
    _, answer = _doctor(capsys=capsys)
    assert "unlock the login Keychain" in _by_name(answer)["session_store"]["next"]


def test_a_plain_import_error_is_an_internal_error_with_no_detail(
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def load(name: str) -> None:
        raise ImportError("cannot import name secret-detail")

    monkeypatch.setattr(
        doctor, "_extras", lambda task=None: _original_extras(load, task)
    )
    with pytest.raises(SystemExit) as stopped:
        main(["doctor"])
    assert stopped.value.code == 1
    out = capsys.readouterr().out
    last = json.loads(out.splitlines()[-1])
    assert (last["ok"], last["outcome"]) == (False, "internal")
    assert "secret-detail" not in out


def test_a_present_extra_passes_in_both_modes(
    machine: InMemorySecureSessionStore, capsys: pytest.CaptureFixture[str]
) -> None:
    for args in ((), ("--for", "release")):
        status, answer = _doctor(*args, capsys=capsys)
        assert (status, answer["failing"]) == (0, [])
        assert answer.get("for") == (args[1] if args else None)
        assert "next" not in _by_name(answer)["extra:github"]


def test_a_broken_install_fails_in_both_modes(
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def load(name: str) -> None:
        raise ModuleNotFoundError("no httpx", name="httpx")

    monkeypatch.setattr(
        doctor, "_extras", lambda task=None: _original_extras(load, task)
    )
    for args in ((), ("--for", "release")):
        status, answer = _doctor(*args, capsys=capsys)
        assert (status, answer["failing"]) == (20, ["extra:github"])
        assert _by_name(answer)["extra:github"]["next"] == (
            "uv tool install --reinstall zeocore"
        )


def test_for_with_an_unknown_job_is_refused_without_quoting_it(
    machine: InMemorySecureSessionStore, capsys: pytest.CaptureFixture[str]
) -> None:
    status, answer = doctor.doctor(["--for", "SENTINEL-JOB"], b"")
    assert (status, answer["outcome"]) == (2, "invalid_request")
    assert "SENTINEL-JOB" not in str(answer)
    assert answer["message"].endswith("zeocore doctor for everything else")
    assert capsys.readouterr().out == ""  # no check printed first


def test_for_with_no_value_is_refused(
    machine: InMemorySecureSessionStore, capsys: pytest.CaptureFixture[str]
) -> None:
    status, answer = doctor.doctor(["--for"], b"")
    assert (status, answer["outcome"]) == (2, "invalid_request")
    assert capsys.readouterr().out == ""


def test_a_bad_for_answers_at_once_with_a_stdin_that_never_closes() -> None:
    """The command line is judged without reading stdin: nothing waits."""
    process = subprocess.Popen(
        [sys.executable, "-m", "zeo_core.cli", "doctor", "--for", "nope"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        out, _ = process.communicate(timeout=30)
    finally:
        if process.stdin is not None:
            process.stdin.close()
        process.kill()
    assert process.returncode == 2
    assert json.loads(out.splitlines()[-1])["outcome"] == "invalid_request"
    assert b"nope" not in out


def test_the_catalogue_names_for_with_its_one_choice() -> None:
    entry = next(entry for entry in ENTRIES if entry.name == "doctor")
    argument = next(arg for arg in entry.arguments if arg.name == "--for")
    assert argument.choices == ("release",)
    assert "[--for release]" in entry.usage
    assert doctor.TASKS == ("release",)


def test_the_dispatcher_carries_for_through_to_the_answer(
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _no_requests(monkeypatch)
    status, answer = run(["doctor", "--for", "release"], lambda: b"")
    assert (status, answer["for"], answer["schema"]) == (
        20,
        "release",
        "zeocore.doctor/v1",
    )
    capsys.readouterr()
