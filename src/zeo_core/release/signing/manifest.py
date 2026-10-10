"""The ``signing`` section of a reviewed ``release.yaml`` (ZEOCORE-SOW-20 §1).

Every input the step uses comes from here, never from a flag. Each shipped
executable carries one binding; the signing identity follows from its
audience and platform, never from whichever key happens to be available.
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Annotated, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

CURRENT: Final = "current"
#: Verify-only policies, named so that rollback can verify them. Nothing new
#: is ever signed under them (SOW-20 §3).
LEGACY_POLICIES: Final = frozenset(
    {"legacy-runtime-installed", "legacy-releasesupport-public"}
)

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Name = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
Product = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
Identifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]{0,254}$")
]
Audience = Literal["internal", "distributed"]
Platform = Literal["darwin", "linux"]
Policy = Literal["current", "legacy-runtime-installed", "legacy-releasesupport-public"]


class ManifestError(ValueError):
    """The manifest can't be used: answered as invalid, before anything runs."""


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError("a path must be relative to the manifest, without '..'")
    return value


RelativePath = Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Entitlements(_Strict):
    """The reviewed entitlements file, pinned by its sha256."""

    path: RelativePath
    sha256: Sha256

    @model_validator(mode="after")
    def _path_is_relative(self) -> Entitlements:
        _relative(self.path)
        return self


class Executable(_Strict):
    name: Name
    path: RelativePath
    unsigned_sha256: Sha256
    identifier: Identifier
    audience: Audience
    platform: Platform
    policy: Policy = CURRENT
    entitlements: Entitlements | Literal["none"]

    @model_validator(mode="after")
    def _binding_is_coherent(self) -> Executable:
        _relative(self.path)
        if self.platform == "linux" and self.entitlements != "none":
            raise ValueError("entitlements are Darwin only")
        return self


class SigningManifest(_Strict):
    release_manifest: Literal[1]
    product: Product
    executables: tuple[Executable, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _bindings_are_distinct(self) -> SigningManifest:
        for field in ("name", "path", "identifier"):
            values = [getattr(item, field) for item in self.executables]
            if len(set(values)) != len(values):
                raise ValueError(f"two executables share one {field}")
        for item in self.executables:
            expected = f"ac.zeo.{self.product}.{item.name}"
            if item.policy == CURRENT and item.identifier != expected:
                raise ValueError(
                    f"{item.name}: a current binding's identifier is {expected}"
                )
        return self


class LoadedManifest(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    signing: SigningManifest
    root: Path
    sha256: Sha256
    manifest_path: Path

    def resolve(self, relative: str) -> Path:
        return self.root / relative


def load(path: Path) -> LoadedManifest:
    """Read ``release.yaml`` and its ``signing`` section, strictly."""
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ManifestError(f"the manifest can't be read: {error.strerror}") from None
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError:
        raise ManifestError("the manifest is not valid YAML") from None
    if not isinstance(document, dict) or not isinstance(document.get("signing"), dict):
        raise ManifestError("the manifest has no signing section")
    try:
        signing = SigningManifest.model_validate(document["signing"])
    except ValueError as error:
        raise ManifestError(f"the signing section is invalid: {error}") from None
    return LoadedManifest(
        signing=signing,
        root=path.resolve().parent,
        sha256=hashlib.sha256(content).hexdigest(),
        manifest_path=path.resolve(),
    )
