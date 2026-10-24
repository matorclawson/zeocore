"""A reviewed post-build inventory as the signing manifest's digests (SOW-44 rev 2).

The manifest keeps the reviewed structure; the inventory holds the digests;
the result is the same pinned manifest the step has always signed.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import tarfile
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
import yaml

from zeo_core.cli.release import make_command
from zeo_core.release.signing import (
    LoadedManifest,
    ManifestError,
    PackError,
    SigningRequest,
    WorkDir,
    create_request,
    load,
    open_request,
    sign,
    verify,
)
from zeo_core.release.signing.admission import (
    GhCli,
    Grant,
    ReviewUnreadableError,
    admit,
)
from zeo_core.release.signing.inventory import (
    APPROVER_USER_ID,
    SCHEMA,
    ChangedFile,
    InventoryError,
    Status,
    check_review,
    git_blob_sha,
    inventory_path,
    parse,
    pin_keys,
)
from zeo_core.release.signing.service import SigningService
from zeo_core.release.signing.toolchain import Completed

from .conftest import HOST, FakeToolchain
from .test_service import FakeGitHub, Runner

Release = Callable[..., LoadedManifest]


def _lift(signing: dict[str, Any]) -> dict[str, str]:
    """Pop every pin out of a pinned ``signing`` section, keyed as the inventory is."""
    pins: dict[str, str] = {}

    def main(entry: dict[str, Any], key: str) -> None:
        declared = entry.get("main_executable")
        if isinstance(declared, dict):
            pins[key] = declared.pop("unsigned_sha256")

    for item in signing.get("executables", []):
        pins[f"executable:{item['name']}"] = item.pop("unsigned_sha256")
    for bundle in signing.get("bundles", []):
        name = bundle["name"]
        pins[f"bundle:{name}:tree"] = bundle.pop("unsigned_tree_sha256")
        main(bundle, f"bundle:{name}:main")
        for nested in bundle.get("nested", []):
            pins[f"bundle:{name}:nested:{nested['path']}"] = nested.pop(
                "unsigned_sha256"
            )
            main(nested, f"bundle:{name}:nested:{nested['path']}:main")
    return pins


def _inventory(product: str, pins: dict[str, str], **changes: Any) -> bytes:  # noqa: ANN401
    document: dict[str, Any] = {
        "schema": SCHEMA,
        "product": product,
        "version": "1.2.3",
        "pins": pins,
    }
    document.update(changes)
    return json.dumps(document).encode()


def _convert(loaded: LoadedManifest) -> tuple[dict[str, Any], dict[str, str]]:
    """The pinned manifest's document with its pins lifted into an inventory."""
    document = yaml.safe_load(loaded.manifest_path.read_bytes())
    pins = _lift(document["signing"])
    document["signing"]["pins"] = "inventory"
    return document, pins


def _written(loaded: LoadedManifest, document: dict[str, Any]) -> Path:
    path = loaded.manifest_path.with_name("inventory-mode.yaml")
    path.write_text(yaml.safe_dump(document))
    return path


def _same(a: LoadedManifest, b: LoadedManifest) -> bool:
    return a.signing.model_dump(exclude={"pins"}) == b.signing.model_dump(
        exclude={"pins"}
    )


def test_an_inventory_gives_the_same_pinned_manifest_for_executables(
    release: Release,
) -> None:
    pinned = release({"name": "pomar"}, {"name": "pomar-host"})
    document, pins = _convert(pinned)
    path = _written(pinned, document)

    loaded = load(path, _inventory("pomar", pins))

    assert _same(loaded, pinned)
    assert loaded.signing.pins == "inventory"
    assert loaded.inventory_sha256 is not None


def test_an_inventory_gives_the_same_pinned_manifest_for_a_bundle(
    app: Callable[..., LoadedManifest],
) -> None:
    pinned = app(framework=True)
    document, pins = _convert(pinned)
    assert any(":nested:" in key for key in pins)
    assert any(key.endswith(":main") for key in pins)
    path = _written(pinned, document)

    loaded = load(
        path,
        _inventory(pinned.signing.product, pins),
    )

    assert _same(loaded, pinned)
    assert sorted(pin_keys(document["signing"])) == sorted(pins)


def test_an_inventory_manifest_signs_and_verifies_like_a_pinned_one(
    release: Release, tmp_path: Path
) -> None:
    pinned = release({"name": "pomar"})
    document, pins = _convert(pinned)
    loaded = load(_written(pinned, document), _inventory("pomar", pins))
    tools = FakeToolchain()
    work = WorkDir(tmp_path / "work")

    signed = sign(loaded, work, tools, HOST)
    checked = verify(loaded, work, tools)

    assert (signed.status, signed.reason) == ("done", "signed")
    assert (checked.status, checked.reason) == ("done", "verified")


