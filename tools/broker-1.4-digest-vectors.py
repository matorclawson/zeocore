"""Write tests/fixtures/broker-1.4/digest-vectors.json: zeocore's parity fixtures.

ZEOCORE-SOW-27 §3: zeocore supplies the language-neutral part of the Broker
client that ZEO-RT's Go door implements, so the two can't drift. For one
request per Broker contract 1.4.0 operation, plus ``on_behalf_of`` variants,
each vector holds the request, the bytes the digest covers (RFC 8785, over
``connection_id``, ``operation_id``, ``arguments`` and ``on_behalf_of``,
never ``door_request_id`` or ``idempotency_key``) and their sha256.

Every request is validated against the frozen 1.4.0 schemas before it is
written. Run from the repository root:

    .venv/bin/python tools/broker-1.4-digest-vectors.py
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from zeo_core.adapters.runtime_host.canonical import canonical_bytes

ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "broker-1.4"
DIGESTED = ("connection_id", "operation_id", "arguments", "on_behalf_of")
SHA = "0123456789abcdef0123456789abcdef01234567"
REPO = "example-org/newsroom"
SEAT: dict[str, Any] = {
    "principal_id": "prn_example",
    "incarnation": 3,
    "authenticated_as": {
        "host_id": "host_example",
        "host_incarnation": 2,
        "os_account": "za-example",
        "account_incarnation": 1,
    },
}

ARGUMENTS: dict[str, dict[str, Any]] = {
    "github.git.publish": {
        "repository": REPO,
        "ref": "refs/heads/zeonewsroom/example",
        "expected_old_sha": None,
        "parents": [SHA],
        "changes": [
            {"path": "README.md", "mode": "100644", "content_base64": "aGVsbG8K"},
            {"path": "old.txt", "delete": True},
        ],
        "message": "Example ü",
        "author": {"name": "Example", "email": "seat@example.com"},
    },
    "github.git.reconcile": {"execution_id": "exe_fixture_0001"},
    "github.intent.resolve": {
        "idempotency_key": "fixture-0001-abcdef-0123456789abcd",
        "request_sha256": (
            "3c04a8b0551bdb180d2b5fc5e1364bacdb23a9af1386b953af3b8085fe6f729f"
        ),
    },
    "github.pr.open": {
        "repository": REPO,
        "head": "zeonewsroom/example",
        "head_sha": SHA,
        "title": "Example",
        "body": "Fixture body ü",
        "draft": False,
    },
    "github.pr.update": {"repository": REPO, "number": 7, "title": "Example"},
    "github.contents.read": {
        "repository": REPO,
        "path": "docs/index.md",
        "ref": "refs/heads/main",
    },
    "github.pr.read": {"repository": REPO, "number": 7, "include": ["files"]},
    "github.checks.read": {"repository": REPO, "sha": SHA},
    "github.pr.list": {"repository": REPO, "state": "open"},
    "github.ref.read": {"repository": REPO, "ref": "refs/heads/main"},
    "github.fork.sync": {"repository": REPO},
}


def _request(operation_id: str, on_behalf_of: dict[str, Any]) -> dict[str, Any]:
    return {
        "connection_id": "role_zeonewsroom_publish",
        "operation_id": operation_id,
        "arguments": ARGUMENTS[operation_id],
        "idempotency_key": "fixture-0001-abcdef-0123456789abcd",
        "on_behalf_of": on_behalf_of,
        "door_request_id": "door-req-0001",
    }


def _variants() -> list[tuple[str, dict[str, Any]]]:
    """The pr.open request, changing one on_behalf_of field at a time."""
    another_seat = {**copy.deepcopy(SEAT), "principal_id": "prn_zeocreator"}
    another_incarnation = {**copy.deepcopy(SEAT), "incarnation": 4}
    another_account = copy.deepcopy(SEAT)
    another_account["authenticated_as"] = {
        **SEAT["authenticated_as"],
        "account_incarnation": 2,
    }
    another_host = copy.deepcopy(SEAT)
    another_host["authenticated_as"] = {
        **SEAT["authenticated_as"],
        "host_id": "host_eng-uk-0006",
    }
    return [
        ("on_behalf_of: another seat", another_seat),
        ("on_behalf_of: another incarnation", another_incarnation),
        ("on_behalf_of: another account incarnation", another_account),
        ("on_behalf_of: another host", another_host),
    ]


def _validator() -> Draft202012Validator:
    schemas = json.loads((ROOT / "schemas.json").read_text())
    registry = Registry().with_resources(
        (schema_id, Resource.from_contents(schema, default_specification=DRAFT202012))
        for schema_id, schema in schemas.items()
    )
    return Draft202012Validator(schemas["zeoconnect:1.4:request"], registry=registry)


def vector(name: str, request: dict[str, Any]) -> dict[str, Any]:
    covered = {key: request[key] for key in DIGESTED}
    raw = canonical_bytes(covered)
    return {
        "name": name,
        "request": request,
        "canonical": raw.decode("utf-8"),
        "canonical_bytes": len(raw),
        "request_sha256": hashlib.sha256(raw).hexdigest(),
    }


def main() -> None:
    validator = _validator()
    vectors = [vector(op, _request(op, SEAT)) for op in ARGUMENTS]
    vectors += [
        vector(name, _request("github.pr.open", obo)) for name, obo in _variants()
    ]
    for item in vectors:
        validator.validate(item["request"])
    digests = [item["request_sha256"] for item in vectors]
    if len(set(digests)) != len(digests):
        raise SystemExit("two vectors share a digest")
    document = {
        "contract": "Broker contract 1.4.0",
        "digest": (
            "sha256 over RFC 8785 of connection_id, operation_id, arguments"
            " and on_behalf_of"
        ),
        "excluded": ["door_request_id", "idempotency_key"],
        "producer": "zeocore, tools/broker-1.4-digest-vectors.py",
        "vectors": vectors,
    }
    text = json.dumps(document, indent=1, ensure_ascii=False) + "\n"
    (ROOT / "digest-vectors.json").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
