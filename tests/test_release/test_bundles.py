"""Bundle-aware internal signing against ZEOCORE-SOW-32 rev b, behind fixtures."""

from __future__ import annotations

import functools
import io
import json
import os
import plistlib
import stat
import tarfile
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from zeo_core.cli.release import make_command
from zeo_core.release.signing import (
    LoadedManifest,
    ManifestError,
    PackError,
    SigningJournal,
    SigningRequest,
    WorkDir,
    create_request,
    create_response,
    load,
    open_request,
    open_response,
    sign,
    verify,
)
from zeo_core.release.signing.admission import Grant, Limits, admit, check_allowlist
from zeo_core.release.signing.tree import (
    TreeError,
    contained_target,
    listing,
    tree_sha256,
)

from .conftest import (
    APP,
    BUNDLE_ID,
    HOST,
    MACHO,
    MAIN,
    NESTED,
    RENDERER,
    SWIFTTERM,
    ZEOAPP_NESTED,
    FakeToolchain,
    signed_as,
)

App = Callable[..., LoadedManifest]
_AUDIO = "Contents/Frameworks/Audio.framework"
INSIDE_OUT = [
    "ac.zeo.zeoapp.libinner",
    "ac.zeo.zeoapp.audio",
    "ac.zeo.zeoapp.console-renderer",
    "ac.zeo.zeoapp.llama-completion",
    "ac.zeo.zeoapp.swiftterm-resources",
    BUNDLE_ID,
]


@pytest.fixture
def zeoapp(app: Callable[..., LoadedManifest]) -> Callable[..., LoadedManifest]:
    """zeoapp's real shape: a main executable and one renderer."""
    return app


@pytest.fixture
def framework_app(
    app: Callable[..., LoadedManifest],
) -> Callable[..., LoadedManifest]:
    """The shape with a helper and a nested framework, built on zeoapp's."""
    return functools.partial(app, framework=True)


NODE = "zeoapp-build.tail29b7b0.ts.net"
REPO = "zeroemployeeorg/zeoapp"
COMMIT = "d" * 40


@pytest.fixture
def work(tmp_path: Path) -> WorkDir:
    return WorkDir(tmp_path / "work")


def _bundle(loaded: LoadedManifest) -> Any:  # noqa: ANN401
    return loaded.signing.bundles[0]


# --- the manifest -----------------------------------------------------------


def _entry(**changes: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "name": "zeoapp",
        "path": APP,
        "bundle_identifier": BUNDLE_ID,
        "identifier": BUNDLE_ID,
        "audience": "internal",
        "platform": "darwin",
        "entitlements": "none",
        "unsigned_tree_sha256": "0" * 64,
        "main_executable": "none",
        "nested": [
            {
                "path": "Contents/Helpers/llama-completion",
                "identifier": "ac.zeo.zeoapp.llama-completion",
                "entitlements": "none",
                "kind": "macho",
                "unsigned_sha256": "0" * 64,
            }
        ],
    }
    entry.update(changes)
    return entry


def _signing(*bundles: dict[str, object], **changes: object) -> dict[str, object]:
    document: dict[str, object] = {
        "release_manifest": 2,
        "product": "zeoapp",
        "bundles": list(bundles),
    }
    document.update(changes)
    return {"signing": document}


def _write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "release.yaml"
    path.write_text(yaml.safe_dump(document))
    return path


def test_a_bundle_keeps_its_own_bundle_identifier(tmp_path: Path) -> None:
    loaded = load(_write(tmp_path, _signing(_entry())))
    bundle = _bundle(loaded)
    assert bundle.identifier == bundle.bundle_identifier == BUNDLE_ID
    assert bundle.nested[0].name == "llama-completion"
    assert loaded.signing.executables == ()


_NESTED_PATH = "Contents/Helpers/llama-completion"
_EXECUTABLE = {
    "name": "llama-completion",
    "path": "bin/x",
    "unsigned_sha256": "0" * 64,
    "identifier": "ac.zeo.zeoapp.llama-completion",
    "audience": "internal",
    "platform": "darwin",
    "entitlements": "none",
}


@pytest.mark.parametrize(
    "document",
    [
        # The outer identifier is the bundle identifier, and nothing else.
        _signing(_entry(identifier="ac.zeo.zeoapp.zeoapp")),
        _signing(_entry(audience="distributed")),
        _signing(_entry(platform="linux")),
        _signing(_entry(policy="legacy-runtime-installed")),
        _signing(_entry(path="app/build/Zeo")),
        _signing(_entry(path="../Zeo.app")),
        _signing(_entry(signer="mine")),
        # Nested code keeps ac.zeo.<product>.<name>.
        _signing(
            _entry(
                nested=[
                    {
                        "path": _NESTED_PATH,
                        "identifier": "ac.zeo.pomar.llama",
                        "entitlements": "none",
                        "kind": "macho",
                        "unsigned_sha256": "0" * 64,
                    }
                ]
            )
        ),
        _signing(
            _entry(
                nested=[
                    {
                        "path": _NESTED_PATH,
                        "identifier": "ac.zeo.zeoapp.a.b",
                        "entitlements": "none",
                        "kind": "macho",
                        "unsigned_sha256": "0" * 64,
                    }
                ]
            )
        ),
        _signing(
            _entry(
                nested=[
                    {
                        "path": p,
                        "identifier": f"ac.zeo.zeoapp.{n}",
                        "entitlements": "none",
                        "kind": "macho",
                        "unsigned_sha256": "0" * 64,
                    }
                    for p, n in ((_NESTED_PATH, "one"), (_NESTED_PATH, "two"))
                ]
            )
        ),
        _signing(
            _entry(
                nested=[
                    {
                        "path": p,
                        "identifier": "ac.zeo.zeoapp.one",
                        "entitlements": "none",
                        "kind": "macho",
                        "unsigned_sha256": "0" * 64,
                    }
                    for p in ("Contents/a", "Contents/b")
                ]
            )
        ),
        _signing(
            _entry(
                nested=[
                    {
                        "path": "../escape",
                        "identifier": "ac.zeo.zeoapp.one",
                        "entitlements": "none",
                        "kind": "macho",
                        "unsigned_sha256": "0" * 64,
                    }
                ]
            )
        ),
        _signing(
            _entry(
                nested=[
                    {
                        "path": "./Contents",
                        "identifier": "ac.zeo.zeoapp.one",
                        "entitlements": "none",
                        "kind": "macho",
                        "unsigned_sha256": "0" * 64,
                    }
                ]
            )
        ),
        # A name or identifier is one binding's, across executables and bundles.
        _signing(_entry(), executables=[_EXECUTABLE]),
        _signing(
            _entry(),
            executables=[
                {
                    **_EXECUTABLE,
                    "name": "x",
                    "identifier": "ac.zeo.zeoapp.x",
                    "path": f"{APP}/x",
                }
            ],
        ),
        _signing(_entry(), _entry(name="other", path="other/Zeo.app", nested=[])),
        _signing(
            _entry(),
            _entry(name="other", bundle_identifier="o.x", identifier="o.x", nested=[]),
        ),
        # Never another product's namespace, even as its own bundle identifier.
        _signing(
            _entry(
                bundle_identifier="ac.zeo.pomar.host", identifier="ac.zeo.pomar.host"
            )
        ),
        # Version 1 is unchanged: executables only.
        _signing(_entry(), release_manifest=1),
        _signing(),
    ],
)
def test_a_bad_bundle_entry_is_refused(tmp_path: Path, document: object) -> None:
    with pytest.raises(ManifestError):
        load(_write(tmp_path, document))


