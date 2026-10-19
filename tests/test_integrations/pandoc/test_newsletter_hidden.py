"""What the converter decides about hidden text, and what it only reports (SOW-41).

Decided from the element alone: the ``hidden`` attribute and its own inline
``display:none`` or ``visibility:hidden``. Everything that needs CSS beyond that is NOT
evaluated: the text is kept and ``diagnostics`` says what was present. These tests need
no pandoc: they run the cleaning step.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from zeo_core.integrations.pandoc import _cleaning, _hidden, _noise, newsletter
from zeo_core.integrations.pandoc._runner import Deadline
from zeo_core.integrations.pandoc.newsletter import ConvertedItem


def _clean(markup: str) -> tuple[str, dict[str, int], dict[str, int]]:
    soup, counts, diagnostics, *_rest = newsletter._clean(
        markup, _noise.load(), Deadline(30)
    )
    return soup.get_text(), counts, diagnostics


def _nonzero(values: dict[str, int]) -> dict[str, int]:
    return {k: v for k, v in values.items() if v}


# --- the regression: an unrelated stylesheet must not change an element's fate -------

_ELEMENT = '<p style="max-height:0">TEXT</p>'


@pytest.mark.parametrize(
    "markup",
    [
        _ELEMENT,
        f"<style>/* c */ p{{margin:0}}</style>{_ELEMENT}",
        f"<style>td p{{display:block}}</style>{_ELEMENT}",
        f'<style>b{{font-family:"A"}}</style>{_ELEMENT}',
    ],
)
def test_the_same_element_is_kept_with_or_without_a_stylesheet(markup: str) -> None:
    text, counts, diagnostics = _clean(markup)
    assert "TEXT" in text
    assert counts["hidden"] == 0
    assert diagnostics["inline_max_size"] == 1  # reported, not decided


# --- decided from the element alone ---------------------------------------------------


@pytest.mark.parametrize(
    ("markup", "kind"),
    [
        ("<p hidden>TEXT</p>", "attribute"),
        ('<p hidden="">TEXT</p>', "attribute"),
        ('<p style="display:none">TEXT</p>', "display"),
        ('<p style="DISPLAY : None">TEXT</p>', "display"),
        ('<p style="display:none !important">TEXT</p>', "display"),
        ('<p style="color:red;display:none;margin:0">TEXT</p>', "display"),
        ('<p style="display:block;display:none">TEXT</p>', "display"),
        ('<p style="visibility:hidden">TEXT</p>', "visibility"),
        ('<p style="visibility:collapse">TEXT</p>', "visibility"),
        ('<div style="display:none"><p>TEXT</p></div>', "display"),
        ('<table><tr><td style="display:none">TEXT</td></tr></table>', "display"),
    ],
)
def test_what_the_html_itself_marks_not_rendered_is_dropped(
    markup: str, kind: str
) -> None:
    text, counts, _diagnostics = _clean(markup)
    assert "TEXT" not in text
    assert counts["hidden"] == 1
    assert counts[f"hidden_{kind}"] == 1


@pytest.mark.parametrize(
    "markup",
    [
        '<p style="display:none;display:block">TEXT</p>',
        '<p style="visibility:hidden;visibility:visible">TEXT</p>',
        '<p style="display:inline-block">TEXT</p>',
        '<p style="mso-hide:all">TEXT</p>',
        '<p style="x-display:none">TEXT</p>',
        '<p data-style="display:none">TEXT</p>',
        '<p class="display:none">TEXT</p>',
    ],
)
def test_text_is_kept_where_the_element_does_not_declare_it_hidden(
    markup: str,
) -> None:
    text, counts, _diagnostics = _clean(markup)
    assert "TEXT" in text
    assert counts["hidden"] == 0


# --- reported, not decided ------------------------------------------------------------


@pytest.mark.parametrize(
    ("style", "key"),
    [
        ("opacity:0", "inline_opacity"),
        ("font-size:0", "inline_font_size"),
        ("max-height:0;overflow:hidden", "inline_max_size"),
        ("width:0;height:0", "inline_zero_size"),
        ("position:absolute;left:-9999px", "inline_offscreen"),
        ("position:absolute;clip:rect(0,0,0,0)", "inline_clip"),
    ],
)
def test_a_technique_in_a_kept_elements_style_is_counted_and_the_text_kept(
    style: str, key: str
) -> None:
    text, counts, diagnostics = _clean(f'<div style="{style}">TEXT</div>')
    assert "TEXT" in text
    assert counts["hidden"] == 0
    assert _nonzero(diagnostics) == {key: 1}


def test_a_hidden_element_is_decided_not_also_reported() -> None:
    _text, counts, diagnostics = _clean('<p style="display:none;font-size:0">TEXT</p>')
    assert counts["hidden_display"] == 1
    assert _nonzero(diagnostics) == {}


def test_class_and_id_rules_are_not_evaluated_but_are_reported() -> None:
    markup = (
        "<style>.pre{display:none}#x{visibility:hidden}.z{opacity:0}</style>"
        '<p class="pre">A</p><p id="x">B</p><p class="z">C</p>'
    )
    text, counts, diagnostics = _clean(markup)
    assert all(letter in text for letter in "ABC")
    assert counts["hidden"] == 0
    assert _nonzero(diagnostics) == {
        "stylesheet_blocks": 1,
        "sheet_display": 1,
        "sheet_visibility": 1,
        "sheet_opacity": 1,
    }


def test_links_imports_media_and_scan_limits_are_reported() -> None:
    markup = (
        '<link rel="stylesheet" href="https://example.com/a.css">'
        "<style>@import url(x.css);@media print{p{display:none}}</style>"
        '<style media="print">p{display:none}</style><p>TEXT</p>'
    )
    text, _counts, diagnostics = _clean(markup)
    assert "TEXT" in text
    assert _nonzero(diagnostics) == {
        "stylesheet_blocks": 2,
        "stylesheet_links": 1,
        "stylesheet_imports": 1,
        "stylesheet_media": 2,
        "sheet_display": 2,
    }


def test_a_style_attribute_over_the_bound_is_kept_and_reported_not_parsed() -> None:
    pad = "x:y;" * (_hidden.STYLE_MAX_CHARS // 4 + 1)
    text, counts, diagnostics = _clean(f'<p style="{pad}display:none">TEXT</p>')
    assert "TEXT" in text
    assert counts["style_oversize"] == 1
    assert counts["hidden"] == 0
    assert _nonzero(diagnostics) == {"inline_style_unparsed": 1}


def test_the_sheet_scan_is_bounded_and_says_when_it_stopped() -> None:
    rule = "p{display:none}"
    markup = f"<style>{rule * (_hidden.SHEET_SCAN_CHARS // len(rule) + 50)}</style>"
    _text, _counts, diagnostics = _clean(markup)
    assert diagnostics["stylesheet_scan_truncated"] == 1
    assert diagnostics["sheet_display"] <= _hidden.SHEET_SCAN_CHARS // len(rule) + 1


# --- the diagnostics are required, complete and named in the data file ---


def test_every_diagnostic_key_is_present_even_when_zero() -> None:
    _text, _counts, diagnostics = _clean("<p>plain</p>")
    assert set(diagnostics) == set(_hidden.DIAGNOSTIC_KEYS)
    assert all(value == 0 for value in diagnostics.values())


def test_diagnostics_is_a_required_field_of_the_result() -> None:
    fields = ConvertedItem.__dataclass_fields__["diagnostics"]
    assert fields.default is fields.default_factory  # both MISSING: no default at all


def test_the_techniques_are_exactly_those_the_data_file_names() -> None:
    names = tuple(kind for kind, _pattern in _noise.load().hidden_patterns)
    assert names == _hidden.TECHNIQUES
    raw = json.loads(
        Path(_noise.__file__)
        .with_name("data")
        .joinpath("newsletter_noise.json")
        .read_text()
    )
    assert [h["kind"] for h in raw["hidden_style_patterns"]] == list(_hidden.TECHNIQUES)


def test_the_hidden_counts_are_exactly_the_decided_kinds() -> None:
    keys = {k for k in _cleaning.COUNT_KEYS if k.startswith("hidden_")}
    assert keys == {f"hidden_{kind}" for kind in _hidden.HIDDEN_KINDS}


def test_the_module_reads_no_class_or_id_rule() -> None:
    source = Path(_hidden.__file__).read_text()
    assert not re.search(r"def (css_rules|style_rules|_keep|_rule_texts)\b", source)


def test_sheet_scanning_checks_the_deadline_per_block() -> None:
    class _Counting(Deadline):
        def __init__(self) -> None:
            super().__init__(30)
            self.checks = 0

        def check(self) -> None:
            self.checks += 1
            super().check()

    bs4 = _cleaning.bs4_module()
    soup = bs4.BeautifulSoup("<style>p{x:y}</style>" * 50, "html.parser")
    room = _Counting()
    _hidden.note_sheets(soup, _noise.load(), _hidden.new_diagnostics(), room)
    assert room.checks >= 50


# --- the council's findings on #153 (a48a2c93) ----------------------------------------


@pytest.mark.parametrize(
    "style",
    [
        "display:none/**/",
        "display:/**/none",
        "disp\\lay:none",
        "display:none!important;display:block",
        "display:block!important;display:none",
        "display:none;display:bogus",
        "visibility:hidden/**/",
        "visibility:/**/hidden",
        "visib\\ility:hidden",
        "visibility:hidden!important;visibility:visible",
        "visibility:hidden;visibility:bogus",
    ],
)
def test_an_element_local_hidden_form_is_dropped_or_reported_never_silent(
    style: str,
) -> None:
    """Zero diagnostics means none was seen: no hidden form may keep text silently."""
    text, counts, diagnostics = _clean(f'<p style="{style}">TEXT</p>')
    assert counts["hidden"] == 1 or _nonzero(diagnostics), (text, style)


@pytest.mark.parametrize(
    ("style", "dropped"),
    [
        ("display:none!important;display:block", True),
        ("display:block!important;display:none", False),
        ("display:none;display:bogus", False),
        ("display:none;display:-webkit-box", False),
        ("display:none;display:-moz-box", False),
        ("display:none;display:block flow", False),
        ("display:none;display:env(x)", False),
        ("display:block!important;display:none!important", True),
        ("display:none!important;display:block!important", False),
        ("display:none;display:var(--x)", False),
        ("display:none;display:inherit", False),
        ("display:bogus", False),
        ("visibility:hidden!important;visibility:visible", True),
        ("visibility:visible!important;visibility:hidden", False),
        ("visibility:hidden;visibility:bogus", False),
        ("visibility:visible!important;visibility:hidden!important", True),
        ("visibility:collapse;visibility:inherit", False),
    ],
)
def test_precedence_and_invalid_values_follow_the_browser(
    style: str, dropped: bool
) -> None:
    text, counts, _diagnostics = _clean(f'<p style="{style}">TEXT</p>')
    assert (counts["hidden"] == 1) is dropped
    assert ("TEXT" not in text) is dropped


@pytest.mark.parametrize(
    ("style", "key"),
    [
        ("display:none/**/", "inline_style_unparsed"),
        ("display:/**/none", "inline_style_unparsed"),
        ("disp\\lay:none", "inline_style_unparsed"),
        ("display:/**/none", "inline_display"),
        ("display:block!important;display:none", "inline_display"),
        ("display:none;display:var(--x)", "inline_display"),
        ("visibility:/**/hidden", "inline_visibility"),
        ("visibility:visible!important;visibility:hidden", "inline_visibility"),
    ],
)
def test_an_undecided_hidden_form_is_counted_where_it_is_named(
    style: str, key: str
) -> None:
    _text, counts, diagnostics = _clean(f'<p style="{style}">TEXT</p>')
    assert counts["hidden"] == 0
    assert diagnostics[key] >= 1


def test_a_visible_child_of_a_dropped_hidden_parent_is_counted() -> None:
    markup = (
        '<div style="visibility:hidden"><p style="visibility:visible">A</p>'
        '<p style="visibility:visible">B</p><p>C</p></div>'
    )
    text, counts, diagnostics = _clean(markup)
    assert counts["hidden_visibility"] == 1
    assert "A" not in text  # the documented limit: the parent takes them with it
    assert diagnostics["visible_descendant_dropped"] == 2


def test_a_first_declaration_in_a_rule_is_seen_in_the_sheet_counts() -> None:
    sheet = ".a{width:0}.b{height:0;color:red}.c{x:y;line-height:0}"
    _text, _counts, diagnostics = _clean(f"<style>{sheet}</style><p>T</p>")
    assert diagnostics["sheet_zero_size"] == 3


def test_a_comment_inside_a_sheet_does_not_hide_a_technique_from_the_count() -> None:
    sheet = ".a{display:/**/none}.b{opacity:/*x*/0}"
    _text, _counts, diagnostics = _clean(f"<style>{sheet}</style><p>T</p>")
    assert diagnostics["sheet_display"] == 1
    assert diagnostics["sheet_opacity"] == 1


@pytest.mark.parametrize(
    "style",
    [
        "display:none;display:-webkit-box",
        "display:none;display:block flow",
        "display:none;display:bogus",
        "visibility:hidden;visibility:bogus",
    ],
)
def test_an_unknown_later_value_leaves_the_property_undecided_and_counted(
    style: str,
) -> None:
    text, counts, diagnostics = _clean(f'<p style="{style}">TEXT</p>')
    assert "TEXT" in text and counts["hidden"] == 0
    assert diagnostics["inline_display"] + diagnostics["inline_visibility"] == 1


def test_a_comment_after_a_real_declaration_leaves_it_undecided_and_counted() -> None:
    # the guard in hidden_kind: without it this split reads display:none and drops
    text, counts, diagnostics = _clean('<p style="display:none;/* x */">TEXT</p>')
    assert "TEXT" in text and counts["hidden"] == 0
    assert diagnostics["inline_style_unparsed"] == 1
    assert diagnostics["inline_display"] == 1


@pytest.mark.parametrize(
    ("style", "key"),
    [
        ("--h:none;display:var(--h)", "inline_display"),
        ("display:var(--x,none)", "inline_display"),
        ("display:env(x,none)", "inline_display"),
        ("display:attr(x)", "inline_display"),
        ("--v:hidden;visibility:var(--v)", "inline_visibility"),
        ("visibility:var(--x,hidden)", "inline_visibility"),
        ("visibility:env(x,collapse)", "inline_visibility"),
        # ANY declared value, not only the final one: a later invalid keyword is
        # dropped at parse time by a browser, so the earlier var() still decides
        ("--h:none;display:var(--h);display:bogus", "inline_display"),
        ("--h:none;display:var(--h);display:hidden", "inline_display"),
        ("--v:hidden;visibility:var(--v);visibility:bogus", "inline_visibility"),
        ("display:var(--x,none);display:block", "inline_display"),
        ("display:block;display:var(--x,none)", "inline_display"),
        ("visibility:visible;visibility:var(--v)", "inline_visibility"),
    ],
)
def test_a_non_literal_value_is_kept_and_counted_once(style: str, key: str) -> None:
    text, counts, diagnostics = _clean(f'<p style="{style}">TEXT</p>')
    assert "TEXT" in text and counts["hidden"] == 0
    assert _nonzero(diagnostics) == {key: 1}


def test_a_plain_keyword_value_is_not_counted_as_non_literal() -> None:
    _text, _counts, diagnostics = _clean(
        '<p style="display:inline-block !important;visibility:visible">T</p>'
    )
    assert _nonzero(diagnostics) == {}


def test_text_in_a_comment_is_not_read_as_a_declaration() -> None:
    text, counts, diagnostics = _clean(
        '<p style="color:red/*;display:none;*/">TEXT</p>'
    )
    assert "TEXT" in text and counts["hidden"] == 0
    assert _nonzero(diagnostics) == {"inline_style_unparsed": 1}


@pytest.mark.parametrize("space", ["\u00a0", "\x0b"])
def test_python_whitespace_is_ignored_in_a_style_a_documented_limit(space: str) -> None:
    text, counts, _diagnostics = _clean(f'<p style="display:{space}none">TEXT</p>')
    assert counts["hidden_display"] == 1 and "TEXT" not in text


def test_a_visible_child_with_a_commented_style_is_counted_not_skipped() -> None:
    markup = (
        '<div style="visibility:hidden">'
        '<p style="visibility:/* x */visible">A</p></div>'
    )
    _text, _counts, diagnostics = _clean(markup)
    assert diagnostics["visible_descendant_dropped"] == 1


def test_a_quote_in_a_style_attribute_is_not_tokenised_and_is_reported() -> None:
    # documented limit: strings are not tokenised, so a `;` inside one splits the style;
    # the piece reads as an invalid value and is ignored, and the technique is counted
    text, counts, diagnostics = _clean("<p style=\"content:'a;display:none'\">TEXT</p>")
    assert "TEXT" in text and counts["hidden"] == 0
    assert diagnostics["inline_display"] == 1


@pytest.mark.parametrize(
    ("style", "pixel"),
    [
        ("width:1px!important;width:50px;height:1px", True),
        ("width:50px!important;width:1px!important;height:1px", True),
        ("width:1px!important;width:50px!important;height:1px", False),
        ("width:50px;width:1px;height:1px", True),
        ("width:1px;width:50px;height:1px", False),
    ],
)
def test_a_declared_size_follows_precedence_when_a_pixel_is_judged(
    style: str, pixel: bool
) -> None:
    """``declarations`` serves the pixel check: last wins, ``!important`` is kept."""
    _text, counts, _diagnostics = _clean(
        f'<p>T</p><img src="https://example.org/a.png" style="{style}">'
    )
    assert (counts["pixels"] == 1) is pixel


# --- browser evidence: where the converter agrees with WebKit, and where not ---

_WEBKIT = json.loads(
    (
        Path(__file__).parent / "fixtures" / "webkit" / "sow41-differential.json"
    ).read_text("utf-8")
)


@pytest.mark.parametrize("case", _WEBKIT, ids=[c["name"] for c in _WEBKIT])
def test_the_converter_against_native_webkit_measurements(
    case: dict[str, object],
) -> None:
    """
    Four pairs measured in native WebKit (dt-library, org-ducktyper-ai@7b4762a2).

    Painted text is always kept. Text WebKit does not paint is dropped only where the
    element's own style says ``display:none``; where WebKit hides it by clipping, the
    converter KEEPS it (CSS clipping is not evaluated) and reports it in diagnostics.
    """
    marker = "DIFFERENTIAL_SENTINEL"
    text, counts, diagnostics = _clean(str(case["html"]))
    painted = bool(case["expected_painted"])
    if painted:
        assert marker in text
        return
    if "display:none" in str(case["html"]):
        assert marker not in text and counts["hidden_display"] == 1
    else:
        assert marker in text  # the documented fidelity limit
        assert diagnostics["inline_max_size"] == 1


@pytest.mark.parametrize("style", ["display:no ne", "dis play:none"])
def test_whitespace_inside_a_keyword_is_removed_a_documented_limit(style: str) -> None:
    # pinned so the limit in the how-to stays true: a browser shows this text
    text, counts, _diagnostics = _clean(f'<p style="{style}">TEXT</p>')
    assert "TEXT" not in text and counts["hidden_display"] == 1


def test_only_the_named_property_is_read_for_non_keyword_values() -> None:
    # pins the filter in _values: another property's non-keyword value is not counted
    _text, _counts, diagnostics = _clean(
        '<p style="width:calc(1px + 2px);--display:var(--a);display:block">T</p>'
    )
    assert _nonzero(diagnostics) == {}


def test_a_comment_inside_a_value_may_add_one_count_a_documented_over_count() -> None:
    text, counts, diagnostics = _clean('<p style="display:block/*x*/">TEXT</p>')
    assert "TEXT" in text and counts["hidden"] == 0
    assert _nonzero(diagnostics) == {"inline_style_unparsed": 1, "inline_display": 1}
