"""``zeocore doctor``: is this machine ready to use the command? (ZEOCORE-SOW-31 rule 6)

One local, read-only pass that answers what an agent otherwise finds out by
failing: the Python floor, the session store, the pairing, the Broker origin and
the extras each command needs. It makes no network call, writes nothing and
never prints a token, a path or a value from the environment. A check that
fails names the exact next command, and ``who`` when someone other than the
caller must act.

Each check is an event line (``{"event": "check", ...}``); the result is last
and carries the same checks, so one line is enough for a caller that doesn't
stream. Exit 0 when every check passes, 20 when any fails (for doctor, 20 means
"not ready"), 2 for a bad ``--profile``. Checks print as they finish, so a slow
one doesn't hide the others.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from typing import Any, Final

from zeo_core.connections.adapters.subprocess_runner import RealSubprocessRunner
from zeo_core.integrations.hosted.file_store import SESSION_FILE_ENV
from zeo_core.integrations.hosted.pairing import (
    DeviceSession,
    InMemorySecureSessionStore,
    SecureStoreError,
)
from zeo_core.integrations.hosted.transport import ZEOCONNECT_PRODUCTION_ORIGIN

from . import client
from .protocol import (
    EXIT_DONE,
    EXIT_HELD,
    Answer,
    Arguments,
    ArgumentsError,
    Emit,
    emit,
    invalid,
)

#: The Python the package supports; a test holds it to ``requires-python``.
PYTHON_FLOOR: Final = (3, 14)
#: The commands that need an extra: the command, the module it loads, the extra
#: and the third-party packages that extra installs.
EXTRAS: Final = (("release", "zeo_core.cli.release", "github", ("requests",)),)
#: How long the Keychain may take. A locked login Keychain can wait on a dialog.
KEYCHAIN_TIMEOUT: Final = 5.0
ORIGIN_FIX: Final = (
    "unset ZEOCONNECT_URL to use production, or set ZEOCONNECT_DEVELOPMENT=1 "
    "for a development Broker"
)

Check = dict[str, Any]


def _check(
    name: str,
    ok: bool,  # noqa: FBT001
    message: str,
    *,
    next_command: str | None = None,
    who: str | None = None,
    **detail: Any,  # noqa: ANN401 -- small JSON facts: never a secret
) -> Check:
    check: Check = {"check": name, "ok": ok, "message": message, **detail}
    if not ok and next_command is not None:
        check["next"] = next_command
    if not ok and who is not None:
        check["who"] = who
    return check


def _python() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    floor = ".".join(str(part) for part in PYTHON_FLOOR)
    ok = sys.version_info >= PYTHON_FLOOR
    return _check(
        "python",
        ok,
        f"Python {version}" + ("" if ok else f" is below the floor, {floor}"),
        next_command=f"uv tool install --python {floor} zeocore",
        python=version,
        floor=floor,
    )


def _file_fix(reason: str) -> str:
    """The step for one of the session file's fixed refusals (no path, no value)."""
    if "another Broker" in reason:
        return (
            "pair this file again with zeocore login, or point ZEOCONNECT_URL at "
            "the Broker it was paired with"
        )
    if "invalid" in reason or "too large" in reason:
        return f"delete the file {SESSION_FILE_ENV} names, then run zeocore login"
    if "directory" in reason or "above" in reason or "symbolic link" in reason:
        return (
            f"fix {SESSION_FILE_ENV}: an absolute path in a directory you own, "
            "mode 0700, with no symbolic link"
        )
    if "group or other access" in reason or "own file" in reason:
        return f"chmod 600 the file {SESSION_FILE_ENV} names, and own it yourself"
    return f"check that you can read and write the file {SESSION_FILE_ENV} names"


def _keychain_fix() -> str:
    if sys.platform != "darwin":
        return (
            "run zeocore on macOS, or set "
            f"{SESSION_FILE_ENV} for a service user's own session file"
        )
    return "unlock the login Keychain, then run zeocore doctor again"


def _store_failed(kind: str, message: str, fix: str) -> tuple[Check, None, bool]:
    return (
        _check("session_store", False, message, next_command=fix, store=kind),
        None,
        False,
    )


