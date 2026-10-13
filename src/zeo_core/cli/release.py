"""``zeocore release sign | verify | pack | receive``: the shared signing step.

``sign`` and ``verify`` (ZEOCORE-SOW-22) take
``{"manifest": "<path to release.yaml>", "work_dir": "<path>"}`` on stdin.
``pack`` writes a build host's request pack (SOW-23 §2) from
``{"manifest", "out", "repo", "commit", "manifest_path"}``, where the last
three say where the manifest was reviewed. ``receive`` unpacks a response
pack from ``{"manifest", "response", "work_dir"}`` into an empty work
directory and verifies it on this host.

``send`` posts a request pack to matormini's service from
``{"pack", "service", "out"}``, where ``service`` is its tailnet
``100.x.y.z:port``, and writes the response pack to ``out``. Sending the same
pack again is safe: a settled request gets the same answer and is never
signed twice. ``serve`` runs that service from ``{"listen", "state_dir"}``
until interrupted (ZEOCORE-SOW-24).

``tree-digest`` prints a bundle's canonical tree digest from ``{"path"}``, the
``unsigned_tree_sha256`` a build's receipt and its ``release.yaml`` record
(ZEOCORE-SOW-32 §1.2), from the same code the signing step uses.

``pypi plan | admit | apply | approve | reconcile | verify`` are the PyPI
adapter's stages for each reviewed target (ZEOCORE-SOW-26), from
``{"manifest": "<release.yaml>"}``. ``apply`` creates the release tag as the
``zeo-release`` App (its id in ``ZEOCORE_RELEASE_APP_ID``, its key in the
Keychain); ``approve`` approves the waiting deployment as matorclawson, once.
Their journal is this user's, at
``$XDG_STATE_HOME/zeocore/release/pypi-journal.jsonl``, keyed by repository
and version, so "never retried" holds across work directories.
Every signing input comes from the manifest's ``signing`` section, never from
a flag. The signing host is configuration: ``ZEOCORE_SIGNING_HOST`` names it,
and ``sign`` holds on any other host. ``verify`` is read-only and runs on any
host. The result is the last line, as with every command; a receipt is never
printed with a key, a password or a token, because the step never reads one.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

from zeo_core.adapters.runtime_host.canonical import ProtocolError, parse_json
from zeo_core.release import pypi
from zeo_core.release.pypi.targets import (
    RELEASE_APP_ID_VARIABLE,
    RELEASE_APP_KEY_ACCOUNT_VARIABLE,
    RELEASE_APP_KEY_SERVICE,
)
from zeo_core.release.signing import (
    DarwinToolchain,
    JournalHeldError,
    LoadedManifest,
    ManifestError,
    Outcome,
    PackError,
    SigningJournal,
    SigningRequest,
    Toolchain,
    WorkDir,
    create_request,
    load,
    open_response,
    sign,
    verify,
)
from zeo_core.release.signing.admission import GhCli, Limits
from zeo_core.release.signing.service import (
    SigningService,
    Whois,
    serve,
    tailnet_address,
)
from zeo_core.release.signing.tree import TreeError, listing
from zeo_core.release.signing.tree import digest as tree_digest

from .protocol import (
    EXIT_AMBIGUOUS,
    EXIT_DONE,
    EXIT_HELD,
    EXIT_INVALID,
    EXIT_WAIT,
    Answer,
    emit,
    invalid,
    own_action,
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
    "send": {"pack", "service", "out"},
    "serve": {"listen", "state_dir"},
    "sign": {"manifest", "work_dir"},
    "verify": {"manifest", "work_dir"},
    "pack": {"manifest", "out", "repo", "commit", "manifest_path"},
    "receive": {"manifest", "response", "work_dir"},
    "tree-digest": {"path"},
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
        return invalid(
            own_action(
                f"the request is invalid: {error}; fix it and send the request again"
            )
        )
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
        return invalid(
            own_action(
                "work_dir must not exist yet; choose a new work_dir, "
                "or remove the old one"
            )
        )
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
            "message": own_action(
                "another signing run holds this work directory; "
                "wait for it to finish, or use a different work_dir"
            ),
        }
    return _answer(outcome)


def _tree(request: dict[str, str]) -> Answer:
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


def _send(request: dict[str, str], post: Post) -> Answer:
    try:
        host, port = tailnet_address(request["service"])
    except ValueError as error:
        return invalid(str(error))
    out = Path(request["out"])
    if out.exists():
        return invalid(
            own_action(
                "out must not exist yet; choose a new out path, or remove the old one"
            )
        )
    try:
        pack = Path(request["pack"]).read_bytes()
    except OSError:
        return invalid(
            own_action(
                "the pack can't be read; check that pack names a file you can read"
            )
        )
    try:
        status, content_type, body = post(f"http://{host}:{port}/v1/sign", pack)
    except httpx.HTTPError:
        # Safe to repeat: the same pack is the same request, signed at most once.
        return EXIT_HELD, {
            "ok": False,
            "outcome": "service_unreachable",
            "status": "held",
            "message": own_action(
                "no answer from the signing service; send the same pack again"
            ),
        }
    if status == 200 and content_type == "application/x-tar":
        out.write_bytes(body)
        return EXIT_DONE, {
            "ok": True,
            "outcome": "signed",
            "request_id": hashlib.sha256(pack).hexdigest(),
        }
    try:
        answer = json.loads(body)
        state = answer["status"]
    except ValueError, KeyError, TypeError:
        return EXIT_AMBIGUOUS, {
            "ok": False,
            "outcome": "service_answer_unreadable",
            "status": "ambiguous",
            "message": own_action(
                "the signing service's answer can't be read; "
                "send the same pack again, and report it if it repeats"
            ),
        }
    exit_status = {"invalid": EXIT_INVALID, "ambiguous": EXIT_AMBIGUOUS}.get(
        state, EXIT_HELD
    )
    return exit_status, {
        key: answer[key]
        for key in ("ok", "outcome", "status", "message", "request_id")
        if key in answer
    }


Post = Callable[[str, bytes], tuple[int, str, bytes]]


def _post(url: str, body: bytes) -> tuple[int, str, bytes]:
    response = httpx.post(
        url,
        content=body,
        headers={"Content-Type": "application/x-tar"},
        timeout=httpx.Timeout(30.0, read=900.0),
        follow_redirects=False,
    )
    return (
        response.status_code,
        response.headers.get("content-type", ""),
        response.content,
    )


def _serve(
    request: dict[str, str], environ: Mapping[str, str], serve_with: Serve
) -> Answer:
    try:
        tailnet_address(request["listen"])
    except ValueError as error:
        return invalid(str(error))
    try:
        limits = Limits.from_environ(environ)
    except ValueError as error:
        return invalid(str(error))
    host = environ.get(SIGNING_HOST_VARIABLE)
    if not host:
        return EXIT_HELD, {
            "ok": False,
            "outcome": "signing_host_unconfigured",
            "status": "held",
            "message": own_action(
                "set ZEOCORE_SIGNING_HOST to the signing host's name"
            ),
        }
    emit({"event": "serving", "listen": request["listen"]})
    serve_with(request["listen"], Path(request["state_dir"]), host, limits)
    return EXIT_DONE, {"ok": True, "outcome": "stopped"}


Serve = Callable[[str, Path, str, Limits], None]


def _serve_forever(
    listen: str, state: Path, host: str, limits: Limits
) -> None:  # pragma: no cover
    service = SigningService(state, DarwinToolchain(), GhCli(), host, limits=limits)
    try:
        serve(listen, service, Whois())
    except KeyboardInterrupt:
        pass
    finally:
        service.close()


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
        return invalid(
            own_action(
                f"stdin has the wrong fields; pipe a JSON object with exactly: "
                f"{', '.join(sorted(_FIELDS[verb]))}"
            )
        )
    return request


PyPIReads = Callable[[], tuple[pypi.GitHubCli, pypi.PyPIHttp]]
ReleaseAppFactory = Callable[[int, Mapping[str, str]], pypi.TagCreator]
_PYPI_STAGES: Final = ("plan", "admit", "apply", "approve", "reconcile", "verify")
_PYPI_EXIT: Final = {
    "done": EXIT_DONE,
    "waiting": EXIT_WAIT,
    "held": EXIT_HELD,
    "refused": EXIT_HELD,
    "invalid": EXIT_INVALID,
    "ambiguous": EXIT_AMBIGUOUS,
}


def _default_pypi_reads() -> tuple[pypi.GitHubCli, pypi.PyPIHttp]:
    return pypi.GitHubCli(), pypi.PyPIHttp()


def _default_release_app(
    app_id: int, environ: Mapping[str, str]
) -> pypi.TagCreator:  # pragma: no cover - needs the Keychain
    from zeo_core.integrations.github.app_tokens import KeychainAppKeySource

    account = environ.get(RELEASE_APP_KEY_ACCOUNT_VARIABLE) or "private-key"
    key = KeychainAppKeySource(service=RELEASE_APP_KEY_SERVICE, account=account)
    return pypi.ReleaseApp(app_id=app_id, key_source=key)


def pypi_journal_path(environ: Mapping[str, str]) -> Path:
    state = environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "zeocore" / "release" / "pypi-journal.jsonl"


def _release_app_id(environ: Mapping[str, str]) -> int | None:
    """The App's id from configuration; None if unset. Not a secret."""
    value = environ.get(RELEASE_APP_ID_VARIABLE, "")
    if not value:
        return None
    if not value.isdigit() or int(value) <= 0:
        raise ValueError(f"{RELEASE_APP_ID_VARIABLE} must be a positive integer")
    return int(value)


