"""The ``signing`` section of a reviewed ``release.yaml`` (ZEOCORE-SOW-20 §1).

Every input the step uses comes from here, never from a flag. Each shipped
executable carries one binding; the signing identity follows from its
audience and platform, never from whichever key happens to be available.

``release_manifest: 2`` adds ``bundles`` beside ``executables``: a macOS
bundle signed inside-out, with every nested piece of code listed by its path
inside the bundle (ZEOCORE-SOW-32). Version 1 is unchanged.
"""

from __future__ import annotations

import hashlib
import re
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
_NAME: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
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


def _normal(value: str) -> str:
    """Compared with a bundle's walk as written, so a path must be normal."""
    _relative(value)
    if PurePosixPath(value).as_posix() != value or "/./" in value:
        raise ValueError("a bundle path is a normal path inside the bundle")
    return value


class MainExecutable(_Strict):
    """A bundle's main executable: declared and hashed, never signed alone.

    Its path is relative to its own bundle and is the file Info.plist's
    ``CFBundleExecutable`` names: ``Contents/MacOS/<name>``, or for a
    framework its binary's real path, such as ``Versions/A/<name>``. It is
    signed by its bundle's codesign call, under the bundle's identifier.
    """

    path: RelativePath
    unsigned_sha256: Sha256

    @model_validator(mode="after")
    def _path_is_normal(self) -> MainExecutable:
        _normal(self.path)
        return self


class Nested(_Strict):
    """One piece of code inside a bundle, signed before the bundle itself.

    Its name is its identifier's last part, ``ac.zeo.<product>.<name>``, and
    names its receipt.
    """

    path: RelativePath
    #: A Mach-O file, or a nested bundle directory. A bundle with no main
    #: executable is a codeless resource bundle, still signed.
    kind: Literal["macho", "bundle"]
    #: The file's sha256, or a nested bundle's canonical tree digest.
    unsigned_sha256: Sha256
    identifier: Identifier
    entitlements: Entitlements | Literal["none"]
    main_executable: MainExecutable | Literal["none"] = "none"

    @model_validator(mode="after")
    def _path_is_relative(self) -> Nested:
        _normal(self.path)
        if self.kind == "macho" and self.main_executable != "none":
            raise ValueError(f"{self.path}: only a bundle has a main executable")
        return self

    @property
    def name(self) -> str:
        return self.identifier.rsplit(".", 1)[-1]


class Bundle(_Strict):
    """A macOS bundle: internal, current policy, signed inside-out.

    ``bundle_identifier`` is the ``CFBundleIdentifier`` in its ``Info.plist``,
    kept exactly. ``identifier`` is its signing identifier and equals it: the
    one reviewed exception to ``ac.zeo.<product>.<name>``, for an outer
    bundle only (SOW-32 §1.5). Nested code keeps the rule.
    """

    name: Name
    path: RelativePath
    bundle_identifier: Identifier
    identifier: Identifier
    audience: Literal["internal"]
    platform: Literal["darwin"]
    policy: Literal["current"] = CURRENT
    entitlements: Entitlements | Literal["none"]
    unsigned_tree_sha256: Sha256
    main_executable: MainExecutable | Literal["none"]
    nested: tuple[Nested, ...] = Field(default=(), max_length=64)

    @model_validator(mode="before")
    @classmethod
    def _internal_only(cls, value: object) -> object:
        if isinstance(value, dict) and value.get("audience") == "distributed":
            raise ValueError("a bundle is signed for the internal audience only")
        return value

    @model_validator(mode="after")
    def _binding_is_coherent(self) -> Bundle:
        _relative(self.path)
        if not PurePosixPath(self.path).name.endswith(".app"):
            raise ValueError(f"{self.name}: a bundle's path ends in .app")
        if self.identifier != self.bundle_identifier:
            raise ValueError(
                f"{self.name}: a bundle's identifier is its bundle_identifier"
            )
        for field in ("path", "identifier"):
            values = [getattr(item, field) for item in self.nested]
            if len(set(values)) != len(values):
                raise ValueError(f"{self.name}: two nested items share one {field}")
        if set(self.nested_paths()) & set(self.main_paths()):
            raise ValueError(
                f"{self.name}: a main executable is its bundle's, never nested"
            )
        return self

    def nested_paths(self) -> list[str]:
        return [item.path for item in self.nested]

    def main_paths(self) -> list[str]:
        """Every declared main executable, by its path inside this bundle."""
        found = [] if self.main_executable == "none" else [self.main_executable.path]
        return found + [
            f"{item.path}/{item.main_executable.path}"
            for item in self.nested
            if item.main_executable != "none"
        ]


def _overlaps(one: str, other: str) -> bool:
    a, b = PurePosixPath(one).parts, PurePosixPath(other).parts
    shorter = min(len(a), len(b))
    return a[:shorter] == b[:shorter]


class SigningManifest(_Strict):
    release_manifest: Literal[1, 2]
    product: Product
    executables: tuple[Executable, ...] = Field(default=(), max_length=64)
    bundles: tuple[Bundle, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def _bindings_are_distinct(self) -> SigningManifest:
        if self.release_manifest == 1 and (self.bundles or not self.executables):
            raise ValueError("release_manifest 1 lists executables, and no bundles")
        if not self.executables and not self.bundles:
            raise ValueError("the manifest lists nothing to sign")
        nested = [item for bundle in self.bundles for item in bundle.nested]
        bindings: list[Executable | Bundle | Nested] = [
            *self.executables,
            *self.bundles,
            *nested,
        ]
        for field in ("name", "identifier"):
            values = [getattr(item, field) for item in bindings]
            if len(set(values)) != len(values):
                raise ValueError(f"two bindings share one {field}")
        _paths_apart(self)
        for item in self.executables:
            expected = f"ac.zeo.{self.product}.{item.name}"
            if item.policy == CURRENT and item.identifier != expected:
                raise ValueError(
                    f"{item.name}: a current binding's identifier is {expected}"
                )
        prefix = f"ac.zeo.{self.product}."
        for each in nested:
            suffix = each.identifier.removeprefix(prefix)
            if not each.identifier.startswith(prefix) or not _NAME.fullmatch(suffix):
                raise ValueError(f"{each.path}: a nested identifier is {prefix}<name>")
        return self

    @model_validator(mode="after")
    def _bundle_identifiers_are_own(self) -> SigningManifest:
        # The one exception to ac.zeo.<product>.<name> is a bundle's own
        # CFBundleIdentifier (SOW-32 §1.5), so it is never in the ac.zeo
        # namespace at all. The allowlist holds it the same way: as a bundle
        # identifier of exactly one product, never an ac.zeo one.
        for bundle in self.bundles:
            if bundle.identifier.startswith("ac.zeo."):
                raise ValueError(
                    f"{bundle.name}: a bundle's identifier is its own "
                    "CFBundleIdentifier, never in the ac.zeo namespace"
                )
        return self


def _paths_apart(manifest: SigningManifest) -> None:
    paths = [item.path for item in manifest.executables]
    if len(set(paths)) != len(paths):
        raise ValueError("two executables share one path")
    for bundle in manifest.bundles:
        others = [*paths, *(b.path for b in manifest.bundles if b is not bundle)]
        if any(_overlaps(bundle.path, other) for other in others):
            raise ValueError(f"{bundle.name}: a bundle's path overlaps another")


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
