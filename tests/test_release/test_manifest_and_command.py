"""The signing manifest's rules and the ``zeocore release`` command."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from zeo_core.cli.__main__ import COMMANDS
from zeo_core.cli.release import make_command
from zeo_core.release.signing import (
    KeychainState,
    LoadedManifest,
    ManifestError,
    SigningJournal,
    WorkDir,
    load,
)

from .conftest import HOST, FakeToolchain

Release = Callable[..., LoadedManifest]


def _write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "release.yaml"
    path.write_text(
        yaml.safe_dump(document) if not isinstance(document, str) else document
    )
    return path


def _binding(**changes: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "name": "pomar",
        "path": "bin/pomar",
        "unsigned_sha256": "0" * 64,
        "identifier": "ac.zeo.pomar.pomar",
        "audience": "internal",
        "platform": "darwin",
        "entitlements": "none",
    }
    entry.update(changes)
    return entry


@pytest.mark.parametrize(
    "document",
    [
        "{not: yaml",
        "[]",
        {"target": "x"},
        {
            "signing": {
                "release_manifest": 3,
                "product": "pomar",
                "executables": [_binding()],
            }
        },
        {"signing": {"release_manifest": 1, "product": "pomar", "executables": []}},
        {
            "signing": {
                "release_manifest": 1,
                "product": "pomar",
                "executables": [_binding(identifier="ac.zeo.zeort.pomar")],
            }
        },
        {
            "signing": {
                "release_manifest": 1,
                "product": "pomar",
                "executables": [_binding(path="../pomar")],
            }
        },
        {
            "signing": {
                "release_manifest": 1,
                "product": "pomar",
                "executables": [_binding(path="/bin/pomar")],
            }
        },
        {
            "signing": {
                "release_manifest": 1,
                "product": "pomar",
                "executables": [
                    _binding(
                        platform="linux", entitlements={"path": "e", "sha256": "0" * 64}
                    )
                ],
            }
        },
        {
            "signing": {
                "release_manifest": 1,
                "product": "pomar",
                "executables": [
                    _binding(entitlements={"path": "../e", "sha256": "0" * 64})
                ],
            }
        },
        {
            "signing": {
                "release_manifest": 1,
                "product": "pomar",
                "executables": [_binding(), _binding(path="bin/other")],
            }
        },
        {
            "signing": {
                "release_manifest": 1,
                "product": "pomar",
                "executables": [_binding(signer="mine")],
            }
        },
    ],
)
def test_a_bad_manifest_is_refused_before_anything_runs(
    tmp_path: Path, document: object
) -> None:
    with pytest.raises(ManifestError):
        load(_write(tmp_path, document))


def test_a_missing_manifest_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="can't be read"):
        load(tmp_path / "absent.yaml")


def test_runtime_identifiers_follow_the_product_segment(tmp_path: Path) -> None:
    document = {
        "signing": {
            "release_manifest": 1,
            "product": "runtime",
            "executables": [
                _binding(name="zeo", path="bin/zeo", identifier="ac.zeo.runtime.zeo"),
                _binding(
                    name="old",
                    path="bin/old",
                    identifier="org.zeroemployee.zeo",
                    policy="legacy-runtime-installed",
                ),
            ],
        }
    }
    loaded = load(_write(tmp_path, document))
    assert [e.identifier for e in loaded.signing.executables] == [
        "ac.zeo.runtime.zeo",
        "org.zeroemployee.zeo",
    ]


def _run(
    release_command: Callable[[list[str], bytes], tuple[int, dict[str, Any]]],
    args: list[str],
    request: object,
) -> tuple[int, dict[str, Any]]:
    return release_command(args, json.dumps(request).encode())


def test_the_command_signs_then_verifies(release: Release, tmp_path: Path) -> None:
    loaded = release()
    tools = FakeToolchain()
    command = make_command(lambda: tools, {"ZEOCORE_SIGNING_HOST": HOST})
    request = {
        "manifest": str(loaded.root / "release.yaml"),
        "work_dir": str(tmp_path / "work"),
    }

    status, answer = _run(command, ["sign"], request)
    assert (status, answer["ok"], answer["outcome"]) == (0, True, "signed")
    assert answer["receipts"][0]["identifier"] == "ac.zeo.pomar.pomar"

    status, answer = _run(command, ["verify"], request)
    assert (status, answer["outcome"]) == (0, "verified")


@pytest.mark.parametrize(
    ("tools", "environ", "status", "outcome"),
    [
        (FakeToolchain(), {}, 20, "signing_host_unconfigured"),
        (
            FakeToolchain(keychain=KeychainState.LOCKED),
            {"ZEOCORE_SIGNING_HOST": HOST},
            11,
            "signing_keychain_locked",
        ),
        (
            FakeToolchain(lose_answer=True),
            {"ZEOCORE_SIGNING_HOST": HOST},
            13,
            "signing_attempt_unreconciled",
        ),
    ],
)
def test_the_command_uses_the_exit_family(
    release: Release,
    tmp_path: Path,
    tools: FakeToolchain,
    environ: dict[str, str],
    status: int,
    outcome: str,
) -> None:
    loaded = release()
    command = make_command(lambda: tools, environ)
    request = {
        "manifest": str(loaded.root / "release.yaml"),
        "work_dir": str(tmp_path / "work"),
    }
    got_status, answer = _run(command, ["sign"], request)
    assert (got_status, answer["ok"], answer["outcome"]) == (status, False, outcome)


def test_the_command_holds_while_another_run_signs(
    release: Release, tmp_path: Path
) -> None:
    loaded = release()
    work = WorkDir(tmp_path / "work")
    work.prepare()
    holder = SigningJournal(work.root / "journal.jsonl")
    command = make_command(FakeToolchain, {"ZEOCORE_SIGNING_HOST": HOST})
    try:
        status, answer = _run(
            command,
            ["sign"],
            {"manifest": str(loaded.root / "release.yaml"), "work_dir": str(work.root)},
        )
    finally:
        holder.close()
    assert (status, answer["outcome"]) == (20, "signing_in_progress")


@pytest.mark.parametrize(
    ("args", "stdin"),
    [
        (["publish"], b"{}"),
        ([], b"{}"),
        (["sign"], b"not json"),
        (["sign"], b'{"manifest": "x"}'),
        (["sign"], b'{"manifest": "x", "work_dir": 3}'),
        (["verify"], b'{"manifest": "/absent/release.yaml", "work_dir": "/w"}'),
    ],
)
def test_the_command_refuses_a_bad_request(args: list[str], stdin: bytes) -> None:
    status, answer = make_command(FakeToolchain, {})(args, stdin)
    assert (status, answer["outcome"]) == (2, "invalid_request")


def test_release_is_a_registered_command() -> None:
    assert "release" in COMMANDS
