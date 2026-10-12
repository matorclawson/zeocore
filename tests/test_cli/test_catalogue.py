"""``zeocore help``: the machine catalogue, held to the code it describes."""

from __future__ import annotations

import ast
import inspect

import pytest

from zeo_core.cli import catalogue as catalogue_module
from zeo_core.cli import client, protocol
from zeo_core.cli.__main__ import COMMANDS, run
from zeo_core.cli.catalogue import CATALOGUE_SCHEMA, ENTRIES, EXIT_CODES
from zeo_core.cli.schemas import SCHEMAS


def _call(*argv: str, stdin: bytes = b"{}") -> tuple[int, dict[str, object]]:
    return run(argv, lambda: stdin)


@pytest.mark.parametrize("argv", [("help",), ("help", "--json")])
def test_help_and_help_json_answer_the_catalogue(argv: tuple[str, ...]) -> None:
    status, answer = _call(*argv)
    assert status == 0
    assert answer["schema"] == CATALOGUE_SCHEMA
    assert answer["ok"] is True
    commands = answer["commands"]
    assert isinstance(commands, list)
    assert [c["name"] for c in commands] == [e.name for e in ENTRIES]


def test_help_refuses_other_arguments() -> None:
    status, answer = _call("help", "--verbose")
    assert (status, answer["outcome"]) == (2, "invalid_request")


def test_the_catalogue_names_exactly_the_dispatched_commands() -> None:
    assert sorted(e.name for e in ENTRIES) == sorted(COMMANDS)


def test_the_catalogue_lists_every_exit_code_the_protocol_defines() -> None:
    defined = {
        value
        for name, value in vars(protocol).items()
        if name.startswith("EXIT_") and isinstance(value, int)
    }
    assert {code for code, _, _ in EXIT_CODES} == defined
    assert len({name for _, name, _ in EXIT_CODES}) == len(EXIT_CODES)


def _declared_arguments(function: object) -> set[str]:
    """Every name the function passes to ``add_argument``."""
    tree = ast.parse(inspect.getsource(function))  # type: ignore[arg-type]
    return {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }


@pytest.mark.parametrize(
    "name", ["login", "logout", "whoami", "connections", "invoke", "artifact"]
)
def test_each_client_commands_arguments_match_its_parser(name: str) -> None:
    """A flag added to or removed from a command must change the catalogue."""
    entry = next(e for e in ENTRIES if e.name == name)
    assert {a.name for a in entry.arguments} == _declared_arguments(
        getattr(client, name)
    )


def test_commands_that_read_stdin_say_what_they_read() -> None:
    from zeo_core.cli.__main__ import _READS_STDIN

    assert {e.name for e in ENTRIES if e.stdin is not None} == set(_READS_STDIN)


@pytest.mark.parametrize(
    "argv", [("version",), ("schema", "list"), ("digest",), ("whoami",)]
)
def test_every_result_names_its_command_schema(argv: tuple[str, ...]) -> None:
    _, answer = _call(*argv)
    assert answer["schema"] == f"zeocore.{argv[0]}/v1"


def test_an_unknown_command_points_at_help() -> None:
    status, answer = _call("nope")
    assert status == 2
    assert answer["schema"] == "zeocore.usage/v1"
    assert "zeocore help" in str(answer["message"])


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_a_contract_document_is_answered_unaltered(name: str) -> None:
    # `zeocore schema <name>` answers with the JSON Schema itself.
    _, answer = _call("schema", name)
    assert "schema" not in answer
    assert "$schema" in answer


def test_the_catalogue_module_has_no_command_logic() -> None:
    # It describes; dispatch stays in __main__.COMMANDS.
    assert not hasattr(catalogue_module, "COMMANDS")
