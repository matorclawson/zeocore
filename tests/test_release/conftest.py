"""A fake toolchain and a manifest builder, so every refusal has a fixture."""

from __future__ import annotations

import hashlib
import plistlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml

from zeo_core.release.signing import (
    INTERNAL,
    Identity,
    Inspection,
    KeychainState,
    LoadedManifest,
    designated_requirement,
    load,
)
from zeo_core.release.signing.toolchain import (
    Assessment,
    SignAnswerLostError,
    SignCause,
)

HOST = "matormini"
VIRTUALIZATION = {"com.apple.security.virtualization": True}


def signed_as(
    identifier: str,
    identity: Identity = INTERNAL,
    /,
    **changes: Any,  # noqa: ANN401
) -> Inspection:
    good = Inspection(
        signed=True,
        identifier=identifier,
        designated=designated_requirement(identifier, identity),
        designated_explicit=True,
        leaf_sha1=identity.certificate_sha1,
        entitlements=None,
    )
    return replace(good, **changes)


@dataclass
class FakeToolchain:
    host_name: str = HOST
    keychain: KeychainState = KeychainState.UNLOCKED
    available: frozenset[str] = frozenset({INTERNAL.certificate_sha1})
    sign_cause: SignCause = "signed"
    lose_answer: bool = False
    identities_fail: bool = False
    # What a signed file reads back as; by default exactly the binding.
    after_sign: Callable[[str, Path | None], Inspection] | None = None
    satisfied: bool = True
    sealed_ok: bool = True
    sealed_fails: bool = False
    fail_on: str | None = None  # an identifier whose codesign run fails
    inspections: dict[Path, Inspection] = field(default_factory=dict)
    sign_calls: list[str] = field(default_factory=list)
    sealed_calls: list[Path] = field(default_factory=list)
    assessed: list[Path] = field(default_factory=list)

    def host(self) -> str:
        return self.host_name

    def keychain_state(self) -> KeychainState:
        return self.keychain

    def identities(self) -> frozenset[str]:
        from zeo_core.release.signing import ToolError

        if self.identities_fail:
            raise ToolError("unreadable")
        return self.available

    def sign(
        self,
        path: Path,
        *,
        identifier: str,
        identity: Identity,
        requirement: str,
        entitlements: Path | None,
        hardened: bool,
    ) -> SignCause:
        self.sign_calls.append(identifier)
        assert requirement == designated_requirement(identifier, identity)
        assert not hardened or identity.audience == "distributed"
        if self.lose_answer:
            _mark(path, b"half")
            raise SignAnswerLostError("lost")
        if identifier == self.fail_on:
            return "failed_unknown"
        if self.sign_cause != "signed":
            return self.sign_cause
        _mark(path, b"-signed")
        if self.after_sign is not None:
            seen = self.after_sign(identifier, entitlements)
        else:
            granted = (
                plistlib.loads(entitlements.read_bytes()) if entitlements else None
            )
            seen = signed_as(identifier, entitlements=granted)
        self.inspections[path] = seen
        if path.is_dir():
            # As codesign does: a bundle's call signs its main executable too.
            from zeo_core.release.signing.bundle import info_executable

            _, main = info_executable(path.parent, path.name)
            if main is not None:
                _mark(path / main, b"-signed")
                self.inspections[path / main] = seen
        return "signed"

    def satisfies(self, path: Path, requirement: str) -> bool:
        return self.satisfied

    def assess(self, path: Path) -> Assessment:
        self.assessed.append(path)
        return Assessment(3, f"{path.name}: rejected\nsource=no usable signature")

    def sealed(self, path: Path) -> bool:
        from zeo_core.release.signing import ToolError

        self.sealed_calls.append(path)
        if self.sealed_fails:
            raise ToolError("unreadable")
        return self.sealed_ok

    def inspect(self, path: Path) -> Inspection:
        return self.inspections.get(path, Inspection(signed=False))