# --- the canonical tree -----------------------------------------------------


def test_the_tree_digest_is_canonical(tmp_path: Path) -> None:
    root = tmp_path / "A.framework"
    (root / "Versions" / "A").mkdir(parents=True)
    (root / "Versions" / "A" / "a").write_bytes(b"a")
    (root / "Versions" / "Current").symlink_to("A")
    entries = listing(root)
    assert [entry.line() for entry in entries] == [
        "Versions\tdir\t755\t-\n",
        "Versions/A\tdir\t755\t-\n",
        "Versions/A/a\tfile\t644\t"
        "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb\n",
        "Versions/Current\tsymlink\t-\tA\n",
    ]
    first = tree_sha256(root)
    (root / "Versions" / "A" / "a").chmod(0o700)
    assert tree_sha256(root) != first
    (root / "Versions" / "A" / "a").chmod(0o640)
    assert tree_sha256(root) == first  # only the owner's execute bit counts
    (root / "Versions" / "empty").mkdir()
    assert tree_sha256(root) != first


@pytest.mark.parametrize(
    ("link", "target"),
    [
        ("Contents/x", "/etc/passwd"),
        ("Contents/x", "../../outside"),
        ("Contents/x", "missing"),
    ],
)
def test_a_link_must_stay_inside_and_resolve(
    tmp_path: Path, link: str, target: str
) -> None:
    root = tmp_path / "Zeo.app"
    (root / "Contents").mkdir(parents=True)
    (root / link).symlink_to(target)
    with pytest.raises(TreeError):
        listing(root)


def test_a_link_through_a_link_cant_climb_out(tmp_path: Path) -> None:
    root = tmp_path / "Zeo.app"
    root.mkdir()
    (root / "here").symlink_to(".")
    (root / "out").symlink_to("here/..")  # lexically inside, really the parent
    with pytest.raises(TreeError):
        listing(root)


def test_only_files_directories_and_links(tmp_path: Path) -> None:
    root = tmp_path / "Zeo.app"
    root.mkdir()
    os.mkfifo(root / "pipe")
    with pytest.raises(TreeError):
        listing(root)
    with pytest.raises(TreeError):
        listing(tmp_path / "missing.app")
    (tmp_path / "file.app").write_bytes(b"")
    with pytest.raises(TreeError):
        listing(tmp_path / "file.app")


def _framework(tmp_path: Path) -> Path:
    """A minimal framework inside an app, with its two usual links."""
    root = tmp_path / "Zeo.app"
    framework = root / "Contents" / "Frameworks" / "A.framework"
    (framework / "Versions" / "A" / "Resources").mkdir(parents=True)
    (framework / "Versions" / "A" / "A").write_bytes(MACHO)
    (framework / "Versions" / "Current").symlink_to("A")
    (framework / "A").symlink_to("Versions/Current/A")
    return root


def test_a_frameworks_own_links_are_allowed(tmp_path: Path) -> None:
    root = _framework(tmp_path)
    links = [entry.path for entry in listing(root) if entry.type == "symlink"]
    assert links == [
        "Contents/Frameworks/A.framework/A",
        "Contents/Frameworks/A.framework/Versions/Current",
    ]
    # A framework's own subtree digest, as a nested item's, sees the same.
    assert len(listing(root / "Contents/Frameworks/A.framework")) == 6


_FW = "Contents/Frameworks/A.framework"


