"""Zeocore's contributors charter keeps the shared shape, and AGENTS.md links it.

These tests check headings and strings only, not facts: the facts in the charter
(paths, commands, job names) are checked by review against the repository.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHARTER = REPO_ROOT / "CONTRIBUTING.md"
AGENTS = REPO_ROOT / "AGENTS.md"

#: The six sections of the shared charter shape, in order.
SECTIONS = (
    "How to propose",
    "What is welcome",
    "Owner-only areas",
    "How to build",
    "How to submit",
    "Response times",
)


def _h2_headings(text: str) -> list[str]:
    return re.findall(r"^## (.+?)\s*$", text, flags=re.MULTILINE)


def test_the_charter_has_the_six_sections_in_order() -> None:
    assert _h2_headings(CHARTER.read_text(encoding="utf-8")) == list(SECTIONS)


def test_the_charter_starts_from_agents_md() -> None:
    intro = CHARTER.read_text(encoding="utf-8").split("\n## ", maxsplit=1)[0]
    assert "[AGENTS.md](AGENTS.md)" in intro


def test_agents_md_links_the_charter() -> None:
    assert "](CONTRIBUTING.md)" in AGENTS.read_text(encoding="utf-8")


def test_the_charter_states_its_response_times() -> None:
    text = CHARTER.read_text(encoding="utf-8")
    assert "Within 4 hours" in text
    assert "Within 1 working day" in text


def test_the_charter_names_no_feature_main_lacks() -> None:
    """Facts only: these arrive in later PRs and must not be promised here."""
    text = CHARTER.read_text(encoding="utf-8")
    for absent in ("ZEO_WORK_DIR", ".githooks", "pypi wait"):
        assert absent not in text, absent
