"""``zeocore help``: the machine catalogue an agent learns the command from.

Every command, its arguments, what it reads on stdin, the exit family and the
schema its result line names. ``tests/test_cli/test_catalogue.py`` checks this
table against the dispatch table and against every ``add_argument`` in the
command modules, so the catalogue can't drift from the code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

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
)

#: The schema of ``zeocore help --json`` itself.
CATALOGUE_SCHEMA: Final = "zeocore.help/v1"

#: The one exit family every command shares (``cli_protocol`` 1).
EXIT_CODES: Final = (
    (EXIT_DONE, "done", "the command did what was asked"),
    (EXIT_INVALID, "invalid", "invalid input; nothing was sent or changed"),
    (EXIT_APPROVAL, "approval_required", "a person must approve; ask again later"),
    (EXIT_WAIT, "waiting", "still in flight or unavailable; ask the same again later"),
    (EXIT_NOT_PAIRED, "not_paired", "no paired ZEOconnect session: run zeocore login"),
    (
        EXIT_AMBIGUOUS,
        "ambiguous",
        "the outcome is unknown; never retried automatically",
    ),
    (EXIT_HELD, "held", "refused or held, including a value that fails its schema"),
    (
        EXIT_INTERNAL,
        "internal",
        "an error in zeocore itself; for an effect, treat as ambiguous",
    ),
)


@dataclass(frozen=True)
class Argument:
    name: str
    summary: str
    required: bool = False
    takes_value: bool = True
    #: The only values accepted, when the command restricts them.
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Verb:
    """A subcommand that takes one JSON object on stdin, with exactly these fields."""

    name: str
    stdin_fields: tuple[str, ...]


@dataclass(frozen=True)
class Entry:
    name: str
    summary: str
    usage: str
    stdin: str | None = None
    arguments: tuple[Argument, ...] = field(default_factory=tuple)
    verbs: tuple[Verb, ...] = field(default_factory=tuple)

    @property
    def schema(self) -> str:
        return f"zeocore.{self.name}/v1"


_PROFILE = Argument(
    "--profile", "the session profile; the default profile when omitted"
)

#: Every command, named explicitly, in the order ``zeocore help`` prints them.
ENTRIES: Final[tuple[Entry, ...]] = (
    Entry(
        "help",
        "this catalogue; every zeocore answer is JSON already, so --json is "
        "accepted and changes nothing",
        "zeocore help [--json]",
        arguments=(
            Argument(
                "--json", "accepted for the standard's spelling", takes_value=False
            ),
        ),
    ),
    Entry(
        "version",
        "the release, cli_protocol and zeoconnect_protocol",
        "zeocore version",
    ),
    Entry(
        "doctor",
        "is this machine ready? one local, read-only check per line (Python, "
        "session store, pairing, ZEOCONNECT_URL, extras); a failing check "
        "names its next command, and who must act when it isn't you",
        "zeocore doctor [--profile P]",
        arguments=(_PROFILE,),
    ),
    Entry(
        "schema",
        "list the contract schemas, or print one as JSON Schema 2020-12; "
        "`schema <name>` answers with the contract document itself, named by "
        "its $schema and $id, so it carries no 'ok' or 'schema'",
        "zeocore schema list | zeocore schema <name>",
    ),
    Entry(
        "validate",
        "validate stdin against a contract schema; errors never echo input",
        "zeocore validate <name> < value.json",
        stdin="the value to validate, as JSON",
    ),
    Entry(
        "digest",
        "the sha256 of stdin's RFC 8785 canonical bytes",
        "zeocore digest < value.json",
        stdin="any JSON value",
    ),
    Entry(
        "login",
        "pair this profile with ZEOconnect; emits a pairing event, then waits",
        "zeocore login [--profile P] [--device-name NAME]",
        arguments=(
            _PROFILE,
            Argument("--device-name", "how ZEOconnect names this device"),
        ),
    ),
    Entry(
        "logout",
        "forget this profile's session",
        "zeocore logout [--profile P]",
        arguments=(_PROFILE,),
    ),
    Entry(
        "whoami",
        "the paired principal and organization",
        "zeocore whoami [--profile P]",
        arguments=(_PROFILE,),
    ),
    Entry(
        "connections",
        "the connections this principal may use",
        "zeocore connections [--profile P] [--service S]",
        arguments=(_PROFILE, Argument("--service", "only this service's connections")),
    ),
    Entry(
        "invoke",
        "run one ZEOconnect operation; a repeat replays the stored outcome",
        "zeocore invoke <operation_id> --connection C [--occurrence N] "
        "[--wait-approval SECONDS] [--profile P] < arguments.json",
        stdin="the operation's arguments, as a JSON object",
        arguments=(
            Argument(
                "operation_id",
                "the operation, for example hubspot.newsletter.create_draft",
                required=True,
            ),
            Argument("--connection", "the connection to act through", required=True),
            Argument("--occurrence", "a new value is a deliberate new act; default 1"),
            Argument(
                "--wait-approval",
                "block up to SECONDS (1 to 3600) for a person's approval",
            ),
            _PROFILE,
        ),
    ),
    Entry(
        "artifact",
        "download an artifact an operation produced",
        "zeocore artifact get --out PATH [--profile P] < ref.json",
        stdin="the artifact reference, as JSON",
        arguments=(
            Argument("action", "what to do", required=True, choices=("get",)),
            Argument("--out", "where to write the bytes", required=True),
            _PROFILE,
        ),
    ),
    Entry(
        "release",
        "the shared signing step and the PyPI adapter; needs the github extra",
        "zeocore release sign|verify|pack|receive|send|serve < request.json | "
        "zeocore release pypi plan|admit|apply|approve|reconcile|verify < request.json",
        stdin="the step's request, as a JSON object; a wrong field set is refused with "
        "the exact fields it needs",
        verbs=(
            Verb("pack", ("commit", "manifest", "manifest_path", "out", "repo")),
            Verb("receive", ("manifest", "response", "work_dir")),
            Verb("send", ("out", "pack", "service")),
            Verb("serve", ("listen", "state_dir")),
            Verb("sign", ("manifest", "work_dir")),
            Verb("verify", ("manifest", "work_dir")),
            *(
                Verb(f"pypi {stage}", ("manifest",))
                for stage in (
                    "plan",
                    "admit",
                    "apply",
                    "approve",
                    "reconcile",
                    "verify",
                )
            ),
        ),
    ),
)


def catalogue() -> dict[str, Any]:
    """The whole catalogue as one JSON-ready document."""
    from zeo_core import __version__

    return {
        "ok": True,
        "schema": CATALOGUE_SCHEMA,
        "zeocore": __version__,
        "cli_protocol": CLI_PROTOCOL,
        "protocol": {
            "stdout": "JSON lines only; events carry 'event', and the last line "
            "is the result, carrying 'ok' and 'schema', except a successful "
            "'schema <name>', which is the contract document itself",
            "stdin": "strict JSON: no duplicate keys, no NaN, at most 1 MiB",
            "errors": "never echo input values",
        },
        "exit_codes": [
            {"code": code, "name": name, "meaning": meaning}
            for code, name, meaning in EXIT_CODES
        ],
        "commands": [
            {
                "name": entry.name,
                "summary": entry.summary,
                "usage": entry.usage,
                "stdin": entry.stdin,
                "arguments": [
                    {
                        "name": argument.name,
                        "summary": argument.summary,
                        "required": argument.required,
                        "takes_value": argument.takes_value,
                        "choices": list(argument.choices),
                    }
                    for argument in entry.arguments
                ],
                "verbs": [
                    {"name": verb.name, "stdin_fields": list(verb.stdin_fields)}
                    for verb in entry.verbs
                ],
                "result_schema": entry.schema,
            }
            for entry in ENTRIES
        ],
    }