@pytest.mark.parametrize(
    ("link", "target"),
    [
        # Only a framework's Versions/Current and its top-level links.
        ("Contents/Resources/x", "../Info.plist"),
        (f"{_FW}/Versions/A/Resources/x", "../A"),
        (f"{_FW}/Versions/Other", "A"),
        # Versions/Current names one version beside it.
        (f"{_FW}/Versions/Current", "A/Resources"),
        (f"{_FW}/Versions/Current", "."),
        # A top-level link names something under Versions/.
        (f"{_FW}/B", "Versions"),
        (f"{_FW}/B", "../A.framework/Versions/A/A"),
        # Every hop stays inside the framework.
        (f"{_FW}/B", "Versions/Current/../../.."),
        (f"{_FW}/B", "Versions/../../../Info.plist"),
        (f"{_FW}/B", "Versions/Current/missing"),
    ],
)
def test_a_link_out_of_place_or_out_of_its_framework_is_refused(
    tmp_path: Path, link: str, target: str
) -> None:
    root = _framework(tmp_path)
    (root / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
    (root / "Contents" / "Info.plist").write_bytes(b"")
    if (root / link).is_symlink():
        (root / link).unlink()
    (root / link).symlink_to(target)
    with pytest.raises(TreeError):
        listing(root)


def test_each_hop_is_resolved_inside_the_framework(tmp_path: Path) -> None:
    root = _framework(tmp_path)
    # Through Versions/Current and back, never above the framework: allowed.
    (root / _FW / "B").symlink_to("Versions/Current/../Current/A")
    assert any(entry.path == f"{_FW}/B" for entry in listing(root))
    # Resolved through Current, "../.." is the framework itself, then above it,
    # though lexically from B it would be inside: refused.
    (root / _FW / "B").unlink()
    (root / _FW / "B").symlink_to("Versions/Current/../../../A.framework/A")
    with pytest.raises(TreeError):
        listing(root)


def test_a_link_loop_is_refused(tmp_path: Path) -> None:
    root = _framework(tmp_path)
    (root / _FW / "Versions" / "Current").unlink()
    (root / _FW / "Versions" / "Current").symlink_to("Current")
    with pytest.raises(TreeError):
        listing(root)


@pytest.mark.parametrize("name", ["tab\there", "line\nfeed", "bell\x07", "del\x7f"])
def test_a_control_character_in_a_path_is_refused(tmp_path: Path, name: str) -> None:
    root = tmp_path / "Zeo.app"
    root.mkdir()
    (root / name).write_bytes(b"")
    with pytest.raises(TreeError):
        listing(root)


def test_a_hard_link_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "Zeo.app"
    root.mkdir()
    (root / "a").write_bytes(b"a")
    os.link(root / "a", root / "b")
    with pytest.raises(TreeError, match="hard link"):
        listing(root)


@pytest.mark.parametrize("bit", [0o4000, 0o2000])
def test_a_setuid_or_setgid_file_is_refused(tmp_path: Path, bit: int) -> None:
    root = tmp_path / "Zeo.app"
    root.mkdir()
    (root / "a").write_bytes(b"a")
    (root / "a").chmod(0o755 | bit)
    if not (root / "a").stat().st_mode & bit:
        pytest.skip("this filesystem doesn't keep the bit")
    with pytest.raises(TreeError, match="setuid"):
        listing(root)


@pytest.mark.parametrize(
    ("link", "target", "inside"),
    [
        ("A.framework/Current", "A", True),
        ("A.framework/Versions/Current/x", "../../y", True),
        ("A.framework/x", "../../y", False),
        ("x", "", False),
        ("x", "/y", False),
    ],
)
def test_contained_target(link: str, target: str, inside: bool) -> None:
    assert contained_target(link, target) is inside


# --- signing inside-out -----------------------------------------------------


def test_signs_inside_out_on_a_copy_and_verify_passes(
    framework_app: App, work: WorkDir
) -> None:
    loaded = framework_app()
    tools = FakeToolchain()

    signed = sign(loaded, work, tools, HOST)

    assert (signed.status, signed.reason) == ("done", "signed")
    assert tools.sign_calls == INSIDE_OUT
    candidate = work.bundle_candidate(_bundle(loaded))
    assert candidate == work.root / "signed" / "zeoapp" / "Zeo.app"
    assert tools.sealed_calls == [candidate]
    *nested, outer = signed.receipts
    assert [receipt["name"] for receipt in nested] == [item["name"] for item in NESTED]
    assert outer["identifier"] == outer["bundle_identifier"] == BUNDLE_ID
    assert outer["signed_tree_sha256"] == tree_sha256(candidate)
    assert outer["unsigned_tree_sha256"] == _bundle(loaded).unsigned_tree_sha256
    assert set(outer["nested_receipts"]) == {item["name"] for item in NESTED}
    framework = next(r for r in nested if r["name"] == "audio")
    assert framework["signed_sha256"] is None and framework["signed_tree_sha256"]
    # The copy keeps the framework's links; the original is untouched.
    assert os.readlink(candidate / "Contents/Frameworks/Audio.framework/Audio") == (
        "Versions/Current/Audio"
    )
    assert tree_sha256(loaded.resolve(APP)) == _bundle(loaded).unsigned_tree_sha256

    checked = verify(loaded, work, tools)
    assert (checked.status, checked.reason) == ("done", "verified")
    assert [r["name"] for r in checked.receipts][-1] == "zeoapp"
    assert {r["step"] for r in checked.receipts} == {"verify"}


def test_signing_again_returns_the_receipts_without_a_second_signature(
    framework_app: App, work: WorkDir
) -> None:
    loaded = framework_app()
    tools = FakeToolchain()
    first = sign(loaded, work, tools, HOST)
    again = sign(loaded, work, tools, HOST)
    assert again.status == "done"
    assert again.receipts == first.receipts
    assert tools.sign_calls == INSIDE_OUT


def _add_code(path: str) -> Callable[[Path], object]:
    def tweak(bundle: Path) -> None:
        (bundle / path).parent.mkdir(parents=True, exist_ok=True)
        (bundle / path).write_bytes(MACHO)

    return tweak


def _add_magic(magic: str) -> Callable[[Path], object]:
    def tweak(bundle: Path) -> None:
        path = bundle / "Contents" / "Resources" / f"data-{magic}"
        path.write_bytes(bytes.fromhex(magic) + b"\x00" * 28)
        path.chmod(0o644)

    return tweak


def _identifier(value: str) -> Callable[[Path], object]:
    def tweak(bundle: Path) -> None:
        (bundle / "Contents" / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": value, "CFBundleExecutable": "Zeo"})
        )

    return tweak


@pytest.mark.parametrize(
    ("build", "reason"),
    [
        # Code is found by its magic number, not its name.
        ({"tweak": _add_code("Contents/Resources/readme.txt")}, "nested_unlisted"),
        ({"tweak": _add_code("Contents/MacOS/second")}, "nested_unlisted"),
        # Every Mach-O magic, thin and universal, either byte order, even
        # in a file without an execute bit.
        *(
            ({"tweak": _add_magic(magic)}, "nested_unlisted")
            for magic in (
                "feedface",
                "cefaedfe",
                "feedfacf",
                "cffaedfe",
                "cafebabe",
                "bebafeca",
                "cafebabf",
                "bfbafeca",
            )
        ),
        # A link in a nested bundle that leaves that bundle, though not the app.
        (
            {
                "tweak": lambda b: (b / _AUDIO / "Up").symlink_to(
                    "Versions/Current/../../../libZEOConsoleRenderer.dylib"
                )
            },
            "bundle_unsafe",
        ),
        (
            {
                "tweak": lambda b: (
                    b / "Contents/Resources/SwiftTerm_SwiftTerm.bundle/x"
                ).symlink_to("Contents/Info.plist")
            },
            "bundle_unsafe",
        ),
        (
            {
                "tweak": lambda b: (b / "Contents/PlugIns/Share.appex").mkdir(
                    parents=True
                )
            },
            "nested_unlisted",
        ),
        ({"nested": NESTED[1:]}, "nested_unlisted"),
        (
            {"nested": (*NESTED, {"path": "Contents/Helpers/gone", "name": "gone"})},
            "nested_missing",
        ),
        # A link is not code of its own, and codesign is never pointed at one.
        (
            {
                "nested": (
                    *NESTED,
                    {"path": "Contents/Frameworks/Audio.framework/Audio", "name": "a"},
                )
            },
            "bundle_unsafe",
        ),
        (
            {
                "nested": (
                    *NESTED,
                    {
                        "path": f"{_AUDIO}/Versions/Current/libinner.dylib",
                        "name": "x",
                    },
                )
            },
            "bundle_unsafe",
        ),
        (
            {"tweak": _identifier("org.zeroemployee.other")},
            "bundle_identifier_mismatch",
        ),
        (
            {"tweak": lambda b: (b / "Contents" / "Info.plist").write_bytes(b"x")},
            "bundle_identifier_mismatch",
        ),
        ({"unsigned_tree_sha256": "0" * 64}, "input_mismatch"),
        (
            {"tweak": lambda b: (b / "Contents" / "out").symlink_to("../../..")},
            "bundle_unsafe",
        ),
    ],
)
def test_a_bundle_is_refused_before_any_signer(
    framework_app: App, work: WorkDir, build: dict[str, Any], reason: str
) -> None:
    tools = FakeToolchain()
    outcome = sign(framework_app(**build), work, tools, HOST)
    assert (outcome.status, outcome.reason) == ("refused", reason)
    assert outcome.executable == "zeoapp"
    assert tools.sign_calls == []
    assert not work.root.exists()


def test_a_tree_changed_after_review_is_refused(
    framework_app: App, work: WorkDir
) -> None:
    loaded = framework_app()
    (loaded.resolve(APP) / "Contents/Resources/model.txt").write_bytes(b"other")
    outcome = sign(loaded, work, FakeToolchain(), HOST)
    assert (outcome.status, outcome.reason) == ("refused", "input_mismatch")


def test_a_changed_entitlements_file_is_refused(
    framework_app: App, work: WorkDir
) -> None:
    loaded = framework_app(entitlements={"com.apple.security.device.audio-input": True})
    (loaded.root / "zeoapp.entitlements").write_bytes(b"changed")
    outcome = sign(loaded, work, FakeToolchain(), HOST)
    assert (outcome.status, outcome.reason) == ("refused", "input_mismatch")


def test_the_bundle_and_nested_items_carry_their_entitlements(
    framework_app: App, work: WorkDir
) -> None:
    granted = {"com.apple.security.device.audio-input": True}
    loaded = framework_app(entitlements=granted)
    tools = FakeToolchain()
    assert sign(loaded, work, tools, HOST).status == "done"
    candidate = work.bundle_candidate(_bundle(loaded))
    assert tools.inspections[candidate].entitlements == granted

    # Dropped from the outer bundle, they are refused.
    tools.inspections[candidate] = replace(
        tools.inspections[candidate], entitlements=None
    )
    outcome = verify(loaded, work, tools)
    assert (outcome.status, outcome.reason) == ("refused", "entitlements_mismatch")


def test_a_failed_nested_signature_closes_the_bundle_so_a_retry_signs_afresh(
    framework_app: App, work: WorkDir
) -> None:
    loaded = framework_app()
    tools = FakeToolchain(fail_on="ac.zeo.zeoapp.console-renderer")
    failed = sign(loaded, work, tools, HOST)
    assert (failed.status, failed.reason) == ("held", "signing_failed_unknown")
    assert failed.executable == "console-renderer"
    assert tools.sign_calls == INSIDE_OUT[:3]

    retry = FakeToolchain()
    again = sign(loaded, work, retry, HOST)
    assert again.status == "done"
    assert retry.sign_calls == INSIDE_OUT
    assert verify(loaded, work, retry).status == "done"


def test_a_bundle_that_isnt_sealed_is_refused(
    framework_app: App, work: WorkDir
) -> None:
    loaded = framework_app()
    tools = FakeToolchain(sealed_ok=False)
    outcome = sign(loaded, work, tools, HOST)
    assert (outcome.status, outcome.reason) == ("refused", "not_signed")
    assert work.read_receipt("zeoapp") is None
    # Every part was closed as failed: a later run signs afresh.
    assert sign(loaded, work, FakeToolchain(), HOST).status == "done"


def test_an_unreadable_bundle_signature_is_held(
    framework_app: App, work: WorkDir
) -> None:
    outcome = sign(framework_app(), work, FakeToolchain(sealed_fails=True), HOST)
    assert (outcome.status, outcome.reason) == ("held", "signing_failed_unknown")


def test_a_nested_item_that_isnt_its_binding_is_refused(
    framework_app: App, work: WorkDir
) -> None:
    tools = FakeToolchain(
        after_sign=lambda identifier, _: signed_as(
            "ac.zeo.zeoapp.other"
            if identifier.endswith("console-renderer")
            else identifier
        )
    )
    outcome = sign(framework_app(), work, tools, HOST)
    assert (outcome.status, outcome.reason) == ("refused", "identity_mismatch")
    assert outcome.executable == "console-renderer"


def test_a_lost_answer_in_a_bundle_is_ambiguous_and_never_signed_again(
    framework_app: App, work: WorkDir
) -> None:
    loaded = framework_app()
    lost = sign(loaded, work, FakeToolchain(lose_answer=True), HOST)
    assert (lost.status, lost.reason) == ("ambiguous", "signing_attempt_unreconciled")
    tools = FakeToolchain()
    again = sign(loaded, work, tools, HOST)
    assert (again.status, again.reason) == ("ambiguous", "signing_attempt_unreconciled")
    assert tools.sign_calls == []


def test_a_lost_answer_whose_bundle_verifies_is_reconciled(
    framework_app: App, work: WorkDir
) -> None:
    loaded = framework_app()
    tools = FakeToolchain()
    assert sign(loaded, work, tools, HOST).status == "done"
    # As if the run stopped after the last codesign, before any receipt.
    for name in ("zeoapp", *(item["name"] for item in NESTED)):
        work.receipt_path(name).unlink()
    journal = SigningJournal(work.root / "journal.jsonl")
    journal.append(f"{loaded.sha256}:zeoapp", "intent", datetime.now(UTC))
    journal.close()

    calls = len(tools.sign_calls)
    reconciled = sign(loaded, work, tools, HOST)
    assert reconciled.status == "done"
    assert len(tools.sign_calls) == calls
    assert all(receipt["reconciled"] for receipt in reconciled.receipts)
    assert verify(loaded, work, tools).status == "done"


def test_a_receipt_from_another_manifest_is_refused(
    framework_app: App, work: WorkDir
) -> None:
    loaded = framework_app()
    sign(loaded, work, FakeToolchain(), HOST)
    receipt = json.loads(work.receipt_path("zeoapp").read_text())
    work.receipt_path("zeoapp").write_text(
        json.dumps({**receipt, "manifest_sha256": "0" * 64})
    )
    outcome = sign(loaded, work, FakeToolchain(), HOST)
    assert (outcome.status, outcome.reason) == ("refused", "receipt_mismatch")
    outcome = verify(loaded, work, FakeToolchain())
    assert (outcome.status, outcome.reason) == ("refused", "receipt_mismatch")


# --- verification -----------------------------------------------------------


def _signed(framework_app: App, work: WorkDir) -> tuple[LoadedManifest, FakeToolchain]:
    loaded = framework_app()
    tools = FakeToolchain()
    assert sign(loaded, work, tools, HOST).status == "done"
    return loaded, tools


def _rewrite(work: WorkDir, name: str, **changes: object) -> None:
    receipt = json.loads(work.receipt_path(name).read_text())
    work.receipt_path(name).write_text(json.dumps({**receipt, **changes}))


@pytest.mark.parametrize(
    ("change", "status", "reason"),
    [
        (
            lambda w, c, _: (c / "Contents/Resources/model.txt").write_bytes(b"x"),
            "refused",
            "signed_digest_mismatch",
        ),
        (
            lambda w, c, _: (c / "Contents/Resources/link").symlink_to("/etc"),
            "refused",
            "signed_digest_mismatch",
        ),
        (
            lambda w, c, _: _rewrite(w, "console-renderer", signed_at="later"),
            "refused",
            "receipt_mismatch",
        ),
        (
            lambda w, c, _: _rewrite(w, "console-renderer", signed_sha256="0" * 64),
            "refused",
            "signed_digest_mismatch",
        ),
        (
            lambda w, c, _: _rewrite(w, "audio", signed_tree_sha256="0" * 64),
            "refused",
            "signed_digest_mismatch",
        ),
        (
            lambda w, c, _: _rewrite(w, "zeoapp", manifest_sha256="0" * 64),
            "refused",
            "receipt_mismatch",
        ),
        (
            lambda w, c, _: w.receipt_path("libinner").unlink(),
            "refused",
            "companion_unsigned",
        ),
        (
            lambda w, c, _: w.receipt_path("zeoapp").unlink(),
            "refused",
            "companion_unsigned",
        ),
        (
            lambda w, c, t: setattr(t, "sealed_ok", False),
            "refused",
            "not_signed",
        ),
        (
            lambda w, c, t: setattr(t, "sealed_fails", True),
            "held",
            "verify_unreadable",
        ),
        (
            lambda w, c, t: t.inspections.update(
                {
                    c / NESTED[1]["path"]: signed_as("ac.zeo.zeoapp.other"),
                }
            ),
            "refused",
            "identity_mismatch",
        ),
        (
            lambda w, c, t: t.inspections.update(
                {c: signed_as("ac.zeo.zeoapp.zeoapp")}
            ),
            "refused",
            "identity_mismatch",
        ),
    ],
)
def test_verify_refuses_a_bundle_that_isnt_as_signed(
    framework_app: App,
    work: WorkDir,
    change: Callable[[WorkDir, Path, FakeToolchain], object],
    status: str,
    reason: str,
) -> None:
    loaded, tools = _signed(framework_app, work)
    change(work, work.bundle_candidate(_bundle(loaded)), tools)
    outcome = verify(loaded, work, tools)
    assert (outcome.status, outcome.reason) == (status, reason)


def test_verify_refuses_an_unsigned_bundle(framework_app: App, work: WorkDir) -> None:
    outcome = verify(framework_app(), work, FakeToolchain())
    assert (outcome.status, outcome.reason) == ("refused", "not_signed")


def test_verify_refuses_a_changed_original(framework_app: App, work: WorkDir) -> None:
    loaded, tools = _signed(framework_app, work)
    (loaded.resolve(APP) / "Contents/Resources/model.txt").write_bytes(b"x")
    outcome = verify(loaded, work, tools)
    assert (outcome.status, outcome.reason) == ("refused", "input_mismatch")


def test_verify_refuses_code_added_to_the_signed_copy(
    framework_app: App, work: WorkDir
) -> None:
    loaded, tools = _signed(framework_app, work)
    candidate = work.bundle_candidate(_bundle(loaded))
    (candidate / "Contents/Helpers/extra").write_bytes(MACHO)
    _rewrite(work, "zeoapp", signed_tree_sha256=tree_sha256(candidate))
    outcome = verify(loaded, work, tools)
    assert (outcome.status, outcome.reason) == ("refused", "nested_unlisted")


# --- packs ------------------------------------------------------------------


def _request() -> SigningRequest:
    return SigningRequest(
        pack_version=1,
        product="zeoapp",
        repo=REPO,
        commit=COMMIT,
        manifest_path="release.yaml",
    )


def test_a_bundle_round_trips_through_packs(framework_app: App, tmp_path: Path) -> None:
    built = framework_app()
    request_id = create_request(built, _request(), tmp_path / "request.tar")
    assert create_request(built, _request(), tmp_path / "again.tar") == request_id

    opened = open_request(tmp_path / "request.tar", tmp_path / "matormini" / "in")
    unpacked = opened.manifest.resolve(APP)
    assert tree_sha256(unpacked) == _bundle(built).unsigned_tree_sha256
    assert (
        unpacked / "Contents/Frameworks/Audio.framework/Versions/Current"
    ).is_symlink()

    signer = FakeToolchain()
    signing = WorkDir(tmp_path / "matormini" / "work")
    assert sign(opened.manifest, signing, signer, HOST).status == "done"
    create_response(opened.manifest, signing, tmp_path / "response.tar")

    returned = open_response(built, tmp_path / "response.tar", tmp_path / "eng" / "w")
    checker = FakeToolchain(host_name="zeoapp-build")
    source, target = (w.bundle_candidate(_bundle(built)) for w in (signing, returned))
    for path, seen in signer.inspections.items():
        checker.inspections[target / path.relative_to(source)] = seen
    outcome = verify(built, returned, checker)
    assert (outcome.status, outcome.reason) == ("done", "verified")


def test_a_bundle_that_differs_is_refused_when_packing(
    framework_app: App, tmp_path: Path
) -> None:
    built = framework_app()
    (built.resolve(APP) / "Contents/Resources/model.txt").write_bytes(b"x")
    with pytest.raises(PackError) as caught:
        create_request(built, _request(), tmp_path / "request.tar")
    assert caught.value.reason == "input_mismatch"


def test_an_unsafe_bundle_is_refused_when_packing(
    framework_app: App, tmp_path: Path
) -> None:
    built = framework_app()
    (built.resolve(APP) / "Contents" / "out").symlink_to("/etc")
    with pytest.raises(PackError) as caught:
        create_request(built, _request(), tmp_path / "request.tar")
    assert caught.value.reason == "input_mismatch"


def _members(pack: Path) -> list[tuple[tarfile.TarInfo, bytes]]:
    with tarfile.open(pack) as tar:
        found = []
        for info in tar.getmembers():
            handle = tar.extractfile(info) if info.isreg() else None
            found.append((info, handle.read() if handle else b""))
    return found


def _repack(
    source: Path,
    out: Path,
    change: Callable[[list[tuple[tarfile.TarInfo, bytes]]], object],
) -> None:
    members = _members(source)
    change(members)
    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data))