def _mark(path: Path, data: bytes) -> None:
    """What codesign changes: a file's bytes, or a bundle's signature file."""
    if path.is_dir():
        base = path / "Contents" if (path / "Contents").is_dir() else path
        signature = base / "_CodeSignature" / "CodeResources"
        signature.parent.mkdir(exist_ok=True)
        signature.write_bytes(data)
    else:
        path.write_bytes(path.read_bytes() + data)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def release(tmp_path: Path) -> Callable[..., LoadedManifest]:
    """Write a release.yaml with built binaries, and load it."""

    def build(*executables: dict[str, Any], product: str = "pomar") -> LoadedManifest:
        root = tmp_path / "release"
        (root / "bin").mkdir(parents=True, exist_ok=True)
        entries = []
        for spec in executables or ({"name": "pomar"},):
            name = spec["name"]
            content = spec.get("content", f"binary {name}".encode())
            (root / "bin" / name).write_bytes(content)
            entry: dict[str, Any] = {
                "name": name,
                "path": f"bin/{name}",
                "unsigned_sha256": _sha(content),
                "identifier": spec.get("identifier", f"ac.zeo.{product}.{name}"),
                "audience": spec.get("audience", "internal"),
                "platform": spec.get("platform", "darwin"),
                "policy": spec.get("policy", "current"),
                "entitlements": "none",
            }
            if "entitlements" in spec:
                plist = plistlib.dumps(spec["entitlements"])
                (root / f"{name}.entitlements").write_bytes(plist)
                entry["entitlements"] = {
                    "path": f"{name}.entitlements",
                    "sha256": _sha(plist),
                }
            entries.append(entry)
        manifest = root / "release.yaml"
        manifest.write_text(
            yaml.safe_dump(
                {
                    "target": "kept for later stages",
                    "signing": {
                        "release_manifest": 1,
                        "product": product,
                        "executables": entries,
                    },
                }
            )
        )
        return load(manifest)

    return build


#: A thin 64-bit Mach-O's magic, little-endian, then anything.
MACHO = bytes.fromhex("cffaedfe") + b"\x07\x00\x00\x01 code"
BUNDLE_ID = "org.zeroemployee.zeo.operator"
APP = "app/build/Zeo.app"


def _code(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MACHO + path.name.encode())
    path.chmod(0o755)


def write_app(root: Path, *, framework: bool = False) -> Path:
    """zeoapp's real shape: Zeo.app, its main executable and one renderer.

    ``framework`` adds a helper and a nested framework with its
    ``Versions/Current`` links and a dylib of its own.
    """
    app = root / APP
    contents = app / "Contents"
    (contents / "Resources").mkdir(parents=True, exist_ok=True)
    (contents / "Info.plist").write_bytes(
        plistlib.dumps(
            {"CFBundleIdentifier": BUNDLE_ID, "CFBundleExecutable": MAIN.name}
        )
    )
    _code(app / MAIN)
    _code(contents / "Frameworks" / "libZEOConsoleRenderer.dylib")
    (contents / "Resources" / "model.txt").write_bytes(b"weights")
    (contents / "Resources" / "AppIcon.icns").write_bytes(b"icon")
    (contents / "PkgInfo").write_bytes(b"APPL????")
    # A codeless resource bundle: an Info.plist and a Metal library, no code.
    swiftterm = contents / "Resources" / "SwiftTerm_SwiftTerm.bundle"
    swiftterm.mkdir()
    (swiftterm / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "swiftterm.SwiftTerm.resources"})
    )
    (swiftterm / "default.metallib").write_bytes(b"MTLB metal library")
    if framework:
        _code(contents / "Helpers" / "llama-completion")
        bundle = contents / "Frameworks" / "Audio.framework"
        version = bundle / "Versions" / "A"
        _code(version / "Audio")
        _code(version / "libinner.dylib")
        (version / "Resources").mkdir()
        (version / "Resources" / "Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": "ac.zeo.zeoapp.audio",
                    "CFBundleExecutable": "Audio",
                }
            )
        )
        (bundle / "Versions" / "Current").symlink_to("A")
        (bundle / "Audio").symlink_to("Versions/Current/Audio")
        (bundle / "Resources").symlink_to("Versions/Current/Resources")
    return app