def _journaled(
    path: Path, act: Callable[[SigningJournal], pypi.Outcome]
) -> pypi.Outcome:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        journal = SigningJournal(path)
    except JournalHeldError:
        return pypi.Outcome(
            "held", "apply_in_progress", "another run holds the journal"
        )
    except ValueError:
        return pypi.Outcome(
            "held", "journal_unreadable", "the journal has an unreadable line"
        )
    try:
        return act(journal)
    finally:
        journal.close()


@dataclass(frozen=True)
class _PyPIRun:
    manifest: pypi.PyPIManifest
    github: pypi.GitHubCli
    index: pypi.PyPIHttp
    app_id: int | None
    environ: Mapping[str, str]
    release_app: ReleaseAppFactory

    def stage(self, name: str) -> pypi.Outcome:
        manifest, github, index = self.manifest, self.github, self.index
        journal = pypi_journal_path(self.environ)
        if name == "plan":
            return pypi.plan(manifest, github, index)
        if name == "admit":
            return pypi.admit(manifest, github, self.app_id)
        if name == "reconcile":
            return pypi.reconcile(manifest, github)
        if name == "verify":
            return pypi.verify(manifest, index)
        if name == "approve":
            return _journaled(
                journal,
                lambda j: pypi.approve(manifest, github, index, self.app_id, j),
            )
        if self.app_id is None:
            return pypi.Outcome(
                "refused",
                "release_app_unconfigured",
                f"set {RELEASE_APP_ID_VARIABLE} to the release App's id",
            )
        app = self.release_app(self.app_id, self.environ)
        return _journaled(
            journal, lambda j: pypi.apply(manifest, github, index, app, j)
        )


