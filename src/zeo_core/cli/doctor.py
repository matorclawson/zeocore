"""``zeocore doctor``: is this machine ready to use the command? (ZEOCORE-SOW-31 rule 6)

One local, read-only pass that answers what an agent otherwise finds out by
failing: the Python floor, the session store, the pairing, the Broker origin and
the extras each command needs. It makes no network call, writes nothing and
never prints a token, a path or a value from the environment. A check that
fails names the exact next command, and ``who`` when someone other than the
caller must act.

Each check is an event line (``{"event": "check", ...}``); the result is last
and carries the same checks, so one line is enough for a caller that doesn't
stream. Exit 0 when every check passes, 20 when any fails, 2 for a bad
``--profile``.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any, Final

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
#: The commands that need an extra, with the module they load and the extra.
EXTRAS: Final = (("release", "zeo_core.cli.release", "github"),)

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


def _store_next() -> str:
    if os.environ.get(SESSION_FILE_ENV):
        return (
            f"fix {SESSION_FILE_ENV}: an absolute path in a directory you own, "
            "mode 0700, with no symbolic link"
        )
    if sys.platform != "darwin":
        return (
            "run zeocore on macOS, or set "
            f"{SESSION_FILE_ENV} for a service user's own session file"
        )
    return "unlock the login Keychain, then run zeocore doctor again"


def _session(profile: str | None) -> tuple[Check, DeviceSession | None, bool]:
    """The session store check, the grant it holds, and whether the store works."""
    kind = "file" if os.environ.get(SESSION_FILE_ENV) else "keychain"
    try:
        store = client.make_store(profile)
        session = store.load()
    except SecureStoreError as error:
        if "profile" in str(error):
            raise ArgumentsError(str(error)) from None
        return (
            _check(
                "session_store",
                False,
                "no usable secure session store on this machine",
                next_command=_store_next(),
                store=kind,
            ),
            None,
            False,
        )
    return (
        _check("session_store", True, "the session store is readable", store=kind),
        session,
        True,
    )


def _pairing(session: DeviceSession | None, *, store_ok: bool) -> Check:
    next_command = "zeocore login"
    who = "a person, who approves the pairing in ZEOconnect"
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
    fix = (
        "unset ZEOCONNECT_URL to use production, or set ZEOCONNECT_DEVELOPMENT=1 "
        "for a development Broker"
    )
    try:
        client.make_transport(InMemorySecureSessionStore()).close()
    except ValueError:
        return _check(
            "zeoconnect_url",
            False,
            "ZEOCONNECT_URL is not an allowed Broker origin",
            next_command=fix,
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
    for command, module, extra in EXTRAS:
        try:
            load(module)
        except ModuleNotFoundError as error:
            if (error.name or "").startswith("zeo_core"):
                raise
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
                    True,
                    f"zeocore {command} has the {extra} extra",
                    command=command,
                )
            )
    return checks


def doctor(args: Sequence[str], _stdin: bytes, *, out: Emit = emit) -> Answer:
    parser = Arguments("zeocore doctor")
    parser.add_argument("--profile")
    try:
        selected = parser.parse_args(list(args)).profile
        profile = selected or os.environ.get(client.PROFILE_ENV) or None
        checks = [_python()]
        session_check, session, store_ok = _session(profile)
        checks += [session_check, _pairing(session, store_ok=store_ok), _origin()]
        checks += _extras()
    except ArgumentsError as error:
        return invalid(str(error))
    for check in checks:
        out({"event": "check", **check})
    failing = [check["check"] for check in checks if not check["ok"]]
    return (EXIT_HELD if failing else EXIT_DONE), {
        "ok": not failing,
        "outcome": "not_ready" if failing else "ready",
        "failing": failing,
        "checks": checks,
    }