MAIN = PurePosixPath("Contents/MacOS/ZEOPlayground")
RENDERER: dict[str, Any] = {
    "path": "Contents/Frameworks/libZEOConsoleRenderer.dylib",
    "name": "console-renderer",
}
SWIFTTERM: dict[str, Any] = {
    "path": "Contents/Resources/SwiftTerm_SwiftTerm.bundle",
    "name": "swiftterm-resources",
    "kind": "bundle",
}
ZEOAPP_NESTED: tuple[dict[str, Any], ...] = (RENDERER, SWIFTTERM)
NESTED: tuple[dict[str, Any], ...] = (
    RENDERER,
    {"path": "Contents/Helpers/llama-completion", "name": "llama-completion"},
    {
        "path": "Contents/Frameworks/Audio.framework",
        "name": "audio",
        "kind": "bundle",
        "main": "Versions/A/Audio",
    },
    {
        "path": "Contents/Frameworks/Audio.framework/Versions/A/libinner.dylib",
        "name": "libinner",
    },
    SWIFTTERM,
)


def _main(base: Path, path: str | None) -> dict[str, str] | str:
    if path is None:
        return "none"
    return {"path": path, "unsigned_sha256": _digest_of(base / path)}


def _tree_of(bundle: Path) -> str:
    from zeo_core.release.signing.tree import TreeError, tree_sha256

    try:
        return tree_sha256(bundle)
    except TreeError:
        return "0" * 64  # an unsafe tree has no digest


def _digest_of(path: Path) -> str:
    from zeo_core.release.signing.tree import tree_sha256

    if path.is_dir() and not path.is_symlink():
        return tree_sha256(path)
    if path.is_file() and not path.is_symlink():
        return _sha(path.read_bytes())
    return "0" * 64


@pytest.fixture
def app(tmp_path: Path) -> Callable[..., LoadedManifest]:
    """Write a release.yaml (manifest 2) with a built Zeo.app, and load it.

    zeoapp's real shape, or with ``framework`` a nested framework too.
    ``tweak`` changes the tree before its digest is taken; ``nested``
    replaces the listing; ``changes`` override the bundle entry.
    """

    def build(
        *,
        framework: bool = False,
        tweak: Callable[[Path], object] | None = None,
        nested: tuple[dict[str, Any], ...] | None = None,
        entitlements: dict[str, Any] | None = None,
        **changes: Any,  # noqa: ANN401
    ) -> LoadedManifest:
        root = tmp_path / "release"
        root.mkdir(parents=True, exist_ok=True)
        bundle = write_app(root, framework=framework)
        if nested is None:
            nested = NESTED if framework else ZEOAPP_NESTED
        if tweak is not None:
            tweak(bundle)
        entry: dict[str, Any] = {
            "name": "zeoapp",
            "path": APP,
            "bundle_identifier": BUNDLE_ID,
            "identifier": BUNDLE_ID,
            "audience": "internal",
            "platform": "darwin",
            "policy": "current",
            "entitlements": "none",
            "unsigned_tree_sha256": _tree_of(bundle),
            "main_executable": _main(bundle, str(MAIN)),
            "nested": [
                {
                    "path": item["path"],
                    "kind": item.get("kind", "macho"),
                    "main_executable": _main(bundle / item["path"], item.get("main")),
                    "unsigned_sha256": _digest_of(bundle / item["path"]),
                    "identifier": f"ac.zeo.zeoapp.{item['name']}",
                    "entitlements": item.get("entitlements", "none"),
                }
                for item in nested
            ],
        }
        if entitlements is not None:
            plist = plistlib.dumps(entitlements)
            (root / "zeoapp.entitlements").write_bytes(plist)
            entry["entitlements"] = {
                "path": "zeoapp.entitlements",
                "sha256": _sha(plist),
            }
        entry.update(changes)
        manifest = root / "release.yaml"
        manifest.write_text(
            yaml.safe_dump(
                {
                    "signing": {
                        "release_manifest": 2,
                        "product": "zeoapp",
                        "bundles": [entry],
                    }
                }
            )
        )
        return load(manifest)

    return build
