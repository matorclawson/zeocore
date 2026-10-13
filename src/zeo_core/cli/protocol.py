"""The rules every ``zeocore`` command keeps (``cli_protocol`` 1).

See ``zeo_core.cli.__main__`` for the full statement. Kept apart so each
command module can emit events and answer without importing the dispatcher.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from collections.abc import Callable, Sequence
from typing import Any, Final, NoReturn

from zeo_core.adapters.runtime_host.canonical import ProtocolError, parse_json

CLI_PROTOCOL: Final = "1"
#: The exit family, the same numbers as the YouTube publish command's.
EXIT_DONE: Final = 0
EXIT_INVALID: Final = 2
EXIT_APPROVAL: Final = 10
EXIT_WAIT: Final = 11
EXIT_NOT_PAIRED: Final = 12
EXIT_AMBIGUOUS: Final = 13
EXIT_HELD: Final = 20
EXIT_INTERNAL: Final = 1

Answer = tuple[int, dict[str, Any]]
Emit = Callable[[dict[str, Any]], None]
Command = Callable[[Sequence[str], bytes], Answer]


def emit(line: dict[str, Any]) -> None:
    """Write one JSON line to stdout: an event now, or the result last."""
    sys.stdout.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


#: Set to ``1`` to print diagnostics for an internal error on stderr.
DEBUG_ENV: Final = "ZEOCORE_DEBUG"

#: A message is "what failed; the next action". The action's first word is one
#: of these, so a test can hold every refusal to the rule (SOW-31 rule 7).
ACTION_VERBS: Final = frozenset(
    {
        "ask", "call", "change", "check", "choose", "create", "fix", "give",
        "install", "pass", "pipe", "read", "remove", "report", "retry",
        "rerun", "run", "send", "set", "try", "unset", "upgrade", "use",
        "wait", "write",
    }
)  # fmt: skip

#: The action appended when a message names a failure and no next step. Keyed
#: by outcome first, then by exit status.
_NEXT_BY_OUTCOME: Final = {
    "stopped": "ask the owner to lift the stop in ZEOconnect, then ask the same "
    "request again",
    "upgrade_required": "upgrade zeocore with 'uv tool upgrade zeocore', then ask "
    "the same request again",
    "unavailable": "ask the same request again in a few seconds",
    "no_session_store": "set ZEOCORE_SESSION_FILE to a private file, or run on a "
    "Mac with a login Keychain",
    "extra_missing": "install the extra it names, then run the command again",
    "invalid": "fix each loc in errors, then validate the value again",
}
_NEXT_BY_STATUS: Final = {
    EXIT_INVALID: "run zeocore help --json for this command's arguments and stdin",
    EXIT_APPROVAL: "ask the same request again once a person has approved it",
    EXIT_WAIT: "ask the same request again in a few seconds",
    EXIT_NOT_PAIRED: "run zeocore login",
    EXIT_AMBIGUOUS: "ask the same request again, and never send a new one",
    EXIT_HELD: "change the request as the message says, then run it again",
}


class OwnAction(str):
    """A message zeocore wrote itself, whose last clause is the next action.

    Provenance, not wording, decides whether the fixed action is appended: a
    message built from an exception's text (which can quote the caller's
    input) is a plain ``str`` and always gets the fixed action, even when
    that text happens to end like an action. Only ``own_action`` makes one.
    It is a ``str``, so the JSON answer is an ordinary string.
    """

    __slots__ = ()


def own_action(message: str) -> OwnAction:
    """Mark a message zeocore authored, ending in its own next action.

    The text before the last ``;`` may quote an error, but the last clause
    must be zeocore's own words, never the caller's.
    """
    return OwnAction(message)


def ends_with_action(message: str) -> bool:
    """True when the message's last clause starts with an imperative verb.

    A wording check for tests and docs only; it never decides what a caller
    sees (see ``OwnAction``).
    """
    clause = message.rsplit(";", 1)[-1].strip().lower()
    return clause.split(" ", 1)[0].strip(",:") in ACTION_VERBS


def with_next_action(status: int, answer: dict[str, Any]) -> dict[str, Any]:
    """Make a failing answer's message end with what to do next.

    Messages marked ``OwnAction`` are left alone; every other message gets the
    fixed action appended. Nothing here reads the input: the action is fixed
    text chosen by outcome and status.
    """
    if answer.get("ok") is not False or answer.get("event") is not None:
        return answer
    message = answer.get("message")
    outcome = answer.get("outcome")
    if message is None and outcome == "invalid":
        message = "the value doesn't match the schema"
    if not isinstance(message, str) or isinstance(message, OwnAction):
        return answer if message is None else {**answer, "message": message}
    action = _NEXT_BY_OUTCOME.get(str(outcome)) or _NEXT_BY_STATUS.get(status)
    if action is None:
        return answer
    return {**answer, "message": f"{message}; {action}"}


def debug_internal(error: BaseException) -> None:
    """With ZEOCORE_DEBUG=1, say where an internal error happened, on stderr.

    Only the exception type and each frame's file, line and function: never
    the exception's text, arguments or locals, which could quote the input.
    """
    if os.environ.get(DEBUG_ENV) != "1":
        return
    lines = [f"zeocore internal error: {type(error).__name__}"]
    lines += [
        f"  at {frame.filename}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_tb(error.__traceback__)
    ]
    sys.stderr.write("\n".join(lines) + "\n")
    sys.stderr.flush()


def invalid(message: str, *, reason: str | None = None) -> Answer:
    """An invalid request: exit 2, nothing sent. ``reason`` is a fixed category."""
    answer: dict[str, Any] = {
        "ok": False,
        "outcome": "invalid_request",
        "message": message,
    }
    if reason is not None:
        answer["reason"] = reason
    return EXIT_INVALID, answer


class ArgumentsError(ValueError):
    """The command line can't be used; answered as exit 2, never printed.

    ``reason`` is a fixed category (never the input) the answer carries.
    """

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason

    def answer(self) -> Answer:
        """The exit-2 answer. An own-action message keeps its provenance."""
        message = self.args[0] if self.args else ""
        if not isinstance(message, OwnAction):
            message = str(self)
        return invalid(message, reason=self.reason)


class Arguments(argparse.ArgumentParser):
    """argparse that raises instead of printing usage and exiting."""

    def __init__(self, prog: str) -> None:
        super().__init__(prog=prog, add_help=False)

    def error(self, message: str) -> NoReturn:
        raise ArgumentsError(message)


#: What to do for each reason the strict reader refuses stdin. Each is zeocore's
#: own words and never quotes the input.
_JSON_FIX: Final = {
    "empty": "pipe one JSON document on stdin",
    "too_large": "send one JSON document of at most 1 MiB",
    "not_utf8": "send one JSON document encoded as UTF-8",
    "syntax": "send one well-formed JSON document",
    "duplicate_key": "send one JSON document with unique keys",
    "non_finite_number": "send finite numbers only, no NaN or infinity",
    "too_deep": "send a flatter JSON document",
    "outside_canonical_domain": "use safe integers and valid text",
}


def stdin_error(error: ProtocolError) -> ArgumentsError:
    """Why stdin is not strict JSON, as a category and the fix. Never the input."""
    reason = error.reason if error.reason in _JSON_FIX else "syntax"
    return ArgumentsError(
        own_action(f"stdin is not valid strict JSON ({reason}); {_JSON_FIX[reason]}"),
        reason=reason,
    )


def strict_json(stdin: bytes) -> Any:  # noqa: ANN401 -- decoded JSON
    """Read stdin as strict JSON, or raise ``ArgumentsError`` with the reason."""
    try:
        return parse_json(stdin)
    except ProtocolError as error:
        raise stdin_error(error) from None
