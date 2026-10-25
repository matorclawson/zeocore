"""``zeocore <command>``: JSON on stdin, JSON lines on stdout.

Commands (``zeocore help`` is the machine catalogue of all of them):

- ``zeocore help [--json]``: every command, its arguments, stdin, the exit
  family and its result schema (``zeo_core.cli.catalogue``).
- ``zeocore version``: the release, this command's protocol and the
  ZEOconnect protocol it speaks.
- ``zeocore doctor``: is this machine ready? One local, read-only check per line
  (Python, session store, pairing, ``ZEOCONNECT_URL``, extras); each failing
  check names the next command (``zeo_core.cli.doctor``).
- ``zeocore schema list``: the stable schema names.
- ``zeocore schema <name>``: that contract as JSON Schema 2020-12.
- ``zeocore validate <name>``: validate stdin against ``<name>``. Answers
  ``{"ok": true, "value": ...}`` with the normalized value, or
  ``{"ok": false, "errors": [{"loc": [...], "type": ...}]}``. Errors never
  echo input values.
- ``zeocore digest``: the sha256 of stdin's RFC 8785 canonical bytes.
- ``zeocore login | logout | whoami | connections | invoke | artifact get``:
  the ZEOconnect Broker client (``zeo_core.cli.client``).
- ``zeocore release sign | verify | pack | receive | send | serve | tree-digest``: the
  shared signing step
  (``zeo_core.cli.release``). It needs the ``github`` extra and loads only
  when it is used; every other command runs on a plain install.

Rules every command keeps (``cli_protocol`` 1):

- stdout is JSON lines and nothing else. Events (a pairing code, a doctor
  check, an approval link, waiting) carry an ``event`` key; the result is always
  the last line, carries ``ok`` and ``schema`` (``zeocore.<command>/v1``) and never
  ``event``. A command with no events prints one line. Diagnostics go to
  stderr.
- Input JSON is strict: no duplicate keys, no NaN, at most 1 MiB.
- Exit status is zeocore's one family, shared with the YouTube publish
  command: 0 done; 2 invalid input, nothing sent; 10 approval required; 11
  waiting, so try the same request later; 12 not paired; 13 ambiguous,
  never retried by the command; 20 held or refused (including a value that
  doesn't match its schema). 1 is an internal error in zeocore itself, with
  ``{"ok": false, "outcome": "internal"}`` and a fixed message, with no detail
  of the input; for an operation, treat it like 13: its state is unknown.
  ``ZEOCORE_DEBUG=1`` prints the exception's type and where it happened to
  stderr, never its text or the input.
- A failing answer's ``message`` ends with the next action, after a ``;``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from typing import Final

from pydantic import ValidationError

from zeo_core.adapters.runtime_host.canonical import (
    MAX_BYTES,
    ProtocolError,
    digest,
    parse_json,
)

from . import client, doctor
from .catalogue import catalogue
from .protocol import (
    CLI_PROTOCOL,
    EXIT_AMBIGUOUS,
    EXIT_APPROVAL,
    EXIT_DONE,
    EXIT_HELD,
    EXIT_INTERNAL,
    EXIT_INVALID,
    EXIT_NOT_PAIRED,
    EXIT_WAIT,
    Answer,
    Command,
    debug_internal,
    emit,
    invalid,
    own_action,
    with_next_action,
)
from .schemas import BYTE_CONTRACTS, SCHEMAS, adapter, render_schema, resolve

OK, INVALID = EXIT_DONE, EXIT_INVALID
#: The schema of the answer to an unknown or missing command.
USAGE_SCHEMA: Final = "zeocore.usage/v1"
#: The schema of the answer when zeocore itself failed.
INTERNAL_SCHEMA: Final = "zeocore.internal/v1"
__all__ = [
    "CLI_PROTOCOL",
    "COMMANDS",
    "EXIT_AMBIGUOUS",
    "EXIT_APPROVAL",
    "EXIT_DONE",
    "EXIT_HELD",
    "EXIT_INTERNAL",
    "EXIT_INVALID",
    "EXIT_NOT_PAIRED",
    "EXIT_WAIT",
    "emit",
    "main",
    "run",
]


def _invalid(message: str) -> Answer:
    return invalid(message)


def _version(args: Sequence[str], _stdin: bytes) -> Answer:
    if args:
        return _invalid("version takes no arguments")
    from zeo_core import __version__
    from zeo_core.integrations.hosted.transport import ZEOCONNECT_PROTOCOL_VERSION

    return OK, {
        "ok": True,
        "zeocore": __version__,
        "cli_protocol": CLI_PROTOCOL,
        "zeoconnect_protocol": ZEOCONNECT_PROTOCOL_VERSION,
        "commands": sorted(COMMANDS),
    }


def _schema(args: Sequence[str], _stdin: bytes) -> Answer:
    if list(args) == ["list"]:
        return OK, {"ok": True, "schemas": sorted(SCHEMAS)}
    name = resolve(args[0]) if len(args) == 1 else None
    if name is None:
        return _invalid("schema takes 'list' or one schema name")
    return OK, render_schema(name)


def _validate(args: Sequence[str], stdin: bytes) -> Answer:
    name = resolve(args[0]) if len(args) == 1 else None
    if name is None:
        return _invalid("validate takes one schema name")
    if name in BYTE_CONTRACTS:
        return _validate_bytes_contract(name, stdin)
    args = [name]
    try:
        value = parse_json(stdin)
    except ProtocolError as error:
        return _invalid(str(error))
    validator = adapter(args[0])
    try:
        model = validator.validate_python(value)
    except ValidationError as error:
        declared = _field_names(render_schema(args[0]))
        return EXIT_HELD, {
            "ok": False,
            "outcome": "invalid",
            # loc and type only: a message or input could quote the value. A
            # key that isn't a declared field (one inside a free-form object,
            # or an unknown extra) is the caller's data, so it shows as "*".
            "errors": [
                {
                    "loc": [
                        part if isinstance(part, int) or part in declared else "*"
                        for part in item["loc"]
                    ],
                    "type": item["type"],
                }
                for item in error.errors(include_url=False, include_input=False)
            ],
        }
    return OK, {
        "ok": True,
        "value": validator.dump_python(model, mode="json", by_alias=True),
    }


def _validate_bytes_contract(name: str, stdin: bytes) -> Answer:
    """A contract with byte-level limits: the raw stdin is the document."""
    if name == "library.view-v2":
        from zeo_core.contracts.library.view import errors_of as library_errors
        from zeo_core.contracts.library.view import validate_bytes as library_validate

        if library_errors(stdin):
            return EXIT_HELD, {
                "ok": False,
                "outcome": "invalid",
                "errors": [{"loc": [], "type": "library_view_refused"}],
            }
        return OK, {
            "ok": True,
            "value": library_validate(stdin).model_dump(mode="json", by_alias=True),
        }
    from zeo_core.contracts.ui import errors_of, validate_bytes

    errors = errors_of(stdin)
    if errors:
        declared = _field_names(render_schema(name))
        return EXIT_HELD, {
            "ok": False,
            "outcome": "invalid",
            "errors": [
                {
                    "loc": [
                        part if isinstance(part, int) or part in declared else "*"
                        for part in item["loc"]
                    ],
                    "type": item["type"],
                }
                for item in errors
            ],
        }
    return OK, {
        "ok": True,
        "value": validate_bytes(stdin).model_dump(
            mode="json", by_alias=True, exclude_none=True
        ),
    }


def _field_names(schema: object) -> frozenset[str]:
    """Every property name the schema declares, at any depth."""
    names: set[str] = set()
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                names.update(properties)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return frozenset(names)


def _digest(args: Sequence[str], stdin: bytes) -> Answer:
    if args:
        return _invalid("digest takes no arguments")
    try:
        return OK, {"ok": True, "sha256": digest(parse_json(stdin))}
    except ProtocolError as error:
        return _invalid(str(error))


def _release(args: Sequence[str], stdin: bytes) -> Answer:
    """``zeocore release``, loaded only when it is used.

    It reaches GitHub through ``zeo_core.integrations.github``, whose
    dependencies are the ``github`` extra's. Every other command runs on a
    plain install.
    """
    try:
        from .release import release
    except ModuleNotFoundError as error:
        if (error.name or "").startswith("zeo_core"):
            raise
        return EXIT_HELD, {
            "ok": False,
            "outcome": "extra_missing",
            "message": "zeocore release needs the github extra: "
            "pip install 'zeocore[github]'",
        }
    return release(args, stdin)


def _help(args: Sequence[str], _stdin: bytes) -> Answer:
    # Every answer is JSON already; --json is accepted so the agentic
    # experience standard's spelling works too.
    if list(args) not in ([], ["--json"]):
        return _invalid("help takes no arguments, or --json")
    return OK, catalogue()


#: Every command, named explicitly. Nothing is discovered or loaded by name.
COMMANDS: Final[dict[str, Command]] = {
    "artifact": client.artifact,
    "connections": client.connections,
    "digest": _digest,
    "doctor": doctor.doctor,
    "help": _help,
    "invoke": client.invoke,
    "login": client.login,
    "logout": client.logout,
    "release": _release,
    "whoami": client.whoami,
    "schema": _schema,
    "validate": _validate,
    "version": _version,
}
_READS_STDIN: Final = frozenset({"artifact", "digest", "invoke", "release", "validate"})


def run(argv: Sequence[str], stdin: Callable[[], bytes]) -> Answer:
    if not argv or argv[0] not in COMMANDS:
        status, answer = _invalid(
            own_action(
                "commands: "
                + ", ".join(sorted(COMMANDS))
                + "; run zeocore help for each "
                "command's arguments, stdin, exit codes and result schema"
            )
        )
        return status, {**answer, "schema": USAGE_SCHEMA}
    name, args = argv[0], argv[1:]
    status, answer = COMMANDS[name](args, stdin() if name in _READS_STDIN else b"")
    answer = with_next_action(status, answer)
    if _is_contract_document(name, args, status):
        # A successful `zeocore schema <name>` answers with the contract
        # itself, which names itself by $schema and $id; a key would alter it.
        return status, answer
    # Every other result line names its schema, so an agent can check what
    # it parses. The label comes last, so no answer can override it.
    return status, {**answer, "schema": f"zeocore.{name}/v1"}


def _is_contract_document(name: str, args: Sequence[str], status: int) -> bool:
    """The one answer that is a document, not a result: `schema <name>`."""
    return name == "schema" and status == OK and list(args) != ["list"]


def main(argv: Sequence[str] | None = None) -> None:
    try:
        status, answer = run(
            sys.argv[1:] if argv is None else argv,
            # One byte past the limit is enough to refuse an oversized input.
            lambda: sys.stdin.buffer.read(MAX_BYTES + 1),
        )
    except Exception as error:
        # Never a traceback in place of the answer. The detail could quote
        # input, so stdout gets a fixed message, and stderr gets the
        # exception type and frames only, and only with ZEOCORE_DEBUG=1.
        debug_internal(error)
        status, answer = (
            EXIT_INTERNAL,
            {
                "ok": False,
                "outcome": "internal",
                "message": own_action(
                    "zeocore failed internally; rerun the same command "
                    "with ZEOCORE_DEBUG=1 to print diagnostics on stderr, then report "
                    "the command, the zeocore version and that output"
                ),
                "schema": INTERNAL_SCHEMA,
            },
        )
    emit(answer)
    raise SystemExit(status)


if __name__ == "__main__":
    main()