def test_a_changed_artifact_no_longer_matches_its_inventory_pin(
    release: Release, tmp_path: Path
) -> None:
    pinned = release({"name": "pomar"})
    document, pins = _convert(pinned)
    loaded = load(_written(pinned, document), _inventory("pomar", pins))
    (loaded.root / "bin" / "pomar").write_bytes(b"not what was reviewed")

    signed = sign(loaded, WorkDir(tmp_path / "work"), FakeToolchain(), HOST)

    assert signed.status != "done"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda pins: pins.pop("executable:pomar"),
        lambda pins: pins.update({"executable:other": "a" * 64}),
        lambda pins: pins.update({"executable:pomar": "xyz"}),
    ],
    ids=["missing pin", "unexpected pin", "not a digest"],
)
def test_an_inventory_that_does_not_match_the_manifest_refuses(
    release: Release, mutate: Callable[[dict[str, str]], object]
) -> None:
    pinned = release({"name": "pomar"})
    document, pins = _convert(pinned)
    mutate(pins)
    with pytest.raises(ManifestError):
        load(_written(pinned, document), _inventory("pomar", pins))


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"[]",
        json.dumps({"schema": "other/v1", "product": "pomar", "pins": {}}).encode(),
        json.dumps({"product": "pomar", "pins": {}}).encode(),
        json.dumps(
            {"schema": SCHEMA, "product": "pomar", "pins": {}, "extra": 1}
        ).encode(),
    ],
    ids=["not json", "not an object", "wrong schema", "no schema", "extra field"],
)
def test_an_unreadable_inventory_refuses(release: Release, content: bytes) -> None:
    pinned = release({"name": "pomar"})
    document, _ = _convert(pinned)
    with pytest.raises(ManifestError):
        load(_written(pinned, document), content)


def test_an_inventory_for_another_product_refuses(release: Release) -> None:
    pinned = release({"name": "pomar"})
    document, pins = _convert(pinned)
    with pytest.raises(ManifestError, match="another product"):
        load(_written(pinned, document), _inventory("other", pins))


def test_inventory_mode_without_an_inventory_refuses(release: Release) -> None:
    pinned = release({"name": "pomar"})
    document, _ = _convert(pinned)
    with pytest.raises(ManifestError, match="none was given"):
        load(_written(pinned, document))


def test_a_manifest_that_pins_its_own_digests_refuses_an_inventory(
    release: Release,
) -> None:
    pinned = release({"name": "pomar"})
    _, pins = _convert(pinned)
    with pytest.raises(ManifestError, match="second source of pins"):
        load(pinned.manifest_path, _inventory("pomar", pins))


def test_a_pin_left_in_an_inventory_mode_manifest_refuses(release: Release) -> None:
    pinned = release({"name": "pomar"})
    document, pins = _convert(pinned)
    document["signing"]["executables"][0]["unsigned_sha256"] = "b" * 64
    with pytest.raises(ManifestError, match="already pins"):
        load(_written(pinned, document), _inventory("pomar", pins))


def test_the_inventory_does_not_change_the_manifest_it_was_applied_to(
    release: Release,
) -> None:
    pinned = release({"name": "pomar"})
    document, pins = _convert(pinned)
    before = copy.deepcopy(document)
    load(_written(pinned, document), _inventory("pomar", pins))
    assert document == before
    assert parse(_inventory("pomar", pins)).pins == pins


VERSION = "1.2.3"


class _Github:
    """What the host reads back; each field is one thing GitHub could say."""

    def __init__(
        self,
        *,
        merged: bool = True,
        held: bytes | None = b"inventory",
        statuses: list[Status] | None = None,
        changed: list[ChangedFile] | None | str = "default",
    ) -> None:
        self.merged = merged
        self.held = held
        self.statuses = (
            [Status("success", APPROVER_USER_ID)] if statuses is None else statuses
        )
        self.changed = (
            [ChangedFile(inventory_path(VERSION), "added", git_blob_sha(b"inventory"))]
            if changed == "default"
            else changed
        )
        self.asked: list[str] = []

    def default_branch(self, repo: str) -> str:
        return "main"

    def file_at(self, repo: str, commit: str, path: str) -> bytes | None:
        self.asked.append(path)
        return self.held

    def is_ancestor(self, repo: str, commit: str, branch: str) -> bool:
        return self.merged

    def approval_statuses(self, repo: str, commit: str, context: str) -> list[Status]:
        assert context == "council/approve"
        return self.statuses

    def merged_pr_files(
        self, repo: str, head: str, branch: str
    ) -> list[ChangedFile] | None:
        return self.changed  # type: ignore[return-value]


def _review(
    github: _Github,
    content: bytes = b"inventory",
    *,
    path: str | None = None,
    version: str = VERSION,
) -> str | None:
    return check_review(
        github,
        repo="zeroemployeeorg/pomar",
        commit="a" * 40,
        path=inventory_path(version) if path is None else path,
        version=version,
        content=content,
    )


def test_a_merged_inventory_added_by_its_own_reviewed_pull_request_is_reviewed() -> (
    None
):
    github = _Github()
    assert _review(github) is None
    assert github.asked == [inventory_path(VERSION)]


@pytest.mark.parametrize(
    ("github", "content", "why"),
    [
        (_Github(merged=False), b"inventory", "not merged"),
        (_Github(held=None), b"inventory", "no inventory"),
        (_Github(held=b"other bytes"), b"inventory", "not the one in the commit"),
        (_Github(), b"other bytes", "not the one in the commit"),
        (_Github(statuses=[]), b"inventory", "adopted approver"),
        (
            _Github(statuses=[Status("success", APPROVER_USER_ID + 1)]),
            b"inventory",
            "adopted approver",
        ),
        (_Github(changed=None), b"inventory", "no single merged pull request"),
        (_Github(changed=[]), b"inventory", "did not add or change"),
    ],
    ids=[
        "unmerged",
        "absent",
        "differs from the commit",
        "caller bytes differ",
        "no approval",
        "approval by someone else",
        "no pull request",
        "pull request changed nothing",
    ],
)
def test_inventory_evidence_that_is_not_the_adopted_review_refuses(
    github: _Github, content: bytes, why: str
) -> None:
    reason = _review(github, content)
    assert reason is not None and why in reason


