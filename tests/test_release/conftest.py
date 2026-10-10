"""A fake toolchain and a manifest builder, so every refusal has a fixture."""

from __future__ import annotations

import hashlib
import plistlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
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
from zeo_core.release.signing.toolchain import SignAnswerLostError, SignCause

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
    inspections: dict[Path, Inspection] = field(default_factory=dict)
    sign_calls: list[str] = field(default_factory=list)

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
        if self.lose_answer:
            path.write_bytes(path.read_bytes() + b"half")
            raise SignAnswerLostError("lost")
        if self.sign_cause != "signed":
            return self.sign_cause
        path.write_bytes(path.read_bytes() + b"-signed")
        if self.after_sign is not None:
            self.inspections[path] = self.after_sign(identifier, entitlements)
        else:
            granted = (
                plistlib.loads(entitlements.read_bytes()) if entitlements else None
            )
            self.inspections[path] = signed_as(identifier, entitlements=granted)
        return "signed"

    def satisfies(self, path: Path, requirement: str) -> bool:
        return self.satisfied

    def inspect(self, path: Path) -> Inspection:
        return self.inspections.get(path, Inspection(signed=False))


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
