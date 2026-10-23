"""CONTRIBUTING.md is written for outside contributors; AGENTS.md links it.

These tests check headings and strings only, not facts. The seat charter (the org's
internal contributors charter) is kept in the org records, not in this repository.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHARTER = REPO_ROOT / "CONTRIBUTING.md"
AGENTS = REPO_ROOT / "AGENTS.md"

SECTIONS = (
    "Before you start",
    "What is welcome",
    "How to build",
    "How to submit",
)

#: Words that belong to the internal seat charter, not to a public file.
INTERNAL_WORDS = ("zmsg", "ZeoWork", "the operator", "zeomerger", "the Architect")


def _text() -> str:
    return CHARTER.read_text(encoding="utf-8")


def test_the_guide_has_the_outsider_sections_in_order() -> None:
    headings = re.findall(r"^## (.+?)\s*$", _text(), flags=re.MULTILINE)
    assert headings == list(SECTIONS)


def test_the_guide_starts_from_agents_md() -> None:
    intro = _text().split("\n## ", maxsplit=1)[0]
    assert "[AGENTS.md](AGENTS.md)" in intro


def test_the_guide_says_org_seats_follow_an_internal_charter() -> None:
    assert "Org seats follow an internal charter" in _text()


def test_agents_md_links_the_guide() -> None:
    assert "](CONTRIBUTING.md)" in AGENTS.read_text(encoding="utf-8")


def test_the_guide_names_no_internal_roles_or_machines() -> None:
    text = _text()
    for word in INTERNAL_WORDS:
        assert word not in text, word


def test_the_guide_covers_vulnerabilities_and_secrets() -> None:
    text = _text()
    assert "[SECURITY.md](SECURITY.md)" in text
    assert "Never put a key, token or secret" in text