def test_a_path_that_is_not_the_versions_own_is_refused() -> None:
    reason = _review(_Github(), path="release/inventory/0.0.1.json")
    assert reason is not None and "is not release/inventory/1.2.3.json" in reason
    reason = _review(_Github(), path="tests/fixtures/inventory.json")
    assert reason is not None and "is not release/inventory/1.2.3.json" in reason


@pytest.mark.parametrize(
    "entry",
    [
        ChangedFile(inventory_path(VERSION), "added", "0" * 40),  # another blob
        ChangedFile(inventory_path(VERSION), "removed", git_blob_sha(b"inventory")),
        ChangedFile(inventory_path(VERSION), "renamed", git_blob_sha(b"inventory")),
        ChangedFile(
            "release/inventory/0.0.1.json", "added", git_blob_sha(b"inventory")
        ),
    ],
    ids=["other blob", "removed", "renamed", "another file"],
)
def test_the_pull_request_must_have_added_or_changed_exactly_this_blob(
    entry: ChangedFile,
) -> None:
    reason = _review(_Github(changed=[entry]))
    assert reason is not None and "did not add or change" in reason


def test_a_changed_inventory_in_the_pull_request_counts_too() -> None:
    changed = [
        ChangedFile(inventory_path(VERSION), "modified", git_blob_sha(b"inventory"))
    ]
    assert _review(_Github(changed=changed)) is None


@pytest.mark.parametrize(
    "statuses",
    [
        # a later failure, error or pending supersedes an earlier success
        [Status("failure", APPROVER_USER_ID), Status("success", APPROVER_USER_ID)],
        [Status("error", APPROVER_USER_ID), Status("success", APPROVER_USER_ID)],
        [Status("pending", APPROVER_USER_ID), Status("success", APPROVER_USER_ID)],
        # a newer success by someone else sits over the approver's older one
        [Status("success", 7), Status("success", APPROVER_USER_ID)],
        # an id that is not an integer matches nobody
        [Status("success", None)],
    ],
    ids=[
        "failure after",
        "error after",
        "pending after",
        "other account newer",
        "no id",
    ],
)
def test_only_the_latest_approval_status_decides(statuses: list[Status]) -> None:
    reason = _review(_Github(statuses=statuses))
    assert reason is not None and "adopted approver" in reason


def test_parse_reports_inventory_errors_as_inventory_errors() -> None:
    with pytest.raises(InventoryError):
        parse(b"{")


def test_an_inventory_that_repeats_a_key_is_refused() -> None:
    """json keeps the last of two equal keys; the reviewed digest is the used one."""
    content = (
        b'{"schema": "zeo.build-inventory/v1", "product": "pomar", "version": "1.2.3",'
        b' "pins": {"executable:pomar": "'
        + b"a" * 64
        + b'", "executable:pomar": "'
        + b"b" * 64
        + b'"}}'
    )
    with pytest.raises(InventoryError, match="repeats a key"):
        parse(content)


@pytest.mark.parametrize("version", ["1.2", "v1.2.3", "1.2.3.4", "../1.2.3", ""])
def test_an_inventory_version_is_a_plain_release_number(version: str) -> None:
    content = _inventory("pomar", {}, version=version)
    with pytest.raises(InventoryError):
        parse(content)


# -- the inventory travels with the request, and admission holds it to its review --

NODE = "eng-uk-0006.tail29b7b0.ts.net"
REPO = "zeroemployeeorg/pomar"
COMMIT = "c" * 40
INVENTORY_PATH = inventory_path(VERSION)
GRANTS = {NODE: {"pomar": Grant(REPO, frozenset({"ac.zeo.pomar.pomar"}))}}


def _inventory_pack(
    release: Release,
    tmp_path: Path,
    **request: Any,  # noqa: ANN401
) -> tuple[Path, LoadedManifest, bytes]:
    pinned = release({"name": "pomar"})
    document, pins = _convert(pinned)
    content = _inventory("pomar", pins)
    loaded = load(_written(pinned, document), content)
    described = {
        "pack_version": 1,
        "product": "pomar",
        "repo": REPO,
        "commit": COMMIT,
        "manifest_path": "release.yaml",
        "inventory_path": INVENTORY_PATH,
        "version": VERSION,
        **request,
    }
    out = tmp_path / "request.tar"
    create_request(loaded, SigningRequest(**described), out)
    return out, loaded, content


def _members(pack: Path) -> dict[str, bytes]:
    with tarfile.open(pack) as tar:
        return {
            m.name: tar.extractfile(m).read()  # type: ignore[union-attr]
            for m in tar.getmembers()
            if m.isreg()
        }