def _pypi(
    args: Sequence[str],
    stdin: bytes,
    reads: PyPIReads,
    environ: Mapping[str, str],
    release_app: ReleaseAppFactory,
) -> Answer:
    if len(args) != 2 or args[1] not in _PYPI_STAGES:
        return invalid(
            own_action(
                f"release pypi takes one stage; use one of: {', '.join(_PYPI_STAGES)}"
            )
        )
    try:
        request = parse_json(stdin)
    except ProtocolError as error:
        return invalid(str(error))
    if (
        not isinstance(request, dict)
        or set(request) != {"manifest"}
        or not isinstance(request["manifest"], str)
    ):
        return invalid(
            own_action('stdin must be one JSON object; pipe {"manifest": path}')
        )
    try:
        manifest = pypi.load(Path(request["manifest"]))
        app_id = _release_app_id(environ)
    except (pypi.ManifestError, ValueError) as error:
        return invalid(str(error))
    github, index = reads()
    run = _PyPIRun(manifest, github, index, app_id, environ, release_app)
    outcome = run.stage(args[1])
    body: dict[str, object] = {
        "ok": outcome.status == "done",
        "outcome": outcome.reason,
        "status": outcome.status,
        "message": outcome.message,
        "target": f"pypi:{manifest.project}",
    }
    if outcome.facts:
        body["facts"] = outcome.facts
    return _PYPI_EXIT[outcome.status], body


def _signing(
    args: Sequence[str],
    stdin: bytes,
    toolchain: Callable[[], Toolchain],
    environ: Mapping[str, str],
    post: Post,
    serve_with: Serve,
) -> Answer:
    if len(args) != 1 or args[0] not in _FIELDS:
        return invalid(
            own_action(
                f"release takes one verb; use one of: {', '.join([*_FIELDS, 'pypi'])}"
            )
        )
    verb = args[0]
    request = _parse(verb, stdin)
    if isinstance(request, tuple):
        return request
    if verb == "send":
        return _send(request, post)
    if verb == "serve":
        return _serve(request, environ, serve_with)
    if verb == "tree-digest":
        return _tree(request)
    try:
        loaded = load(Path(request["manifest"]))
    except ManifestError as error:
        return invalid(str(error))
    if verb == "pack":
        return _pack(loaded, request)
    if verb == "receive":
        return _receive(loaded, request, toolchain())
    if verb == "verify":
        return _answer(verify(loaded, WorkDir(Path(request["work_dir"])), toolchain()))
    return _sign(loaded, request, toolchain(), environ.get(SIGNING_HOST_VARIABLE))


def make_command(
    toolchain: Callable[[], Toolchain] = DarwinToolchain,
    environ: Mapping[str, str] = os.environ,
    post: Post = _post,
    serve_with: Serve = _serve_forever,
    pypi_reads: PyPIReads = _default_pypi_reads,
    release_app: ReleaseAppFactory = _default_release_app,
) -> Callable[[Sequence[str], bytes], Answer]:
    def release(args: Sequence[str], stdin: bytes) -> Answer:
        if args and args[0] == "pypi":
            return _pypi(args, stdin, pypi_reads, environ, release_app)
        return _signing(args, stdin, toolchain, environ, post, serve_with)

    return release


release = make_command()
