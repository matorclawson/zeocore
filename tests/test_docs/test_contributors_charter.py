"""Zeocore's contributors charter keeps the shared skeleton, and AGENTS.md links it.

These tests check headings and strings only, not facts: the facts in the charter
(paths, commands, job names) are checked by review against the repository. The
shared lines come from the Architect's charter skeleton (v1, 2026-10-14).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHARTER = REPO_ROOT / "CONTRIBUTING.md"
AGENTS = REPO_ROOT / "AGENTS.md"

#: The eight sections of the shared skeleton, in order.
SECTIONS = (
    "Who owns this, and the ruling",
    "How to propose",
    "What is welcome",
    "Owner-only areas",
    "How to build",
    "How to submit",
    "Response times",
    "Escalation",
)

#: Lines the skeleton gives verbatim.
SHARED_LINES = (
    "Fork `matorclawson/zeocore`, branch `<your-seat>/<topic>-<yyyymmdd>`, where "
    "`<your-seat>` is the authoring seat's zmsg name",
    "this is the lane form; anything else is refused.",
    "Until the A1 seat-list install, only the owner seat's PRs are admitted on this "
    "repository",
    "Route: owner, then council, then zeomerger. Until zeomerger is live for that "
    "repo, the Architect sequences the merge. The operator never merges, and is "
    "never asked.",
    "Every dependency, new tool or pin goes through the Architect. "
    "Never ask the operator.",
    "whose first line is `head: <full sha>`",
    "A missed time goes to the Architect by zmsg.",
)


def _text() -> str:
    return CHARTER.read_text(encoding="utf-8")


def _h2_headings(text: str) -> list[str]:
    return re.findall(r"^## (.+?)\s*$", text, flags=re.MULTILINE)


def test_the_charter_has_the_eight_sections_in_order() -> None:
    assert _h2_headings(_text()) == list(SECTIONS)


def test_the_charter_starts_from_agents_md() -> None:
    intro = _text().split("\n## ", maxsplit=1)[0]
    assert "[AGENTS.md](AGENTS.md)" in intro


def test_agents_md_links_the_charter() -> None:
    assert "](CONTRIBUTING.md)" in AGENTS.read_text(encoding="utf-8")


def test_the_charter_uses_the_skeletons_shared_lines() -> None:
    text = _text()
    for line in SHARED_LINES:
        assert line in text, line


def test_the_charter_names_the_ruling_and_the_proposal_form() -> None:
    text = _text()
    assert "0cc5d7bf8714d6c02a8533ca3867f4a16bda02066797f67dc83001d6da90ec45" in text
    assert "`PROPOSAL:`" in text
    for answer in ("AGREE", "AMEND", "DECLINE"):
        assert answer in text, answer


def test_the_charter_states_its_response_times() -> None:
    text = _text()
    assert "Within 4 hours" in text
    assert "Within 1 working day" in text


def test_the_operator_neither_merges_nor_is_asked() -> None:
    text = _text()
    assert "operator merges" not in text
    assert "hand-merge" not in text
    assert "operator's OK" not in text


def test_the_charter_names_no_feature_main_lacks() -> None:
    """Facts only: these arrive in later PRs and must not be promised here."""
    text = _text()
    for absent in ("ZEO_WORK_DIR", ".githooks", "pypi wait"):
        assert absent not in text, absent