def _repacked(pack: Path, out: Path, **changes: bytes | None) -> Path:
    """The pack with some members replaced or, given None, dropped."""
    members = _members(pack)
    for name, data in changes.items():
        if data is None:
            members.pop(name.replace("__", "."), None)
        else:
            members[name.replace("__", ".")] = data
    with tarfile.open(out, "w") as tar:
        for name, data in sorted(members.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out


def _plain_request(**extra: object) -> bytes:
    return json.dumps(
        {
            "pack_version": 1,
            "product": "pomar",
            "repo": REPO,
            "commit": COMMIT,
            "manifest_path": "release.yaml",
            **extra,
        }
    ).encode()


def test_the_inventory_rides_in_the_pack_and_comes_out_unchanged(
    release: Release, tmp_path: Path
) -> None:
    pack, loaded, content = _inventory_pack(release, tmp_path)

    assert _members(pack)["inventory.json"] == content
    opened = open_request(pack, tmp_path / "opened")

    assert opened.inventory_bytes == content
    assert opened.request.inventory_path == INVENTORY_PATH
    assert opened.request.version == VERSION
    assert opened.manifest.signing == loaded.signing


def test_a_request_with_no_inventory_is_byte_for_byte_what_it_always_was(
    release: Release, tmp_path: Path
) -> None:
    built = release({"name": "pomar"})
    request = SigningRequest(
        pack_version=1,
        product="pomar",
        repo=REPO,
        commit=COMMIT,
        manifest_path="release.yaml",
    )
    out = tmp_path / "plain.tar"
    create_request(built, request, out)

    members = _members(out)
    assert "inventory.json" not in members
    assert b"inventory_path" not in members["request.json"]
    assert b'"version"' not in members["request.json"]
    assert json.loads(members["request.json"]) == json.loads(_plain_request())


def test_an_inventory_manifest_needs_its_path_and_version_in_the_request(
    release: Release, tmp_path: Path
) -> None:
    pinned = release({"name": "pomar"})
    document, pins = _convert(pinned)
    loaded = load(_written(pinned, document), _inventory("pomar", pins))
    bare = SigningRequest(
        pack_version=1,
        product="pomar",
        repo=REPO,
        commit=COMMIT,
        manifest_path="release.yaml",
    )
    with pytest.raises(PackError, match="needs its inventory"):
        create_request(loaded, bare, tmp_path / "a.tar")


def test_a_request_for_another_version_than_the_inventorys_refuses(
    release: Release, tmp_path: Path
) -> None:
    with pytest.raises(PackError, match="needs its inventory"):
        _inventory_pack(
            release,
            tmp_path,
            version="9.9.9",
            inventory_path=inventory_path("9.9.9"),
        )


def test_a_manifest_with_its_own_pins_takes_no_inventory_path(
    release: Release, tmp_path: Path
) -> None:
    built = release({"name": "pomar"})
    described = SigningRequest(
        pack_version=1,
        product="pomar",
        repo=REPO,
        commit=COMMIT,
        manifest_path="release.yaml",
        inventory_path=INVENTORY_PATH,
        version=VERSION,
    )
    with pytest.raises(PackError, match="no other manifest takes them"):
        create_request(built, described, tmp_path / "b.tar")


@pytest.mark.parametrize(
    "extra",
    [
        {"inventory_path": "release/inventory/1.2.3.json"},
        {"version": "1.2.3"},
        {"inventory_path": "release/inventory/9.9.9.json", "version": "1.2.3"},
        {"inventory_path": "tests/inv.json", "version": "1.2.3"},
    ],
    ids=["path alone", "version alone", "path of another version", "any other path"],
)
def test_a_request_binds_the_inventory_path_to_its_version(
    extra: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        SigningRequest.model_validate_json(_plain_request(**extra))


@pytest.mark.parametrize(
    "path",
    ["../release.yaml", "a/../b.yaml", "./release.yaml", "a//b.yaml", "a/./b.yaml"],
)
def test_a_repository_path_has_no_empty_dot_or_dotdot_segment(path: str) -> None:
    with pytest.raises(ValueError):
        SigningRequest.model_validate_json(
            json.dumps(
                {
                    "pack_version": 1,
                    "product": "pomar",
                    "repo": REPO,
                    "commit": COMMIT,
                    "manifest_path": path,
                }
            ).encode()
        )
    with pytest.raises(ValueError):
        SigningRequest.model_validate_json(
            _plain_request(inventory_path=path, version=VERSION)
        )


def test_a_pack_that_lacks_the_inventory_its_request_names_refuses(
    release: Release, tmp_path: Path
) -> None:
    pack, _, _ = _inventory_pack(release, tmp_path)
    cut = _repacked(pack, tmp_path / "cut.tar", inventory__json=None)
    with pytest.raises(PackError):
        open_request(cut, tmp_path / "opened-cut")


def test_a_pack_with_an_inventory_its_request_does_not_name_refuses(
    release: Release, tmp_path: Path
) -> None:
    pack, _, _ = _inventory_pack(release, tmp_path)
    swapped = _repacked(pack, tmp_path / "swap.tar", request__json=_plain_request())
    with pytest.raises(PackError):
        open_request(swapped, tmp_path / "opened-swap")


def test_a_pack_whose_request_names_another_version_refuses(
    release: Release, tmp_path: Path
) -> None:
    pack, _, _ = _inventory_pack(release, tmp_path)
    other = _plain_request(inventory_path=inventory_path("9.9.9"), version="9.9.9")
    swapped = _repacked(pack, tmp_path / "other.tar", request__json=other)
    with pytest.raises(PackError):
        open_request(swapped, tmp_path / "opened-other")


def test_a_swapped_inventory_in_the_pack_no_longer_matches_the_artifacts(
    release: Release, tmp_path: Path
) -> None:
    pack, _, content = _inventory_pack(release, tmp_path)
    document = json.loads(content)
    document["pins"]["executable:pomar"] = "f" * 64
    swapped = _repacked(
        pack, tmp_path / "swapped.tar", inventory__json=json.dumps(document).encode()
    )
    with pytest.raises(PackError, match="differs from the manifest"):
        open_request(swapped, tmp_path / "opened-swapped")


class _ApprovingGithub(FakeGitHub):
    """The host's reads, with a reviewed inventory in the commit by default."""

    def __init__(
        self,
        content: bytes,
        *,
        statuses: list[Status] | None = None,
        changed: list[ChangedFile] | None | str = "default",
    ) -> None:
        super().__init__()
        self.statuses = (
            [Status("success", APPROVER_USER_ID)] if statuses is None else statuses
        )
        self.changed = (
            [ChangedFile(INVENTORY_PATH, "added", git_blob_sha(content))]
            if changed == "default"
            else changed
        )

    def approval_statuses(self, repo: str, commit: str, context: str) -> list[Status]:
        assert context == "council/approve"
        return self.statuses

    def merged_pr_files(
        self, repo: str, head: str, branch: str
    ) -> list[ChangedFile] | None:
        return self.changed  # type: ignore[return-value]


def _reviewed_inventory(
    loaded: LoadedManifest,
    content: bytes,
    **kw: Any,  # noqa: ANN401
) -> _ApprovingGithub:
    github = _ApprovingGithub(content, **kw)
    github.files[(REPO, COMMIT, "release.yaml")] = loaded.manifest_path.read_bytes()
    github.files[(REPO, COMMIT, INVENTORY_PATH)] = content
    github.merged.add(COMMIT)
    return github


def test_an_inventory_with_the_approvers_review_is_admitted(
    release: Release, tmp_path: Path
) -> None:
    pack, loaded, content = _inventory_pack(release, tmp_path)
    opened = open_request(pack, tmp_path / "opened")
    assert admit(opened, NODE, _reviewed_inventory(loaded, content), GRANTS) is None


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"statuses": []}, "adopted approver"),
        ({"statuses": [Status("success", APPROVER_USER_ID + 1)]}, "adopted approver"),
        (
            {
                "statuses": [
                    Status("failure", APPROVER_USER_ID),
                    Status("success", APPROVER_USER_ID),
                ]
            },
            "adopted approver",
        ),
        ({"changed": None}, "no single merged pull request"),
        ({"changed": []}, "did not add or change"),
    ],
    ids=[
        "no approval",
        "approved by another account",
        "superseded by a failure",
        "no pull request",
        "the pull request did not touch it",
    ],
)
def test_an_inventory_without_the_approvers_review_is_refused(
    release: Release, tmp_path: Path, change: dict[str, Any], why: str
) -> None:
    pack, loaded, content = _inventory_pack(release, tmp_path)
    opened = open_request(pack, tmp_path / "opened")

    refusal = admit(
        opened, NODE, _reviewed_inventory(loaded, content, **change), GRANTS
    )

    assert refusal is not None
    assert (refusal.status, refusal.reason) == ("refused", "inventory_unreviewed")
    assert why in refusal.message