def _link(name: str, target: str) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = tarfile.SYMTYPE, target
    return info, b""


def _file(name: str, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


_FRAMEWORK = f"{APP}/Contents/Frameworks/Audio.framework"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        # A link outside any bundle, or one that leaves the pack.
        (lambda m: m.append(_link("loose", "release.yaml")), "input_mismatch"),
        (lambda m: m.append(_link(f"{APP}/Contents/x", "/etc")), "request_invalid"),
        (lambda m: m.append(_link(f"{APP}/x", "../../../../y")), "request_invalid"),
        # A link that leaves its bundle but not the pack.
        (
            lambda m: m.append(_link(f"{APP}/x", "../../../release.yaml")),
            "input_mismatch",
        ),
        # Nothing is written through a link.
        (
            lambda m: m.append(_file(f"{_FRAMEWORK}/Versions/Current/evil", b"x")),
            "request_invalid",
        ),
        # A changed, added or missing file in the bundle.
        (lambda m: m.append(_file(f"{APP}/Contents/extra", b"x")), "input_mismatch"),
        (
            lambda m: m.remove(
                next(p for p in m if p[0].name == f"{APP}/Contents/Resources/model.txt")
            ),
            "input_mismatch",
        ),
        (lambda m: m.append(m[-1]), "request_invalid"),
    ],
)
def test_a_tampered_bundle_pack_is_refused(
    framework_app: App,
    tmp_path: Path,
    change: Callable[[list[tuple[tarfile.TarInfo, bytes]]], object],
    reason: str,
) -> None:
    create_request(framework_app(), _request(), tmp_path / "request.tar")
    _repack(tmp_path / "request.tar", tmp_path / "bad.tar", change)
    with pytest.raises(PackError) as caught:
        open_request(tmp_path / "bad.tar", tmp_path / "in")
    assert caught.value.reason == reason


