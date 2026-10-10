"""``zeocore release sign | verify | pack | receive``: the shared signing step.

``sign`` and ``verify`` (ZEOCORE-SOW-22) take
``{"manifest": "<path to release.yaml>", "work_dir": "<path>"}`` on stdin.
``pack`` writes a build host's request pack (SOW-23 §2) from
``{"manifest", "out", "repo", "commit", "manifest_path"}``, where the last
three say where the manifest was reviewed. ``receive`` unpacks a response
pack from ``{"manifest", "response", "work_dir"}`` into an empty work
directory and verifies it on this host.
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
    LoadedManifest,
    ManifestError,
    Outcome,
    PackError,
    SigningRequest,
    Toolchain,
    WorkDir,
    create_request,
    load,
    open_response,
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


_FIELDS: Final = {
    "sign": {"manifest", "work_dir"},
    "verify": {"manifest", "work_dir"},
    "pack": {"manifest", "out", "repo", "commit", "manifest_path"},
    "receive": {"manifest", "response", "work_dir"},
}


def _refused(error: PackError) -> Answer:
    return EXIT_HELD, {
        "ok": False,
        "outcome": error.reason,
        "status": "refused",
        "message": str(error),
    }


def _pack(loaded: LoadedManifest, request: dict[str, str]) -> Answer:
    try:
        described = SigningRequest(
            pack_version=1,
            product=loaded.signing.product,
            repo=request["repo"],
            commit=request["commit"],
            manifest_path=request["manifest_path"],
        )
    except ValueError as error:
        return invalid(f"the request is invalid: {error}")
    try:
        request_id = create_request(loaded, described, Path(request["out"]))
    except PackError as error:
        return _refused(error)
    return EXIT_DONE, {"ok": True, "outcome": "packed", "request_id": request_id}


def _receive(
    loaded: LoadedManifest, request: dict[str, str], toolchain: Toolchain
) -> Answer:
    try:
        work = open_response(
            loaded, Path(request["response"]), Path(request["work_dir"])
        )
    except PackError as error:
        return _refused(error)
    except FileExistsError:
        return invalid("work_dir must not exist yet")
    return _answer(verify(loaded, work, toolchain))


def _sign(
    loaded: LoadedManifest,
    request: dict[str, str],
    toolchain: Toolchain,
    host: str | None,
) -> Answer:
    try:
        outcome = sign(loaded, WorkDir(Path(request["work_dir"])), toolchain, host)
    except JournalHeldError:
        return EXIT_HELD, {
            "ok": False,
            "outcome": "signing_in_progress",
            "status": "held",
            "message": "another signing run holds this work directory",
        }
    return _answer(outcome)


def _parse(verb: str, stdin: bytes) -> dict[str, str] | Answer:
    try:
        request = parse_json(stdin)
    except ProtocolError as error:
        return invalid(str(error))
    if (
        not isinstance(request, dict)
        or set(request) != _FIELDS[verb]
        or not all(isinstance(value, str) and value for value in request.values())
    ):
        return invalid(f"stdin must hold exactly: {', '.join(sorted(_FIELDS[verb]))}")
    return request


def make_command(
    toolchain: Callable[[], Toolchain] = DarwinToolchain,
    environ: Mapping[str, str] = os.environ,
) -> Callable[[Sequence[str], bytes], Answer]:
    def release(args: Sequence[str], stdin: bytes) -> Answer:
        if len(args) != 1 or args[0] not in _FIELDS:
            return invalid("release takes 'sign', 'verify', 'pack' or 'receive'")
        verb = args[0]
        request = _parse(verb, stdin)
        if isinstance(request, tuple):
            return request
        try:
            loaded = load(Path(request["manifest"]))
        except ManifestError as error:
            return invalid(str(error))
        if verb == "pack":
            return _pack(loaded, request)
        if verb == "receive":
            return _receive(loaded, request, toolchain())
        if verb == "verify":
            return _answer(
                verify(loaded, WorkDir(Path(request["work_dir"])), toolchain())
            )
        return _sign(loaded, request, toolchain(), environ.get(SIGNING_HOST_VARIABLE))

    return release


release = make_command()