def _session(profile: str | None) -> tuple[Check, DeviceSession | None, bool]:
    """The session store check, the grant it holds, and whether the store works."""
    kind = "file" if os.environ.get(SESSION_FILE_ENV) else "keychain"
    fix = _file_fix("") if kind == "file" else _keychain_fix()
    try:
        runner = RealSubprocessRunner(timeout=KEYCHAIN_TIMEOUT)
        store = client.make_store(profile, runner=runner)
        session = store.load()
    except SecureStoreError as error:
        reason = str(error)
        if "profile" in reason:
            raise ArgumentsError(reason) from None
        if kind == "file":
            return _store_failed(
                kind, f"the session file can't be used: {reason}", _file_fix(reason)
            )
        return _store_failed(
            kind, "no usable secure session store on this machine", fix
        )
    except subprocess.TimeoutExpired:
        return _store_failed(
            kind, "the Keychain did not answer in time", _keychain_fix()
        )
    except ValueError, OSError:
        return _store_failed(kind, "the session store could not be read", fix)
    return (
        _check("session_store", True, "the session store is readable", store=kind),
        session,
        True,
    )


def _pairing(
    session: DeviceSession | None, *, store_ok: bool, origin_ok: bool
) -> Check:
    next_command = "zeocore login"
    who = "a person, who approves the pairing in ZEOconnect"
    if not origin_ok:
        # Login would fail on the same origin, so no person can fix this yet.
        return _check(
            "pairing",
            False,
            "not checked: ZEOCONNECT_URL is not an allowed Broker origin",
            next_command=ORIGIN_FIX,
        )
    if not store_ok:
        return _check(
            "pairing",
            False,
            "not checked: there is no usable session store",
            next_command=next_command,
            who=who,
        )
    if session is None:
        return _check(
            "pairing",
            False,
            "this profile is not paired",
            next_command=next_command,
            who=who,
        )
    if session.refresh_expires_at <= client.now():
        return _check(
            "pairing",
            False,
            "the pairing has expired",
            next_command=next_command,
            who=who,
            expires_at=session.refresh_expires_at.isoformat(),
        )
    return _check(
        "pairing",
        True,
        "this profile is paired",
        expires_at=session.refresh_expires_at.isoformat(),
    )


def _origin() -> Check:
    production = client.origin() == ZEOCONNECT_PRODUCTION_ORIGIN
    development = os.environ.get("ZEOCONNECT_DEVELOPMENT") == "1"
    try:
        client.make_transport(InMemorySecureSessionStore()).close()
    except ValueError:
        return _check(
            "zeoconnect_url",
            False,
            "ZEOCONNECT_URL is not an allowed Broker origin",
            next_command=ORIGIN_FIX,
            origin="production" if production else "custom",
        )
    return _check(
        "zeoconnect_url",
        True,
        "the Broker origin is allowed",
        origin="production" if production else "custom",
        development=development,
    )


def _extras(load: Callable[[str], object] = importlib.import_module) -> list[Check]:
    """One check per command that needs an extra, by loading what it loads."""
    checks: list[Check] = []
    for command, module, extra, packages in EXTRAS:
        try:
            load(module)
        except ModuleNotFoundError as error:
            name = error.name or ""
            if name.startswith("zeo_core"):
                raise
            if name.split(".")[0] in packages:
                checks.append(
                    _check(
                        f"extra:{extra}",
                        False,
                        f"zeocore {command} needs the {extra} extra",
                        next_command=f"pip install 'zeocore[{extra}]'",
                        command=command,
                    )
                )
            else:
                checks.append(
                    _check(
                        f"extra:{extra}",
                        False,
                        f"zeocore {command} can't load: a package it needs is missing",
                        next_command="uv tool install --reinstall zeocore",
                        command=command,
                    )
                )
        else:
            checks.append(
                _check(
                    f"extra:{extra}",
                    True,
                    f"zeocore {command} has the {extra} extra",
                    command=command,
                )
            )
    return checks


def doctor(args: Sequence[str], _stdin: bytes, *, out: Emit = emit) -> Answer:
    parser = Arguments("zeocore doctor")
    parser.add_argument("--profile")
    checks: list[Check] = []

    def report(*found: Check) -> None:
        for check in found:
            checks.append(check)
            out({"event": "check", **check})

    try:
        selected = parser.parse_args(list(args)).profile
        profile = selected or os.environ.get(client.PROFILE_ENV) or None
        report(_python())
        origin = _origin()
        report(origin)
        session_check, session, store_ok = _session(profile)
        report(session_check)
        report(_pairing(session, store_ok=store_ok, origin_ok=origin["ok"]))
        report(*_extras())
    except ArgumentsError as error:
        return invalid(str(error))
    failing = [check["check"] for check in checks if not check["ok"]]
    return (EXIT_HELD if failing else EXIT_DONE), {
        "ok": not failing,
        "outcome": "not_ready" if failing else "ready",
        "failing": failing,
        "checks": checks,
    }
