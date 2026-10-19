"""``hidden_report``: the body-free reason a hidden-text rule was or was not narrowed.

zeocore-SOW-41. The report changes no decision (it must agree with the cleaning
counts) and carries no text of the document (only fixed codes and fixed tag names).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from zeo_core.integrations.pandoc import _noise, newsletter
from zeo_core.integrations.pandoc._runner import Deadline
from zeo_core.integrations.pandoc.diagnostic import hidden_report

SHRUNK = "max-height:0;overflow:visible"

CASES: dict[str, str] = {
    "clean-sheet-narrowed": (
        f"<style>.a{{{SHRUNK};display:inline}}</style><span class='a'>SENTINEL</span>"
    ),
    "quote-anywhere": (
        "<style>.f{font-family:'Helvetica Neue'}.a{max-height:0;overflow:visible;"
        "display:inline}</style><span class='a'>SENTINEL</span>"
    ),
    "comment-anywhere": (
        "<style>/* x */.a{max-height:0;overflow:visible;display:inline}</style>"
        "<span class='a'>SENTINEL</span>"
    ),
    "link-element": (
        "<link rel='stylesheet' href='https://example.com/a.css'>"
        f"<span style='{SHRUNK};display:inline'>SENTINEL</span>"
    ),
    "import": (
        "<style>@import 'x.css';</style>"
        f"<span style='{SHRUNK};display:inline'>SENTINEL</span>"
    ),
    "not-proven-inline": "<div style='max-height:0;overflow:hidden'>SENTINEL</div>",
    "plain-hidden-attr": "<p hidden>SENTINEL</p>",
    "display-none": "<p style='display:none'>SENTINEL</p>",
    "weird-tag": "<custom-SENTINEL style='max-height:0;overflow:hidden'>x</custom-SENTINEL>",
}


def _counts(markup: str) -> dict[str, int]:
    noise = _noise.load()
    _soup, counts, *_rest = newsletter._clean(markup, noise, Deadline(30))
    return {k: v for k, v in counts.items() if k.startswith("hidden_") and v}


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_report_agrees_with_the_cleaning_counts(name: str) -> None:
    report = hidden_report(CASES[name])
    assert {f"hidden_{k}": v for k, v in report["hidden"].items()} == _counts(
        CASES[name]
    )


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_report_holds_no_text_of_the_document(name: str) -> None:
    dump = json.dumps(hidden_report(CASES[name])).lower()
    assert "sentinel" not in dump
    assert "custom" not in dump
    assert "helvetica" not in dump


def test_a_sheet_that_can_be_modelled_narrows_and_reports_no_gate() -> None:
    report = hidden_report(CASES["clean-sheet-narrowed"])
    assert report["hidden"] == {}
    assert report["gates"] == {}
    assert report["sheet_unmodelled"] == {}
    assert report["style_blocks"] == 1


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("quote-anywhere", "sheet_quote"),
        ("comment-anywhere", "sheet_comment"),
        ("link-element", "document_link"),
        ("import", "style_import"),
    ],
)
def test_an_unmodelled_sheet_names_its_reason_and_the_gate(
    name: str, reason: str
) -> None:
    report = hidden_report(CASES[name])
    assert reason in report["sheet_unmodelled"]
    assert report["gates"] == {"max_size:unmodelled_sheet": 1}


def test_an_element_read_but_not_proven_names_the_missing_proof() -> None:
    report = hidden_report(CASES["not-proven-inline"])
    assert report["gates"] == {"max_size:no_proof(no_clip+inline+table_part)": 1}
    assert report["gate_tags"] == {
        "max_size:no_proof(no_clip+inline+table_part):div": 1
    }


def test_a_kind_that_never_narrows_reports_no_gate() -> None:
    assert hidden_report(CASES["plain-hidden-attr"])["hidden"] == {"attribute": 1}
    assert hidden_report(CASES["display-none"])["gates"] == {}


def test_an_unlisted_tag_is_reported_as_other() -> None:
    report = hidden_report(CASES["weird-tag"])
    assert list(report["gate_tags"]) == [
        "max_size:no_proof(no_clip+inline+table_part):other"
    ]


def test_bytes_are_decoded_like_the_converter() -> None:
    data: Any = CASES["quote-anywhere"].encode()
    assert hidden_report(data) == hidden_report(CASES["quote-anywhere"])


def test_the_module_imports_nothing_that_needs_pandoc() -> None:
    source = Path(newsletter.__file__).with_name("diagnostic.py").read_text("utf-8")
    assert "run_pandoc" not in source
