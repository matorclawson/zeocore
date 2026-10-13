"""What ``zeocore release`` can say and do on a plain install.

``release`` reaches GitHub through the ``github`` extra, so ``cli/release.py``
cannot be imported without it. Two things don't need GitHub and live here
instead, so they work on a plain install:

- the usage check of every verb (it runs before stdin is read), and
- ``tree-digest``, which only walks a directory and prints its canonical
  tree digest (``zeo_core.release.signing.tree``, standard library only).

``cli/release.py`` imports its verb table and its request parser from here,
so the two never drift.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from zeo_core.release.signing.tree import TreeError, listing
from zeo_core.release.signing.tree import digest as tree_digest

from .protocol import (
    EXIT_DONE,
    EXIT_HELD,
    Answer,
    ArgumentsError,
    invalid,
    own_action,
    strict_json,
)

#: The stdin fields of every release verb, by verb.
FIELDS: Final = {
    "send": {"pack", "service", "out"},
    "serve": {"listen", "state_dir"},
    "sign": {"manifest", "work_dir"},
    "verify": {"manifest", "work_dir"},
    "pack": {"manifest", "out", "repo", "commit", "manifest_path"},
    "receive": {"manifest", "response", "work_dir"},
    "tree-digest": {"path"},
}

#: The stages of ``release pypi``.
PYPI_STAGES: Final = ("plan", "admit", "apply", "approve", "reconcile", "verify")

#: The one verb that needs no ``github`` extra.
NO_EXTRA_VERB: Final = "tree-digest"


def argument_problem(args: Sequence[str]) -> Answer | None:
    """The usage check of ``release``, which runs before stdin is read."""
    if args and args[0] == "pypi":
        if len(args) != 2 or args[1] not in PYPI_STAGES:
            return invalid(
                own_action(
                    "release pypi takes one stage; "
                    f"use one of: {', '.join(PYPI_STAGES)}"
                )
            )
        return None
    if len(args) != 1 or args[0] not in FIELDS:
        return invalid(
            own_action(
                f"release takes one verb; use one of: {', '.join([*FIELDS, 'pypi'])}"
            )
        )
    return None


def parse_request(verb: str, stdin: bytes) -> dict[str, str] | Answer:
    """The request on stdin: an object with exactly the verb's fields, as text."""
    try:
        request = strict_json(stdin)
    except ArgumentsError as error:
        return error.answer()
    if (
        not isinstance(request, dict)
        or set(request) != FIELDS[verb]
        or not all(isinstance(value, str) and value for value in request.values())
    ):
        return invalid(
            own_action(
                f"stdin has the wrong fields; pipe a JSON object with exactly: "
                f"{', '.join(sorted(FIELDS[verb]))}"
            )
        )
    return request


def tree(request: dict[str, str]) -> Answer:
    """``release tree-digest``: the canonical tree digest of one directory."""
    try:
        entries = listing(Path(request["path"]))
    except TreeError as error:
        return EXIT_HELD, {
            "ok": False,
            "outcome": "bundle_unsafe",
            "status": "refused",
            "message": str(error),
        }
    except OSError:
        return invalid("the bundle can't be read")
    return EXIT_DONE, {
        "ok": True,
        "outcome": "digested",
        "unsigned_tree_sha256": tree_digest(entries),
        "entries": len(entries),
    }


def tree_digest_command(stdin: bytes) -> Answer:
    """``zeocore release tree-digest < {"path": dir}``, on a plain install."""
    request = parse_request(NO_EXTRA_VERB, stdin)
    if isinstance(request, tuple):
        return request
    return tree(request)