def test_a_response_with_something_else_is_refused(
    framework_app: App, tmp_path: Path
) -> None:
    built = framework_app()
    work = WorkDir(tmp_path / "work")
    assert sign(built, work, FakeToolchain(), HOST).status == "done"
    create_response(built, work, tmp_path / "response.tar")
    for n, change in enumerate(
        (
            lambda m: m.append(_file("signed/zeoapp/extra", b"x")),
            lambda m: m.append(_link("signed/loose", "zeoapp")),
            lambda m: m.remove(
                next(p for p in m if p[0].name == "receipts/sign-libinner.json")
            ),
        )
    ):
        _repack(tmp_path / "response.tar", tmp_path / f"bad{n}.tar", change)
        with pytest.raises(PackError):
            open_response(built, tmp_path / f"bad{n}.tar", tmp_path / f"in{n}")


def test_a_response_needs_every_receipt(framework_app: App, tmp_path: Path) -> None:
    built = framework_app()
    work = WorkDir(tmp_path / "work")
    with pytest.raises(PackError):
        create_response(built, work, tmp_path / "response.tar")
    assert sign(built, work, FakeToolchain(), HOST).status == "done"
    work.receipt_path("audio").unlink()
    with pytest.raises(PackError):
        create_response(built, work, tmp_path / "response.tar")