def test_an_inventory_not_in_the_commits_tree_is_refused(
    release: Release, tmp_path: Path
) -> None:
    pack, loaded, content = _inventory_pack(release, tmp_path)
    opened = open_request(pack, tmp_path / "opened")
    github = _reviewed_inventory(loaded, content)
    del github.files[(REPO, COMMIT, INVENTORY_PATH)]

    refusal = admit(opened, NODE, github, GRANTS)

    assert refusal is not None and refusal.reason == "inventory_unreviewed"


def test_an_inventory_that_differs_from_the_commits_is_refused(
    release: Release, tmp_path: Path
) -> None:
    pack, loaded, content = _inventory_pack(release, tmp_path)
    opened = open_request(pack, tmp_path / "opened")
    github = _reviewed_inventory(loaded, content)
    github.files[(REPO, COMMIT, INVENTORY_PATH)] = content + b" "

    refusal = admit(opened, NODE, github, GRANTS)

    assert refusal is not None and refusal.reason == "inventory_unreviewed"


def test_a_reader_that_cannot_read_approvals_holds_the_request(
    release: Release, tmp_path: Path
) -> None:
    pack, loaded, content = _inventory_pack(release, tmp_path)
    opened = open_request(pack, tmp_path / "opened")
    plain = FakeGitHub()
    plain.files = {
        (REPO, COMMIT, "release.yaml"): loaded.manifest_path.read_bytes(),
        (REPO, COMMIT, INVENTORY_PATH): content,
    }
    plain.merged.add(COMMIT)

    refusal = admit(opened, NODE, plain, GRANTS)

    assert refusal is not None
    assert (refusal.status, refusal.reason) == ("held", "review_unreadable")


