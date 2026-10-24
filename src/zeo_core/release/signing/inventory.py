"""A reviewed post-build inventory as the signing manifest's digests (SOW-44 rev 2).

A signing manifest pins what it signs: each executable's ``unsigned_sha256``,
each bundle's ``unsigned_tree_sha256`` and each nested piece's digest. Written
by hand into ``release.yaml`` for every release, those pins are the one step a
person had to repeat. With ``pins: inventory`` the manifest keeps only the
reviewed STRUCTURE (paths, identifiers, audience, entitlements) and the digests
come from an inventory file, ``zeo.build-inventory/v1``, made after the build.

The inventory is a file in the owner repository
(``release/inventory/<version>.json``) that goes through an ordinary pull
request. It is trusted only as far as the evidence for it: ``check_review``
reads GitHub itself and accepts it only when the commit is on the default
branch, its tree holds exactly these bytes at the named path, and the council's
approval status on that commit was created by the adopted approver, not by any
other account (SOW-44 rev 2, D1). Nothing here trusts a flag, a hash the caller
supplies about itself, or a copied receipt label.

The inventory replaces where a pin is written, not what a pin means: the
result is the same fully pinned manifest the step has always used.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

SCHEMA: Final = "zeo.build-inventory/v1"

#: The adopted approver: the App that posts ``council/approve`` (mator-approver,
#: bot user id). The context name alone is never enough (SOW-44 rev 2, D1).
APPROVER_USER_ID: Final = 341600801
APPROVE_CONTEXT: Final = "council/approve"

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class InventoryError(ValueError):
    """The inventory can't be used: answered as invalid, before anything runs."""


class Inventory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=False)

    schema_id: str = Field(alias="schema")
    product: str
    pins: dict[str, Sha256]


def parse(content: bytes) -> Inventory:
    """Read an inventory strictly: its schema, its product and its pins."""
    try:
        document = json.loads(content)
    except ValueError:
        raise InventoryError("the inventory is not valid JSON") from None
    if not isinstance(document, dict):
        raise InventoryError("the inventory is not a JSON object")
    try:
        inventory = Inventory.model_validate(document)
    except ValidationError as error:
        raise InventoryError(f"the inventory is invalid: {error}") from None
    if inventory.schema_id != SCHEMA:
        raise InventoryError(f"the inventory's schema is {SCHEMA}")
    return inventory


def _items(owner: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = owner.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise InventoryError(f"the manifest's {key} is not a list of entries")
    return value


def _text(entry: Mapping[str, Any], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise InventoryError(f"a manifest entry has no {key}")
    return value


def _slots(signing: dict[str, Any]) -> list[tuple[str, Callable[[str], None]]]:
    """Every pin the manifest needs: its key, and how to write it.

    The manifest is a plain mapping here, before it has been validated, so
    the shapes are checked as they are read.
    """
    slots: list[tuple[str, Callable[[str], None]]] = []

    def put(entry: dict[str, Any], field: str, key: str) -> None:
        if field in entry:
            raise InventoryError(
                f"{key}: the manifest already pins this; with pins: inventory "
                "the only source of a pin is the inventory"
            )

        def write(value: str) -> None:
            entry[field] = value

        slots.append((key, write))

    def main(entry: dict[str, Any], key: str) -> None:
        declared = entry.get("main_executable", "none")
        if isinstance(declared, dict):
            put(declared, "unsigned_sha256", key)

    for item in _items(signing, "executables"):
        put(item, "unsigned_sha256", f"executable:{_text(item, 'name')}")
    for bundle in _items(signing, "bundles"):
        name = _text(bundle, "name")
        put(bundle, "unsigned_tree_sha256", f"bundle:{name}:tree")
        main(bundle, f"bundle:{name}:main")
        for nested in _items(bundle, "nested"):
            path = _text(nested, "path")
            put(nested, "unsigned_sha256", f"bundle:{name}:nested:{path}")
            main(nested, f"bundle:{name}:nested:{path}:main")
    return slots


def pin_keys(signing: Mapping[str, Any]) -> list[str]:
    """The keys an inventory must hold for this manifest, exactly."""
    return [key for key, _ in _slots(copy.deepcopy(dict(signing)))]


def apply(signing: Mapping[str, Any], inventory: Inventory) -> dict[str, Any]:
    """The manifest's ``signing`` section with every pin taken from the inventory.

    Refuses a pin the manifest already holds, a key the inventory lacks, and a
    key the manifest does not need: an extra pin would be a digest nobody
    asked for, and a missing one a binding nobody checked.
    """
    if inventory.product != signing.get("product"):
        raise InventoryError("the inventory is for another product")
    filled = copy.deepcopy(dict(signing))
    slots = _slots(filled)
    keys = [key for key, _ in slots]
    if len(set(keys)) != len(keys):
        raise InventoryError("two bindings in the manifest share one inventory key")
    missing = sorted(set(keys) - set(inventory.pins))
    extra = sorted(set(inventory.pins) - set(keys))
    if missing or extra:
        raise InventoryError(
            "the inventory does not match the manifest: "
            f"missing {missing or 'none'}, unexpected {extra or 'none'}"
        )
    for key, write in slots:
        write(inventory.pins[key])
    return filled


@runtime_checkable
class ApprovalReads(Protocol):
    """A GitHub reader that can also say who approved a commit."""

    def approval_creators(self, repo: str, commit: str, context: str) -> list[int]:
        """The creator id of ``context``'s LATEST status on ``commit`` if it
        is a success, else an empty list."""
        ...


class ReviewReads(Protocol):
    """What the host reads back from GitHub about the inventory's review."""

    def default_branch(self, repo: str) -> str: ...

    def file_at(self, repo: str, commit: str, path: str) -> bytes | None: ...

    def is_ancestor(self, repo: str, commit: str, branch: str) -> bool: ...

    def approval_creators(self, repo: str, commit: str, context: str) -> list[int]:
        """The creator ids of ``context``'s SUCCESS statuses on ``commit``."""
        ...


def sha256_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def check_review(
    github: ReviewReads,
    *,
    repo: str,
    commit: str,
    path: str,
    content: bytes,
    approver: int = APPROVER_USER_ID,
) -> str | None:
    """None when the inventory was reviewed; otherwise the reason it was not.

    Every condition is read from GitHub by the host: the commit is merged to
    the default branch, the commit's own tree holds these exact bytes at this
    path, and the council's approval on that commit was made by the adopted
    approver. A status that only carries the right name, from anyone else,
    does not count.
    """
    branch = github.default_branch(repo)
    if not github.is_ancestor(repo, commit, branch):
        return "the commit is not merged to the default branch"
    held = github.file_at(repo, commit, path)
    if held is None:
        return "the commit has no inventory at that path"
    if held != content:
        return "the inventory is not the one in the commit"
    creators = github.approval_creators(repo, commit, APPROVE_CONTEXT)
    if approver not in creators:
        return f"no {APPROVE_CONTEXT} by the adopted approver on this commit"
    return None


__all__ = [
    "APPROVER_USER_ID",
    "SCHEMA",
    "Inventory",
    "InventoryError",
    "ApprovalReads",
    "ReviewReads",
    "apply",
    "check_review",
    "parse",
    "pin_keys",
    "sha256_of",
]
