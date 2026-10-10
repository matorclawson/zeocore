"""``zeocore release sign | verify``: the shared signing step (ZEOCORE-SOW-22).

stdin is ``{"manifest": "<path to release.yaml>", "work_dir": "<path>"}``.
Every signing input comes from the manifest's ``signing`` section, never from
a flag. The signing host is configuration: ``ZEOCORE_SIGNING_HOST`` names it,
and ``sign`` holds on any other host. ``verify`` is read-only and runs on any
host. The result is the last line, as with every command; a receipt is never
printed with a key, a password or a token, because the step never reads one.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

from zeo_core.adapters.runtime_host.canonical import ProtocolError, parse_json
from zeo_core.release.signing import (
    DarwinToolchain,
    JournalHeldError,
    ManifestError,
    Outcome,
    Toolchain,
    WorkDir,
    load,
    sign,
    verify,
)

from .protocol import (
    EXIT_AMBIGUOUS,
    EXIT_DONE,
    EXIT_HELD,
    EXIT_WAIT,
    Answer,
    invalid,
)

SIGNING_HOST_VARIABLE: Final = "ZEOCORE_SIGNING_HOST"
_EXIT: Final = {
    "done": EXIT_DONE,
    "waiting": EXIT_WAIT,
    "ambiguous": EXIT_AMBIGUOUS,
    "held": EXIT_HELD,
    "refused": EXIT_HELD,
}


def _answer(outcome: Outcome) -> Answer:
    body: dict[str, object] = {
        "ok": outcome.status == "done",
        "outcome": outcome.reason,
        "status": outcome.status,
        "message": outcome.message,
    }
    if outcome.executable is not None:
        body["executable"] = outcome.executable
    if outcome.receipts:
        body["receipts"] = list(outcome.receipts)
    return _EXIT[outcome.status], body


def make_command(
    toolchain: Callable[[], Toolchain] = DarwinToolchain,
    environ: Mapping[str, str] = os.environ,
) -> Callable[[Sequence[str], bytes], Answer]:
    def release(args: Sequence[str], stdin: bytes) -> Answer:
        if len(args) != 1 or args[0] not in ("sign", "verify"):
            return invalid("release takes 'sign' or 'verify'")
        try:
            request = parse_json(stdin)
        except ProtocolError as error:
            return invalid(str(error))
        if (
            not isinstance(request, dict)
            or set(request) != {"manifest", "work_dir"}
            or not all(isinstance(value, str) and value for value in request.values())
        ):
            return invalid('stdin must be {"manifest": path, "work_dir": path}')
        try:
            loaded = load(Path(request["manifest"]))
        except ManifestError as error:
            return invalid(str(error))
        work = WorkDir(Path(request["work_dir"]))
        if args[0] == "verify":
            return _answer(verify(loaded, work, toolchain()))
        try:
            outcome = sign(
                loaded, work, toolchain(), environ.get(SIGNING_HOST_VARIABLE)
            )
        except JournalHeldError:
            return EXIT_HELD, {
                "ok": False,
                "outcome": "signing_in_progress",
                "status": "held",
                "message": "another signing run holds this work directory",
            }
        return _answer(outcome)

    return release


release = make_command()