def test_the_manifests_mode_decides_whether_an_inventory_is_needed(
    release: Release, tmp_path: Path
) -> None:
    """Keyed on the manifest, not on what the request happens to carry."""
    pack, loaded, content = _inventory_pack(release, tmp_path)
    opened = open_request(pack, tmp_path / "opened")
    github = _reviewed_inventory(loaded, content)

    without = replace(opened, inventory_bytes=None)
    refusal = admit(without, NODE, github, GRANTS)
    assert refusal is not None and refusal.reason == "request_invalid"

    plain_pack, plain = _pack_plain(release, tmp_path)
    plain_opened = open_request(plain_pack, tmp_path / "opened-plain")
    carrying = replace(plain_opened, inventory_bytes=content)
    refusal = admit(carrying, NODE, _plain_github(plain), GRANTS)
    assert refusal is not None and refusal.reason == "request_invalid"


def _pack_plain(release: Release, tmp_path: Path) -> tuple[Path, LoadedManifest]:
    built = release({"name": "pomar"})
    request = SigningRequest(
        pack_version=1,
        product="pomar",
        repo=REPO,
        commit=COMMIT,
        manifest_path="release.yaml",
    )
    out = tmp_path / "plain-mode.tar"
    create_request(built, request, out)
    return out, built


def _plain_github(built: LoadedManifest) -> FakeGitHub:
    github = FakeGitHub()
    github.files[(REPO, COMMIT, "release.yaml")] = built.manifest_path.read_bytes()
    github.merged.add(COMMIT)
    return github


def test_a_plain_request_with_a_plain_manifest_is_unaffected(
    release: Release, tmp_path: Path
) -> None:
    built = release({"name": "pomar"})
    request = SigningRequest(
        pack_version=1,
        product="pomar",
        repo=REPO,
        commit=COMMIT,
        manifest_path="release.yaml",
    )
    out = tmp_path / "plain.tar"
    create_request(built, request, out)
    opened = open_request(out, tmp_path / "opened-plain")
    github = FakeGitHub()
    github.files[(REPO, COMMIT, "release.yaml")] = built.manifest_path.read_bytes()
    github.merged.add(COMMIT)

    assert admit(opened, NODE, github, GRANTS) is None


def test_the_inventory_is_named_in_the_receipts_the_response_and_the_journal(
    release: Release, tmp_path: Path
) -> None:
    pack, loaded, content = _inventory_pack(release, tmp_path)
    clock = [datetime(2026, 10, 24, 9, 0, tzinfo=UTC)]
    service = SigningService(
        tmp_path / "state",
        FakeToolchain(),
        _reviewed_inventory(loaded, content),
        HOST,
        allowlist=GRANTS,
        now=lambda: clock[0],
    )
    try:
        reply = service.handle(NODE, pack.read_bytes())
    finally:
        service.close()

    digest = hashlib.sha256(content).hexdigest()
    assert reply.status is HTTPStatus.OK
    assert reply.inventory_sha256 == digest
    with tarfile.open(fileobj=io.BytesIO(reply.body)) as tar:
        member = tar.extractfile("receipts/sign-pomar.json")
        assert member is not None
        receipt = json.loads(member.read())
        assert receipt["inventory_sha256"] == digest
        assert receipt["inventory_version"] == VERSION
    journal = (tmp_path / "state" / "requests.jsonl").read_text().splitlines()
    assert json.loads(journal[-1])["inventory_sha256"] == digest


def test_a_refused_inventory_request_names_the_inventory_in_its_answer(
    release: Release, tmp_path: Path
) -> None:
    pack, loaded, content = _inventory_pack(release, tmp_path)
    clock = [datetime(2026, 10, 24, 9, 0, tzinfo=UTC)]
    service = SigningService(
        tmp_path / "state",
        FakeToolchain(),
        _reviewed_inventory(loaded, content, statuses=[]),
        HOST,
        allowlist=GRANTS,
        now=lambda: clock[0],
    )
    try:
        reply = service.handle(NODE, pack.read_bytes())
    finally:
        service.close()

    body = json.loads(reply.body)
    assert body["outcome"] == "inventory_unreviewed"
    assert body["inventory_sha256"] == hashlib.sha256(content).hexdigest()


def _statuses(*entries: tuple[str, str, object]) -> Completed:
    body = [
        {"context": context, "state": state, "creator": {"id": creator}}
        for context, state, creator in entries
    ]
    return Completed(0, json.dumps(body).encode(), b"")


def test_gh_reads_the_approval_statuses_newest_first_for_exactly_the_context() -> None:
    gh = GhCli(
        Runner(
            _statuses(
                ("ci/other", "success", 1),
                ("council/approve", "failure", APPROVER_USER_ID),
                ("council/approve-shadow", "success", APPROVER_USER_ID),
                ("council/approve", "success", APPROVER_USER_ID),
                ("council/approve", "success", "341600801"),
            ),
            Completed(1, b"", b"gh: Not Found (HTTP 404)"),
            _statuses(*[("council/approve", "pending", 1)] * 99),
        )
    )
    context = "council/approve"
    assert gh.approval_statuses(REPO, COMMIT, context) == [
        Status("failure", APPROVER_USER_ID),
        Status("success", APPROVER_USER_ID),
        Status("success", None),  # an id sent as a string matches nobody
    ]
    assert gh.approval_statuses(REPO, COMMIT, context) == []
    assert len(gh.approval_statuses(REPO, COMMIT, context)) == 99