# --- admission --------------------------------------------------------------


class _Reviewed:
    def __init__(self, manifest: bytes) -> None:
        self.manifest = manifest

    def default_branch(self, repo: str) -> str:
        return "main"

    def file_at(self, repo: str, commit: str, path: str) -> bytes | None:
        return self.manifest

    def is_ancestor(self, repo: str, commit: str, branch: str) -> bool:
        return True


def _admit(
    framework_app: App,
    tmp_path: Path,
    identifiers: set[str],
    limits: Limits | None = None,
) -> str | None:
    built = framework_app()
    create_request(built, _request(), tmp_path / "request.tar")
    opened = open_request(tmp_path / "request.tar", tmp_path / "in")
    grants = {
        NODE: {
            "zeoapp": Grant(
                REPO,
                frozenset(identifiers - {BUNDLE_ID}),
                frozenset(identifiers & {BUNDLE_ID}),
            )
        }
    }
    refusal = admit(
        opened, NODE, _Reviewed(opened.manifest_bytes), grants, limits or Limits()
    )
    return None if refusal is None else refusal.reason


DECLARED = {BUNDLE_ID, *(f"ac.zeo.zeoapp.{item['name']}" for item in NESTED)}


def test_a_bundle_is_admitted_with_every_identifier_declared(
    framework_app: App, tmp_path: Path
) -> None:
    assert _admit(framework_app, tmp_path, DECLARED) is None


def test_an_undeclared_nested_identifier_is_refused(
    framework_app: App, tmp_path: Path
) -> None:
    reason = _admit(framework_app, tmp_path, DECLARED - {"ac.zeo.zeoapp.libinner"})
    assert reason == "identifier_undeclared"


def test_a_bundle_counts_once_for_itself_and_once_per_nested_item(
    framework_app: App, tmp_path: Path
) -> None:
    reason = _admit(framework_app, tmp_path, DECLARED, Limits(max_executables=4))
    assert reason == "request_invalid"


def test_an_outer_identifier_granted_as_nested_is_refused(
    framework_app: App, tmp_path: Path
) -> None:
    built = framework_app()
    create_request(built, _request(), tmp_path / "request.tar")
    opened = open_request(tmp_path / "request.tar", tmp_path / "in")
    grants = {NODE: {"zeoapp": Grant(REPO, frozenset(DECLARED))}}
    refusal = admit(opened, NODE, _Reviewed(opened.manifest_bytes), grants, Limits())
    assert refusal is not None
    assert refusal.reason == "identifier_undeclared"


@pytest.mark.parametrize(
    "allowlist",
    [
        {NODE: {"zeoapp": Grant(REPO, frozenset({"ac.zeo.pomar.x"}))}},
        {NODE: {"zeoapp": Grant(REPO, frozenset(), frozenset({"ac.zeo.zeoapp.x"}))}},
        {
            NODE: {"zeoapp": Grant(REPO, frozenset(), frozenset({BUNDLE_ID}))},
            "other": {"pomar": Grant(REPO, frozenset(), frozenset({BUNDLE_ID}))},
        },
    ],
)
def test_an_allowlist_binds_each_identifier_to_one_product(
    allowlist: dict[str, dict[str, Grant]],
) -> None:
    with pytest.raises(ValueError, match="identifier|two products"):
        check_allowlist(allowlist)


def test_the_allowlist_is_consistent() -> None:
    from zeo_core.release.signing.admission import ALLOWLIST

    check_allowlist(ALLOWLIST)


def test_zeoapp_has_no_grant_yet() -> None:
    from zeo_core.release.signing.admission import ALLOWLIST

    assert all("zeoapp" not in grants for grants in ALLOWLIST.values())


# --- the command ------------------------------------------------------------


def test_release_tree_prints_a_bundles_digest(framework_app: App) -> None:
    built = framework_app()
    command = make_command(toolchain=FakeToolchain)
    code, body = command(
        ["tree-digest"], json.dumps({"path": str(built.resolve(APP))}).encode()
    )
    assert code == 0
    assert body["unsigned_tree_sha256"] == _bundle(built).unsigned_tree_sha256
    assert body["entries"] == len(listing(built.resolve(APP)))


def test_release_tree_refuses_an_unsafe_tree(tmp_path: Path) -> None:
    (tmp_path / "Zeo.app").mkdir()
    (tmp_path / "Zeo.app" / "out").symlink_to("/etc")
    command = make_command(toolchain=FakeToolchain)
    code, body = command(
        ["tree-digest"], json.dumps({"path": str(tmp_path / "Zeo.app")}).encode()
    )
    assert (code, body["outcome"]) == (20, "bundle_unsafe")


