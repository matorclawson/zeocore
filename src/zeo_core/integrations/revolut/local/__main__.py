"""``python -m zeo_core.integrations.revolut.local <step>``: guided local setup.

``complete`` is the one human checkpoint: Revolut redirects the person's browser to
the redirect URI with a one-time code, and only that person can copy the address.
On a terminal it prompts with hidden input. Without one it reads the first line
from stdin, so a caller can pipe the URL; later lines are ignored, and a pipe that
sends nothing, or stops before the line ends, for ``STDIN_WAIT_SECONDS`` in all is
refused (``< /dev/null`` refuses at once). The URL is never an argument, because
the code would then show in process listings and shell history, and it is never
echoed, not even in a usage error.

A refusal to read the URL prints ``REDIRECT_URL: ...`` and exits 2, like any input
that was not accepted. The older refusals of this module exit 1.
"""

from __future__ import annotations

import argparse
import getpass
import os
import select
import sys
import time
from typing import NoReturn
from urllib.parse import urlsplit

from ..models import RevolutEnvironment
from .session import LocalEnrollmentError, LocalRevolutEnrollment
from .store import LocalStoreError, dump_public

MAX_URL_BYTES = 8192
STDIN_WAIT_SECONDS = 30.0

_ONE_LINE = "run complete in a terminal, or pipe the redirect URL as one line on stdin"
_NEVER_AN_ARGUMENT = (
    "the redirect URL is never an argument; pipe it as one line on stdin or run "
    "complete in a terminal"
)
_INVALID_ARGUMENTS = "invalid arguments; run with --help for the steps and options"
#: Usage errors that argparse is asked to print. Anything else is replaced, because
#: argparse quotes the offending token, and that token could be the redirect URL.
_OWN_ERRORS = frozenset(
    (
        "--redirect-uri or REVOLUT_REDIRECT_URI is required",
        "--client-id or REVOLUT_CLIENT_ID is required",
    )
)


class _InputRefusedError(Exception):
    """Input that was not accepted; nothing was sent. Exits 2."""

    code = "INPUT"


class _NoRedirectUrlError(_InputRefusedError):
    code = "REDIRECT_URL"


class _ClientIdError(_InputRefusedError):
    code = "CLIENT_ID"


class _Parser(argparse.ArgumentParser):
    """Never echoes a token from the command line in a usage error."""

    def error(self, message: str) -> NoReturn:
        super().error(message if message in _OWN_ERRORS else _INVALID_ARGUMENTS)


def _from_descriptor(descriptor: int) -> bytes:
    """The first line from a descriptor, under ONE deadline for the whole line.

    A pipe that stays silent, or sends a partial line and stays open, is refused when
    ``STDIN_WAIT_SECONDS`` have passed since the read began.
    """
    deadline = time.monotonic() + STDIN_WAIT_SECONDS
    limit = MAX_URL_BYTES + 2  # room for a CRLF
    buffer = bytearray()
    while b"\n" not in buffer and len(buffer) < limit:
        remaining = deadline - time.monotonic()
        ready = select.select([descriptor], [], [], max(remaining, 0.0))[0]
        if not ready:
            if buffer:
                raise _NoRedirectUrlError(
                    f"the first line on stdin did not finish in time; {_ONE_LINE}"
                )
            raise _NoRedirectUrlError(f"nothing arrived on stdin; {_ONE_LINE}")
        chunk = os.read(descriptor, limit - len(buffer))
        if not chunk:  # end of input
            break
        buffer += chunk
    line, newline, _ = bytes(buffer).partition(b"\n")
    if not newline and len(buffer) > MAX_URL_BYTES:
        raise _NoRedirectUrlError(f"the first line on stdin is too long; {_ONE_LINE}")
    return line.removesuffix(b"\r")


def _from_stream() -> bytes:
    """The first line of a stream with no descriptor (a test double or embedding)."""
    raw = sys.stdin.readline(MAX_URL_BYTES + 2)  # room for a CRLF
    ended = raw.endswith("\n")
    if not ended and len(raw) > MAX_URL_BYTES:
        raise _NoRedirectUrlError(f"the first line on stdin is too long; {_ONE_LINE}")
    return (raw.rstrip("\r\n") if ended else raw).encode("utf-8")