@pytest.mark.parametrize(
    "answer",
    [
        Completed(0, b"not json", b""),
        Completed(0, b"{}", b""),
        Completed(0, json.dumps([{}] * 100).encode(), b""),
        Completed(1, b"", b"HTTP 500"),
        Completed(0, b"[" + b" " * (9 * 1024 * 1024) + b"]", b""),
    ],
    ids=["not json", "not a list", "a full page", "server error", "answer too large"],
)
def test_gh_approvals_it_cannot_read_are_unreadable(answer: Completed) -> None:
    with pytest.raises(ReviewUnreadableError):
        GhCli(Runner(answer)).approval_statuses(REPO, COMMIT, "council/approve")


def _file(data: bytes, **changes: object) -> Completed:
    body: dict[str, object] = {
        "type": "file",
        "encoding": "base64",
        "size": len(data),
        "content": base64.b64encode(data).decode(),
        **changes,
    }
    return Completed(0, json.dumps(body).encode(), b"")


def test_gh_reads_a_small_file_from_the_json_form() -> None:
    runner = Runner(
        _file(b"manifest bytes"), Completed(1, b"", b"gh: Not Found (HTTP 404)")
    )
    gh = GhCli(runner)
    assert gh.file_at(REPO, COMMIT, "release.yaml") == b"manifest bytes"
    assert gh.file_at(REPO, COMMIT, "absent.yaml") is None
    assert runner.calls[0][2].endswith(f"contents/release.yaml?ref={COMMIT}")


@pytest.mark.parametrize(
    "answer",
    [
        _file(b"x" * (2 * 1024 * 1024)),
        _file(b"x", size=5),
        _file(b"x", encoding="none"),
        _file(b"x", type="dir"),
        _file(b"x", content=3),
        Completed(0, b"[]", b""),
    ],
    ids=[
        "over 1 MiB",
        "size disagrees",
        "not base64",
        "a directory",
        "no content",
        "a list",
    ],
)
def test_gh_files_it_cannot_read_whole_and_small_are_unreadable(
    answer: Completed,
) -> None:
    with pytest.raises(ReviewUnreadableError):
        GhCli(Runner(answer)).file_at(REPO, COMMIT, "inventory.json")


MERGE_COMMIT = "e" * 40  # the merger's own merge commit: never the approved head


def _pull(
    number: int = 12,
    head: str = COMMIT,
    base: str = "main",
    merged: object = "2026-10-24",
) -> dict[str, object]:
    """A pull request as GitHub lists it for a commit: head, base, merge commit."""
    return {
        "number": number,
        "head": {"sha": head},
        "base": {"ref": base},
        "merge_commit_sha": MERGE_COMMIT,
        "merged_at": merged,
    }


def _files(count: int, start: int = 0) -> Completed:
    body = [
        {"filename": f"f{start + n}", "status": "added", "sha": "a" * 40}
        for n in range(count)
    ]
    return Completed(0, json.dumps(body).encode(), b"")


def test_gh_reads_the_files_of_the_pull_request_whose_head_was_approved() -> None:
    runner = Runner(
        Completed(0, json.dumps([_pull(11, "d" * 40), _pull()]).encode(), b""),
        _files(100),
        _files(3, 100),
    )
    files = GhCli(runner).merged_pr_files(REPO, COMMIT, "main")

    assert files is not None and len(files) == 103
    assert files[0] == ChangedFile("f0", "added", "a" * 40)
    assert runner.calls[0][2].endswith(f"commits/{COMMIT}/pulls?per_page=100")
    assert runner.calls[1][2].endswith("pulls/12/files?per_page=100&page=1")
    assert runner.calls[2][2].endswith("pulls/12/files?per_page=100&page=2")


@pytest.mark.parametrize(
    "pulls",
    [
        [],
        [_pull(head="d" * 40)],
        [_pull(merged=None)],
        [_pull(base="other")],
        [_pull(1), _pull(2)],
    ],
    ids=["none", "another head", "never merged", "another base", "two claim it"],
)
def test_gh_finds_no_pull_request_unless_exactly_one_merged_with_that_head(
    pulls: list[dict[str, object]],
) -> None:
    runner = Runner(Completed(0, json.dumps(pulls).encode(), b""))
    assert GhCli(runner).merged_pr_files(REPO, COMMIT, "main") is None


def test_the_merge_commit_is_not_the_commit_that_was_approved() -> None:
    """Asking for the merge commit's pull request finds nothing: the approval
    and the pull request's head are one commit, the merge commit is another."""
    pulls = Completed(0, json.dumps([_pull()]).encode(), b"")
    assert GhCli(Runner(pulls)).merged_pr_files(REPO, MERGE_COMMIT, "main") is None


def test_gh_holds_a_pull_request_it_cannot_read_in_full() -> None:
    pulls = Completed(0, json.dumps([_pull()]).encode(), b"")
    too_many = Runner(pulls, *[_files(100, n * 100) for n in range(30)])
    with pytest.raises(ReviewUnreadableError):
        GhCli(too_many).merged_pr_files(REPO, COMMIT, "main")
    odd = Runner(pulls, Completed(0, b'[{"filename": 3}]', b""))
    with pytest.raises(ReviewUnreadableError):
        GhCli(odd).merged_pr_files(REPO, COMMIT, "main")