# --- the ruling draft r4 §8A ------------------------------------------------


def test_a_nested_items_unsigned_digest_is_checked(
    framework_app: App, work: WorkDir
) -> None:
    loaded = framework_app()
    entry = yaml.safe_load(loaded.manifest_path.read_text())
    for item in entry["signing"]["bundles"][0]["nested"]:
        if item["identifier"].endswith(".audio"):
            item["unsigned_sha256"] = "0" * 64
    loaded.manifest_path.write_text(yaml.safe_dump(entry))
    tools = FakeToolchain()
    outcome = sign(load(loaded.manifest_path), work, tools, HOST)
    assert (outcome.status, outcome.reason) == ("refused", "input_mismatch")
    assert outcome.executable == "audio"
    assert tools.sign_calls == []


def test_each_nested_receipt_records_its_unsigned_digest(
    framework_app: App, work: WorkDir
) -> None:
    loaded = framework_app()
    signed = sign(loaded, work, FakeToolchain(), HOST)
    by_name = {item.name: item for item in _bundle(loaded).nested}
    for receipt in signed.receipts[:-1]:
        assert receipt["unsigned_sha256"] == by_name[receipt["name"]].unsigned_sha256


def _script(bundle: Path) -> None:
    script = bundle / "Contents" / "Helpers" / "run.sh"
    script.write_bytes(b"#!/bin/sh\n")
    script.chmod(0o750)


def test_an_unlisted_executable_file_refuses_the_bundle(
    framework_app: App, work: WorkDir
) -> None:
    outcome = sign(framework_app(tweak=_script), work, FakeToolchain(), HOST)
    assert (outcome.status, outcome.reason) == ("refused", "nested_unlisted")
    assert "run.sh" in outcome.message


def test_a_listed_item_must_be_mach_o_or_a_bundle(
    framework_app: App, work: WorkDir
) -> None:
    nested = (*NESTED, {"path": "Contents/Helpers/run.sh", "name": "run"})
    outcome = sign(
        framework_app(tweak=_script, nested=nested), work, FakeToolchain(), HOST
    )
    assert (outcome.status, outcome.reason) == ("refused", "nested_missing")


def test_verify_records_gatekeepers_answer_as_information_only(
    framework_app: App, work: WorkDir
) -> None:
    loaded, tools = _signed(framework_app, work)
    outcome = verify(loaded, work, tools)
    # spctl rejects an internal, unnotarised bundle; the release still verifies.
    assert (outcome.status, outcome.reason) == ("done", "verified")
    assert outcome.receipts[-1]["spctl"] == {
        "status": 3,
        "output": "Zeo.app: rejected\nsource=no usable signature",
    }
    assert tools.assessed == [work.bundle_candidate(_bundle(loaded))]
    assert all("spctl" not in receipt for receipt in outcome.receipts[:-1])


# --- zeoapp's real shape and its main executables ----------------------------


def test_zeoapps_real_shape_signs_and_verifies(zeoapp: App, work: WorkDir) -> None:
    loaded = zeoapp()
    tools = FakeToolchain()
    signed = sign(loaded, work, tools, HOST)
    assert (signed.status, signed.reason) == ("done", "signed")
    # The main executable shares the bundle's identity: one codesign call
    # signs it with the bundle, after the renderer.
    assert tools.sign_calls == [
        "ac.zeo.zeoapp.console-renderer",
        "ac.zeo.zeoapp.swiftterm-resources",
        BUNDLE_ID,
    ]
    renderer, resources, outer = signed.receipts
    assert renderer["path"] == "Contents/Frameworks/libZEOConsoleRenderer.dylib"
    assert renderer["main_executable"] is None
    # A codeless resource bundle is signed too, and keeps its own Info.plist.
    assert resources["signed_tree_sha256"] and resources["main_executable"] is None
    candidate = work.bundle_candidate(_bundle(loaded))
    swiftterm = candidate / "Contents/Resources/SwiftTerm_SwiftTerm.bundle"
    assert plistlib.loads((swiftterm / "Info.plist").read_bytes()) == {
        "CFBundleIdentifier": "swiftterm.SwiftTerm.resources"
    }
    assert outer["main_executable"]["path"] == str(MAIN)
    assert (
        outer["main_executable"]["signed_sha256"]
        != (outer["main_executable"]["unsigned_sha256"])
    )
    assert outer["nested_receipts"].keys() == {
        "console-renderer",
        "swiftterm-resources",
    }
    checked = verify(loaded, work, tools)
    assert (checked.status, checked.reason) == ("done", "verified")
    assert [receipt["name"] for receipt in checked.receipts] == [
        "console-renderer",
        "swiftterm-resources",
        "zeoapp",
    ]


@pytest.mark.parametrize(
    "build",
    [
        {
            "nested": (
                *ZEOAPP_NESTED,
                {"path": str(MAIN), "name": "zeoplayground"},
            )
        },
        {
            "framework": True,
            "nested": (
                *NESTED,
                {"path": f"{_AUDIO}/Versions/A/Audio", "name": "audio-binary"},
            ),
        },
    ],
)
def test_a_declared_main_executable_is_never_nested(
    app: App, build: dict[str, Any]
) -> None:
    with pytest.raises(ManifestError, match="never nested"):
        app(**build)


def test_a_macho_item_has_no_main_executable(app: App) -> None:
    renderer = {**RENDERER, "main": "x"}
    with pytest.raises(ManifestError, match="only a bundle"):
        app(nested=(renderer, SWIFTTERM))


@pytest.mark.parametrize(
    ("build", "reason"),
    [
        # Each item is the kind it says it is.
        ({"nested": ({**RENDERER, "kind": "bundle"}, SWIFTTERM)}, "nested_missing"),
        ({"nested": (RENDERER, {**SWIFTTERM, "kind": "macho"})}, "nested_missing"),
        # Declared exactly as Info.plist names it, or not at all.
        ({"main_executable": "none"}, "main_executable_invalid"),
        (
            {
                "main_executable": {
                    "path": "Contents/MacOS/Other",
                    "unsigned_sha256": "0" * 64,
                }
            },
            "main_executable_invalid",
        ),
        ({"nested": (RENDERER, {**SWIFTTERM, "main": "x"})}, "main_executable_invalid"),
        (
            {
                "main_executable": {
                    "path": str(MAIN),
                    "unsigned_sha256": "0" * 64,
                }
            },
            "input_mismatch",
        ),
    ],
)
def test_kinds_and_main_executables_are_as_declared(
    zeoapp: App, work: WorkDir, build: dict[str, Any], reason: str
) -> None:
    tools = FakeToolchain()
    outcome = sign(zeoapp(**build), work, tools, HOST)
    assert (outcome.status, outcome.reason) == ("refused", reason)
    assert tools.sign_calls == []