def _first_line() -> str:
    """The first line of stdin, which must fit in ``MAX_URL_BYTES`` before any strip."""
    try:
        descriptor = sys.stdin.fileno()
    except AttributeError, OSError, ValueError:
        body = _from_stream()
    else:
        body = _from_descriptor(descriptor)
    if len(body) > MAX_URL_BYTES:
        raise _NoRedirectUrlError(f"the first line on stdin is too long; {_ONE_LINE}")
    return body.decode("utf-8").strip()  # not UTF-8: a UnicodeError, refused above


def _redirect_url() -> str:
    """The redirect URL: hidden prompt on a terminal, else the first line of stdin."""
    if sys.stdin is None:  # file descriptor 0 is closed
        raise _NoRedirectUrlError(f"there is no stdin; {_ONE_LINE}")
    try:
        if sys.stdin.isatty():
            # Hidden, so the one-time code does not land in shell history.
            line = getpass.getpass("URL Revolut redirected to: ")
        else:
            line = _first_line()
    except EOFError, KeyboardInterrupt, UnicodeError:
        raise _NoRedirectUrlError(
            f"no redirect URL could be read; {_ONE_LINE}"
        ) from None
    if not line:
        raise _NoRedirectUrlError(f"no redirect URL on stdin; {_ONE_LINE}")
    try:
        urlsplit(line)
    except ValueError:
        raise _NoRedirectUrlError(
            "that is not a redirect URL; copy the whole address from the browser, "
            "then run complete again"
        ) from None
    return line


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m zeo_core.integrations.revolut.local")
    parser.add_argument(
        "--environment",
        choices=[item.value for item in RevolutEnvironment],
        default=os.environ.get("REVOLUT_ENVIRONMENT", RevolutEnvironment.SANDBOX.value),
    )
    steps = parser.add_subparsers(dest="step", required=True)
    setup = steps.add_parser("setup", help="create the key and certificate")
    setup.add_argument("--redirect-uri", default=os.environ.get("REVOLUT_REDIRECT_URI"))
    setup.add_argument("--new-key", action="store_true")
    authorize = steps.add_parser("authorize", help="record the client id; get the URL")
    authorize.add_argument("--client-id", default=os.environ.get("REVOLUT_CLIENT_ID"))
    steps.add_parser(
        "complete", help="the URL Revolut redirected to: prompt, or one line on stdin"
    )
    steps.add_parser("status", help="show enrollment state without any secret")
    steps.add_parser("accounts", help="list accounts as a first live check")
    return parser


def _step(
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
    enrollment: LocalRevolutEnrollment,
) -> None:
    if arguments.step == "setup":
        if not arguments.redirect_uri:
            parser.error("--redirect-uri or REVOLUT_REDIRECT_URI is required")
        path = enrollment.setup(
            redirect_uri=arguments.redirect_uri, new_key=arguments.new_key
        )
        print(f"Register this certificate in Revolut Business settings:\n{path}")
    elif arguments.step == "authorize":
        if not arguments.client_id:
            parser.error("--client-id or REVOLUT_CLIENT_ID is required")
        if any(mark in arguments.client_id for mark in ("://", "?", "=")):
            raise _ClientIdError(
                "that is not a client id; it looks like an address. Copy the client "
                "id from Revolut Business settings, then run authorize again"
            )
        print(enrollment.authorize(client_id=arguments.client_id))
    elif arguments.step == "complete":
        enrollment.complete(_redirect_url())
        print("Enrolled.")
    elif arguments.step == "status":
        state = enrollment.status()
        print("Not set up." if state is None else dump_public(state))
    else:
        for account in enrollment.read(lambda client: client.list_accounts()):
            print(account.id, account.currency, account.state)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments, extra = parser.parse_known_args(argv)
    if extra and arguments.step == "complete":
        print(f"REDIRECT_URL: {_NEVER_AN_ARGUMENT}", file=sys.stderr)
        return 2
    if extra:
        parser.error("unrecognized arguments")
    enrollment = LocalRevolutEnrollment(RevolutEnvironment(arguments.environment))
    try:
        _step(parser, arguments, enrollment)
    except _InputRefusedError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        return 2
    except (LocalEnrollmentError, LocalStoreError) as error:
        code = getattr(error, "code", "STORE")
        print(f"{code}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
