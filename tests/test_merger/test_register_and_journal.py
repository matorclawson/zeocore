"""The register is read strictly; the journal is durable and refuses damage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zeo_core.merger import (
    ATTEMPTED,
    FileJournal,
    Receipt,
    RegisterError,
    parse_register,
)

HEAD = "c6a0b8cb1e9ad9aa6b3115bbf49f2fdc135568b0"
BASE = "3a06f60de5316d29c581d651ef06b1c966374ae8"


def _ready(**overrides: object) -> str:
    event: dict[str, object] = {
        "v": 1,
        "event": "ready",
        "time": "2026-10-10T15:40:55Z",
        "repo": "zeroemployeeorg/zeocore",
        "repo_id": 1337139177,
        "pr": 85,
        "head": HEAD,
        "head_ref": "b",
        "base_ref": "main",
        "base_sha_at_review": BASE,
        "author_seat": "zeocore",
        "after": [],
        "receipt_id": f"apr1-1337139177-85-{HEAD}",
        "approved_by": "architect",
    }
    event.update(overrides)
    return json.dumps({k: v for k, v in event.items() if v is not _DROP})


_DROP = object()


def test_a_ready_entry_is_a_candidate() -> None:
    (candidate,) = parse_register(_ready()).candidates()
    assert (candidate.repo, candidate.pr, candidate.head) == (
        "zeroemployeeorg/zeocore",
        85,
        HEAD,
    )
    assert candidate.blocked == ()


@pytest.mark.parametrize(
    "line",
    [
        " ",
        "not json",
        "[1]",
        _ready(extra="x"),
        _ready(v=2),
        _ready(event="approved"),
        _ready(receipt_id=_DROP),
        _ready(head="abc"),
        _ready(receipt_id="apr1-1-85-" + HEAD),
        _ready(after=["not a pr"]),
        _ready(pr=0),
        _ready(pr=True),
        '{"v":1,"v":1}',
    ],
)
def test_one_bad_line_refuses_the_whole_register(line: str) -> None:
    with pytest.raises(RegisterError):
        parse_register(_ready() + "\n" + line)


def test_a_later_event_replaces_ready_and_after_entries_block() -> None:
    withdrawn = json.dumps(
        {
            "v": 1,
            "event": "withdrawn",
            "time": "t",
            "repo": "zeroemployeeorg/zeocore",
            "pr": 85,
            "head": HEAD,
            "reason": "changes requested",
        }
    )
    assert parse_register(_ready() + "\n" + withdrawn).candidates() == []
    (blocked,) = parse_register(
        _ready(after=["zeroemployeeorg/zeocore#84"])
    ).candidates()
    assert blocked.blocked == ("zeroemployeeorg/zeocore#84",)


def _receipt(action: str) -> Receipt:
    return Receipt(
        v=1,
        repo="zeroemployeeorg/zeocore",
        pr=85,
        head=HEAD,
        base_ref="main",
        receipt_id=f"apr1-1337139177-85-{HEAD}",
        register_line=1,
        action=action,
    )


def test_an_intent_survives_a_reopen(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = FileJournal(path)
    journal.append(_receipt(ATTEMPTED))
    journal.close()
    assert FileJournal(path).attempted(f"apr1-1337139177-85-{HEAD}")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    record = json.loads(path.read_text().splitlines()[0])
    assert record["action"] == "attempted" and "merge_commit" not in record


def test_a_damaged_journal_refuses_to_open(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    path.write_text('{"receipt_id": "x", "action": "attempted"}\nnot json\n')
    with pytest.raises(ValueError, match="line 2"):
        FileJournal(path)
