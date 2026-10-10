"""Request and response packs (SOW-23 §2): the build host to matormini and back."""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from zeo_core.cli.release import make_command
from zeo_core.release.signing import (
    LoadedManifest,
    PackError,
    SigningRequest,
    WorkDir,
    create_request,
    create_response,
    open_request,
    open_response,
    sign,
    verify,
)

from .conftest import HOST, VIRTUALIZATION, FakeToolchain

Release = Callable[..., LoadedManifest]
COMMIT = "a" * 40


def _request(product: str = "pomar") -> SigningRequest:
    return SigningRequest(
        pack_version=1,
        product=product,
        repo="rodriveracom/pomar",
        commit=COMMIT,
        manifest_path="release.yaml",
    )


def _two(release: Release) -> LoadedManifest:
    return release(
        {"name": "pomar"}, {"name": "pomar-host", "entitlements": VIRTUALIZATION}
    )


def test_the_round_trip_signs_on_one_host_and_verifies_on_another(
    release: Release, tmp_path: Path
) -> None:
    built = _two(release)
    request_id = create_request(built, _request(), tmp_path / "request.tar")

    opened = open_request(tmp_path / "request.tar", tmp_path / "matormini" / "in")
    assert opened.request_id == request_id
    assert opened.request.commit == COMMIT
    assert opened.manifest_bytes == built.manifest_path.read_bytes()

    signer = FakeToolchain()
    signing_work = WorkDir(tmp_path / "matormini" / "work")
    assert sign(opened.manifest, signing_work, signer, HOST).status == "done"
    create_response(opened.manifest, signing_work, tmp_path / "response.tar")

    returned = open_response(
        built, tmp_path / "response.tar", tmp_path / "eng-uk" / "work"
    )
    # The build host's verify reads its own copy of what came back.
    checker = FakeToolchain(host_name="eng-uk-0006")
    for item in built.signing.executables:
        checker.inspections[returned.candidate(item.name)] = signer.inspections[
            signing_work.candidate(item.name)
        ]
    outcome = verify(built, returned, checker)
    assert (outcome.status, outcome.reason) == ("done", "verified")


def test_the_same_inputs_give_the_same_request_id(
    release: Release, tmp_path: Path
) -> None:
    built = _two(release)
    first = create_request(built, _request(), tmp_path / "one.tar")
    second = create_request(built, _request(), tmp_path / "two.tar")
    assert first == second


def test_a_changed_input_is_refused_when_packing(
    release: Release, tmp_path: Path
) -> None:
    built = _two(release)
    (built.root / "bin" / "pomar").write_bytes(b"changed")
    with pytest.raises(PackError) as caught:
        create_request(built, _request(), tmp_path / "request.tar")
    assert caught.value.reason == "input_mismatch"


def test_a_request_for_another_product_is_refused(
    release: Release, tmp_path: Path
) -> None:
    with pytest.raises(PackError) as caught:
        create_request(_two(release), _request("runtime"), tmp_path / "r.tar")
    assert caught.value.reason == "request_invalid"


def _tar(path: Path, members: dict[str, bytes], *, link: str | None = None) -> None:
    with tarfile.open(path, "w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if link is not None:
            info = tarfile.TarInfo(link)
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(info)


def _repack(
    source: Path, out: Path, change: Callable[[dict[str, bytes]], object]
) -> None:
    with tarfile.open(source) as tar:
        members = {}
        for info in tar:
            handle = tar.extractfile(info)
            assert handle is not None
            members[info.name] = handle.read()
    change(members)
    _tar(out, members)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda m: m.update({"extra.sh": b"echo"}), "input_mismatch"),
        (lambda m: m.update({"bin/pomar": b"swapped"}), "input_mismatch"),
        (lambda m: m.pop("bin/pomar-host"), "input_mismatch"),
        (lambda m: m.pop("request.json"), "request_invalid"),
        (lambda m: m.update({"request.json": b"{}"}), "request_invalid"),
        (lambda m: m.update({"release.yaml": b"signing: 3"}), "request_invalid"),
        (lambda m: m.update({"../escape": b"x"}), "request_invalid"),
        (lambda m: m.update({"/abs": b"x"}), "request_invalid"),
        (
            lambda m: m.update(
                {
                    "request.json": json.dumps(
                        {**json.loads(m["request.json"]), "product": "runtime"}
                    ).encode()
                }
            ),
            "request_invalid",
        ),
    ],
)
def test_a_bad_request_pack_is_refused(
    release: Release,
    tmp_path: Path,
    change: Callable[[dict[str, bytes]], object],
    reason: str,
) -> None:
    create_request(_two(release), _request(), tmp_path / "good.tar")
    _repack(tmp_path / "good.tar", tmp_path / "bad.tar", change)
    with pytest.raises(PackError) as caught:
        open_request(tmp_path / "bad.tar", tmp_path / "in")
    assert caught.value.reason == reason