def test_a_real_merge_is_reviewed_through_the_real_github_call_shapes() -> None:
    """The head is approved, the merger's merge commit M is another commit.

    Every read goes through GhCli's real endpoints and answers; nothing is
    faked above them. The approval and the pull request are read on the head,
    never on M, and M is only ever the pull request's merge_commit_sha.
    """
    inventory = b"the reviewed inventory"
    path = inventory_path(VERSION)
    runner = Runner(
        Completed(0, b'{"default_branch": "main"}', b""),
        Completed(0, b'{"status": "behind"}', b""),
        _file(inventory),
        Completed(0, json.dumps([_pull()]).encode(), b""),
        Completed(
            0,
            json.dumps(
                [
                    {
                        "filename": path,
                        "status": "added",
                        "sha": git_blob_sha(inventory),
                    }
                ]
            ).encode(),
            b"",
        ),
        _statuses(("council/approve", "success", APPROVER_USER_ID)),
    )

    reason = check_review(
        GhCli(runner),
        repo=REPO,
        commit=COMMIT,
        path=path,
        version=VERSION,
        content=inventory,
    )

    assert reason is None
    read = " ".join(call[2] for call in runner.calls)
    assert MERGE_COMMIT not in read
    assert f"commits/{COMMIT}/pulls" in read
    assert f"commits/{COMMIT}/statuses" in read
    assert f"compare/main...{COMMIT}" in read


@pytest.mark.parametrize("status", ["ahead", "diverged"])
def test_a_commit_ahead_of_or_diverged_from_the_default_branch_is_not_merged(
    status: str,
) -> None:
    runner = Runner(Completed(0, json.dumps({"status": status}).encode(), b""))
    assert GhCli(runner).is_ancestor(REPO, COMMIT, "main") is False


# -- the release command ---------------------------------------------------------


def _run(
    command: Callable[[list[str], bytes], tuple[int, dict[str, Any]]],
    args: list[str],
    request: dict[str, str],
) -> tuple[int, dict[str, Any]]:
    return command(args, json.dumps(request).encode())


def test_the_command_signs_an_inventory_manifest_with_its_inventory_file(
    release: Release, tmp_path: Path
) -> None:
    pinned = release({"name": "pomar"})
    document, pins = _convert(pinned)
    manifest = _written(pinned, document)
    inventory = tmp_path / "inventory.json"
    inventory.write_bytes(_inventory("pomar", pins))
    tools = FakeToolchain()  # one instance: it remembers what it signed
    command = make_command(lambda: tools, {"ZEOCORE_SIGNING_HOST": HOST})
    request = {
        "manifest": str(manifest),
        "inventory": str(inventory),
        "work_dir": str(tmp_path / "work"),
    }

    status, answer = _run(command, ["sign"], request)
    assert (status, answer["ok"], answer["outcome"]) == (0, True, "signed")
    status, answer = _run(command, ["verify"], request)
    assert (status, answer["outcome"]) == (0, "verified")


def test_the_command_refuses_an_inventory_manifest_with_no_inventory_file(
    release: Release, tmp_path: Path
) -> None:
    pinned = release({"name": "pomar"})
    document, _ = _convert(pinned)
    command = make_command(FakeToolchain, {"ZEOCORE_SIGNING_HOST": HOST})
    request = {
        "manifest": str(_written(pinned, document)),
        "work_dir": str(tmp_path / "work"),
    }

    status, answer = _run(command, ["sign"], request)

    assert (status, answer["outcome"]) == (2, "invalid_request")
    assert "none was given" in answer["message"]


def test_the_command_refuses_an_unreadable_inventory_file(
    release: Release, tmp_path: Path
) -> None:
    pinned = release({"name": "pomar"})
    document, _ = _convert(pinned)
    command = make_command(FakeToolchain, {"ZEOCORE_SIGNING_HOST": HOST})
    request = {
        "manifest": str(_written(pinned, document)),
        "inventory": str(tmp_path / "absent.json"),
        "work_dir": str(tmp_path / "work"),
    }

    status, answer = _run(command, ["sign"], request)

    assert (status, answer["outcome"]) == (2, "invalid_request")
    assert "can't be read" in answer["message"]


def test_the_command_names_its_optional_fields_when_the_request_is_wrong() -> None:
    status, answer = make_command(FakeToolchain, {})(
        ["pack"], json.dumps({"manifest": "x"}).encode()
    )
    assert (status, answer["outcome"]) == (2, "invalid_request")
    assert "optionally inventory, inventory_path, version" in answer["message"]


def test_the_command_packs_an_inventory_request(
    release: Release, tmp_path: Path
) -> None:
    pinned = release({"name": "pomar"})
    document, pins = _convert(pinned)
    inventory = tmp_path / "inventory.json"
    inventory.write_bytes(_inventory("pomar", pins))
    out = tmp_path / "request.tar"
    command = make_command(FakeToolchain, {})

    status, answer = _run(
        command,
        ["pack"],
        {
            "manifest": str(_written(pinned, document)),
            "inventory": str(inventory),
            "inventory_path": INVENTORY_PATH,
            "version": VERSION,
            "repo": REPO,
            "commit": COMMIT,
            "manifest_path": "release.yaml",
            "out": str(out),
        },
    )

    assert (status, answer["outcome"]) == (0, "packed")
    assert _members(out)["inventory.json"] == inventory.read_bytes()