def test_a_main_executable_is_verified_under_its_bundles_identifier(
    zeoapp: App, work: WorkDir
) -> None:
    loaded = zeoapp()
    tools = FakeToolchain()
    assert sign(loaded, work, tools, HOST).status == "done"
    main = work.bundle_candidate(_bundle(loaded)) / MAIN
    tools.inspections[main] = signed_as("ac.zeo.zeoapp.zeoplayground")
    outcome = verify(loaded, work, tools)
    assert (outcome.status, outcome.reason) == ("refused", "identity_mismatch")

    _rewrite(work, "zeoapp", main_executable=None)
    outcome = verify(loaded, work, tools)
    assert (outcome.status, outcome.reason) == ("refused", "signed_digest_mismatch")


def _plist(path: str, **values: str) -> Callable[[Path], object]:
    def tweak(bundle: Path) -> None:
        (bundle / path).write_bytes(plistlib.dumps(values))

    return tweak


@pytest.mark.parametrize(
    ("build", "reason"),
    [
        (
            {"tweak": _plist("Contents/Info.plist", CFBundleIdentifier=BUNDLE_ID)},
            "main_executable_invalid",
        ),
        (
            {
                "tweak": _plist(
                    "Contents/Info.plist",
                    CFBundleIdentifier=BUNDLE_ID,
                    CFBundleExecutable="Missing",
                )
            },
            "main_executable_invalid",
        ),
        (
            {"tweak": lambda b: (b / MAIN).write_bytes(b"#!/bin/sh\n")},
            "main_executable_invalid",
        ),
    ],
)
def test_the_outer_main_executable_is_the_bundles_own(
    zeoapp: App, work: WorkDir, build: dict[str, Any], reason: str
) -> None:
    tools = FakeToolchain()
    outcome = sign(zeoapp(**build), work, tools, HOST)
    assert (outcome.status, outcome.reason) == ("refused", reason)
    assert tools.sign_calls == []


@pytest.mark.parametrize(
    ("build", "reason"),
    [
        (
            {
                "tweak": _plist(
                    f"{_AUDIO}/Versions/A/Resources/Info.plist",
                    CFBundleIdentifier="ac.zeo.zeoapp.audio",
                )
            },
            "main_executable_invalid",
        ),
        (
            {"tweak": lambda b: (b / _AUDIO / "Versions/A/Audio").write_bytes(b"x")},
            "main_executable_invalid",
        ),
    ],
)
def test_a_nested_frameworks_main_executable_is_its_own(
    framework_app: App, work: WorkDir, build: dict[str, Any], reason: str
) -> None:
    tools = FakeToolchain()
    outcome = sign(framework_app(**build), work, tools, HOST)
    assert (outcome.status, outcome.reason) == ("refused", reason)
    assert tools.sign_calls == []


def test_a_frameworks_binary_through_versions_current_isnt_unlisted(
    framework_app: App, work: WorkDir
) -> None:
    loaded = framework_app()
    framework = loaded.resolve(APP) / _AUDIO
    assert (framework / "Audio").is_symlink()
    assert sign(loaded, work, FakeToolchain(), HOST).status == "done"


# --- the council's review of 1856aaca ---------------------------------------


def test_a_nested_item_signed_by_another_leaf_is_refused_at_verify(
    framework_app: App, work: WorkDir
) -> None:
    built = framework_app()
    tools = FakeToolchain()
    assert sign(built, work, tools, HOST).status == "done"
    renderer = work.bundle_candidate(_bundle(built)) / RENDERER["path"]
    tools.inspections[renderer] = signed_as(
        "ac.zeo.zeoapp.console-renderer", leaf_sha1="AB" * 20
    )
    outcome = verify(built, work, tools)
    assert (outcome.status, outcome.reason) == ("refused", "identity_mismatch")
    assert outcome.executable == "console-renderer"


def test_an_undeclared_entitlement_or_an_empty_blob_is_refused_at_verify(
    framework_app: App, work: WorkDir
) -> None:
    built = framework_app()
    tools = FakeToolchain()
    assert sign(built, work, tools, HOST).status == "done"
    renderer = work.bundle_candidate(_bundle(built)) / RENDERER["path"]
    for granted in ({"com.apple.security.get-task-allow": True}, {}):
        tools.inspections[renderer] = signed_as(
            "ac.zeo.zeoapp.console-renderer", entitlements=granted
        )
        outcome = verify(built, work, tools)
        assert outcome.reason == "entitlements_mismatch"


@pytest.mark.parametrize("target", ["..", ".", "../signed"])
def test_a_response_whose_bundle_is_a_link_is_refused(
    framework_app: App, tmp_path: Path, target: str
) -> None:
    built = framework_app()
    work = WorkDir(tmp_path / "work")
    assert sign(built, work, FakeToolchain(), HOST).status == "done"
    create_response(built, work, tmp_path / "response.tar")

    def swap(members: list[tuple[tarfile.TarInfo, bytes]]) -> None:
        members[:] = [m for m in members if not m[0].name.startswith("signed/")]
        members.append(_link("signed/zeoapp", target))

    _repack(tmp_path / "response.tar", tmp_path / "bad.tar", swap)
    with pytest.raises(PackError):
        open_response(built, tmp_path / "bad.tar", tmp_path / "in")


def test_a_sticky_or_flagged_entry_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "Zeo.app"
    (root / "dir").mkdir(parents=True)
    (root / "dir").chmod(0o1755)
    with pytest.raises(TreeError, match="sticky"):
        listing(root)
    (root / "dir").chmod(0o755)
    (root / "file").write_bytes(b"x")
    if not hasattr(os, "chflags") or not hasattr(stat, "UF_HIDDEN"):
        pytest.skip("no BSD file flags here")
    os.chflags(root / "file", stat.UF_HIDDEN)
    with pytest.raises(TreeError, match="flags"):
        listing(root)


def test_the_signing_copy_carries_only_a_packs_mode(tmp_path: Path) -> None:
    from zeo_core.release.signing.tree import copy_plain

    source, copied = tmp_path / "a", tmp_path / "b"
    source.write_bytes(b"x")
    source.chmod(0o750)
    copy_plain(str(source), str(copied))
    assert stat.S_IMODE(copied.stat().st_mode) == 0o755
    assert copied.read_bytes() == b"x"


def test_a_nested_subtree_that_cant_be_described_is_refused_not_raised(
    framework_app: App, work: WorkDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zeo_core.release.signing import step

    built = framework_app()

    def unsafe(path: Path) -> tuple[str | None, str | None]:
        raise TreeError("a link leaves the nested bundle")

    monkeypatch.setattr(step, "_code_digests", unsafe)
    tools = FakeToolchain()
    outcome = sign(built, work, tools, HOST)
    assert (outcome.status, outcome.reason) == ("refused", "bundle_unsafe")
    assert tools.sign_calls == []
