"""``zeocore doctor``: one local, read-only check per line, each naming its fix."""

from __future__ import annotations

import ast
import inspect
import json
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
from zeo_core.cli.__main__ import run
from zeo_core.cli.catalogue import ENTRIES
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
    monkeypatch.setattr(client, "make_store", lambda profile: store)
    monkeypatch.setattr(client, "make_transport", lambda store: Transport())
    monkeypatch.setattr(client, "now", lambda: NOW)
    monkeypatch.setattr(doctor, "_extras", lambda: _original_extras(lambda name: None))
    for name in (client.PROFILE_ENV, SESSION_FILE_ENV, "ZEOCONNECT_DEVELOPMENT"):
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
        "session_store",
        "pairing",
        "zeoconnect_url",
        "extra:github",
    ]
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
    def broken(profile: str | None) -> InMemorySecureSessionStore:
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
    assert (status, answer["failing"]) == (20, ["zeoconnect_url"])
    check = _by_name(answer)["zeoconnect_url"]
    assert check["origin"] == "custom"
    assert "ZEOCONNECT_DEVELOPMENT=1" in check["next"]
    assert "evil.example" not in str(answer)


def test_a_missing_extra_names_the_install(
    machine: InMemorySecureSessionStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def load(name: str) -> None:
        raise ModuleNotFoundError("no requests", name="requests")

    monkeypatch.setattr(doctor, "_extras", lambda: _original_extras(load))
    status, answer = _doctor(capsys=capsys)
    assert (status, answer["failing"]) == (20, ["extra:github"])
    assert _by_name(answer)["extra:github"]["next"] == "pip install 'zeocore[github]'"


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

    def make_store(profile: str | None) -> InMemorySecureSessionStore:
        seen.append(profile)
        return machine

    monkeypatch.setattr(client, "make_store", make_store)
    monkeypatch.setenv(client.PROFILE_ENV, "studio")
    _doctor(capsys=capsys)
    _doctor("--profile", "other", capsys=capsys)
    assert seen == ["studio", "other"]


def test_a_bad_profile_or_flag_is_invalid_input_not_a_diagnosis(
    machine: InMemorySecureSessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(profile: str | None) -> InMemorySecureSessionStore:
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