def test_links_and_non_tar_files_are_refused(tmp_path: Path) -> None:
    _tar(tmp_path / "link.tar", {}, link="bin/pomar")
    with pytest.raises(PackError, match="unsafe"):
        open_request(tmp_path / "link.tar", tmp_path / "a")
    (tmp_path / "junk.tar").write_bytes(b"not a tar file at all" * 40)
    with pytest.raises(PackError, match="not a tar"):
        open_request(tmp_path / "junk.tar", tmp_path / "b")
    with pytest.raises(PackError, match="missing or too large"):
        open_request(tmp_path / "absent.tar", tmp_path / "c")


def test_a_pack_is_never_opened_into_a_used_directory(
    release: Release, tmp_path: Path
) -> None:
    create_request(_two(release), _request(), tmp_path / "r.tar")
    (tmp_path / "in").mkdir()
    with pytest.raises(FileExistsError):
        open_request(tmp_path / "r.tar", tmp_path / "in")


def test_a_response_must_match_the_manifest(release: Release, tmp_path: Path) -> None:
    built = _two(release)
    work = WorkDir(tmp_path / "work")
    with pytest.raises(PackError, match="has not been signed"):
        create_response(built, work, tmp_path / "early.tar")
    sign(built, work, FakeToolchain(), HOST)
    create_response(built, work, tmp_path / "response.tar")

    _repack(
        tmp_path / "response.tar",
        tmp_path / "short.tar",
        lambda m: m.pop("signed/pomar"),
    )
    with pytest.raises(PackError) as caught:
        open_response(built, tmp_path / "short.tar", tmp_path / "a")
    assert caught.value.reason == "input_mismatch"

    _repack(
        tmp_path / "response.tar",
        tmp_path / "junk.tar",
        lambda m: m.update({"receipts/sign-pomar.json": b"not json"}),
    )
    with pytest.raises(PackError) as caught:
        open_response(built, tmp_path / "junk.tar", tmp_path / "b")
    assert caught.value.reason == "request_invalid"


def test_the_pack_and_receive_commands(release: Release, tmp_path: Path) -> None:
    built = _two(release)
    signer = FakeToolchain()
    command = make_command(lambda: signer, {})
    manifest = str(built.manifest_path)

    status, answer = command(
        ["pack"],
        json.dumps(
            {
                "manifest": manifest,
                "out": str(tmp_path / "r.tar"),
                "repo": "rodriveracom/pomar",
                "commit": COMMIT,
                "manifest_path": "release.yaml",
            }
        ).encode(),
    )
    assert (status, answer["outcome"]) == (0, "packed")
    assert len(answer["request_id"]) == 64

    work = WorkDir(tmp_path / "signing")
    sign(built, work, signer, HOST)
    create_response(built, work, tmp_path / "response.tar")
    receive = {
        "manifest": manifest,
        "response": str(tmp_path / "response.tar"),
        "work_dir": str(tmp_path / "back"),
    }
    # The fake reads signatures by path, so point the returned copies at them.
    for item in built.signing.executables:
        signer.inspections[tmp_path / "back" / "signed" / item.name] = (
            signer.inspections[work.candidate(item.name)]
        )
    status, answer = command(["receive"], json.dumps(receive).encode())
    assert (status, answer["outcome"]) == (0, "verified")

    status, answer = command(["receive"], json.dumps(receive).encode())
    assert (status, answer["outcome"]) == (2, "invalid_request")


@pytest.mark.parametrize(
    ("verb", "fields", "status", "outcome"),
    [
        ("pack", {"commit": "short"}, 2, "invalid_request"),
        ("pack", {"repo": "no-slash"}, 2, "invalid_request"),
        ("receive", {"response": "/absent.tar"}, 20, "request_invalid"),
    ],
)
def test_bad_pack_and_receive_requests(
    release: Release,
    tmp_path: Path,
    verb: str,
    fields: dict[str, str],
    status: int,
    outcome: str,
) -> None:
    built = _two(release)
    base = (
        {
            "out": str(tmp_path / "r.tar"),
            "repo": "rodriveracom/pomar",
            "commit": COMMIT,
            "manifest_path": "release.yaml",
        }
        if verb == "pack"
        else {"response": "x", "work_dir": str(tmp_path / "w")}
    )
    request = {"manifest": str(built.manifest_path), **base, **fields}
    got_status, answer = make_command(FakeToolchain, {})(
        [verb], json.dumps(request).encode()
    )
    assert (got_status, answer["outcome"]) == (status, outcome)


def test_pack_refuses_a_changed_input(release: Release, tmp_path: Path) -> None:
    built = _two(release)
    (built.root / "bin" / "pomar").write_bytes(b"changed")
    request = {
        "manifest": str(built.manifest_path),
        "out": str(tmp_path / "r.tar"),
        "repo": "o/r",
        "commit": COMMIT,
        "manifest_path": "release.yaml",
    }
    status, answer = make_command(FakeToolchain, {})(
        ["pack"], json.dumps(request).encode()
    )
    assert (status, answer["outcome"]) == (20, "input_mismatch")
