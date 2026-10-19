"""Bounded selector matching for the hidden-text proof (zeocore-SOW-41, fix (a)).

A rule with a selector that is not a plain ``.class`` or ``#id`` leaves the sheet
modelled only when its selector, in the closed grammar of ``_selectors``, provably
cannot match the candidate element and its ancestors. Everything else still marks the
sheet not modelled, so the text is dropped as before. The cases are body-free and
synthetic; the first mirrors the counts of the real original's report
(``sheet_comment`` and ``rule_selector_not_simple``, one empty inline candidate).
"""

from __future__ import annotations

import time

import pytest

from zeo_core.integrations.pandoc import _hidden, _noise, _selectors, newsletter
from zeo_core.integrations.pandoc._runner import Deadline
from zeo_core.integrations.pandoc.diagnostic import hidden_report

#: A candidate the filter reads as ``max_size``: it is dropped unless proven visible.
CANDIDATE = '<span class="k" id="i" style="max-height:0">TEXT</span>'


def _hidden_count(markup: str) -> int:
    noise = _noise.load()
    _soup, counts, *_rest = newsletter._clean(markup, noise, Deadline(30))
    return counts["hidden_max_size"]


def _doc(sheet: str, body: str = CANDIDATE) -> str:
    return f"<style>{sheet}</style>{body}"


def test_the_real_originals_shape_is_now_admitted() -> None:
    sheet = "/* c */ body{margin:0} td p{display:block} .a b{position:static}"
    markup = _doc(sheet)
    assert _hidden_count(markup) == 0
    report = hidden_report(markup)
    assert report["gates"] == {}
    assert report["sheet_unmodelled"] == {}


def test_the_same_shape_before_the_change_is_what_the_receipt_showed() -> None:
    # a rule outside the grammar still gives the two-trigger poison of the receipt
    markup = _doc("/* c */ body{margin:0} td p:hover{display:block}")
    assert _hidden_count(markup) == 1
    assert "rule_selector_not_simple" in hidden_report(markup)["sheet_unmodelled"]


@pytest.mark.parametrize(
    "selector",
    [
        "span",
        "SPAN",
        "span.k",
        "span#i",
        ".k#i",
        "span.k#i",
        "span.k.k",
        "div span",
        "div .k",
        "body span",
        "html div span",
        "div  \t span",
        "div .k, td p",
        "td p, span",
    ],
)
def test_a_rule_that_can_match_the_candidate_still_hides(selector: str) -> None:
    body = f"<div>{CANDIDATE}</div>"
    markup = _doc(f"{selector}{{display:block}}", f"<html><body>{body}</body></html>")
    assert _hidden_count(markup) == 1


@pytest.mark.parametrize(
    "selector",
    [
        "td",
        "p",
        "body",
        "div",
        "span.z",
        "span#j",
        ".z",
        "div.z span",
        "td span",
        "span div",
        "span .k",
        "span span",
        "div div .k",
        "td p, th b",
        "table.x td",
    ],
)
def test_a_rule_that_provably_cannot_match_leaves_the_sheet_modelled(
    selector: str,
) -> None:
    body = f"<div>{CANDIDATE}</div>"
    markup = _doc(f"{selector}{{display:block}}", f"<html><body>{body}</body></html>")
    assert _hidden_count(markup) == 0


def test_only_the_last_compound_decides_whether_the_element_is_the_target() -> None:
    # ``div`` is an ancestor of the candidate, but the rule's target is the div, not it
    markup = _doc("div{display:block}", f"<div>{CANDIDATE}</div>")
    assert _hidden_count(markup) == 0


def test_the_ancestors_are_searched_nearest_first_and_in_order() -> None:
    body = f'<section class="a"><div class="b">{CANDIDATE}</div></section>'
    assert _hidden_count(_doc(".a .b span{display:block}", body)) == 1
    assert _hidden_count(_doc(".b .a span{display:block}", body)) == 0
    assert _hidden_count(_doc(".a .a span{display:block}", body)) == 0
    assert _hidden_count(_doc("section div span{display:block}", body)) == 1


def test_class_and_id_names_match_without_regard_to_case() -> None:
    assert _hidden_count(_doc("span.K{display:block}")) == 1
    assert _hidden_count(_doc("span#I{display:block}")) == 1


def test_a_class_is_a_whole_token_not_a_substring() -> None:
    markup = _doc("span.kk{display:block}")
    assert _hidden_count(markup) == 0


def test_a_rule_that_can_match_the_parent_stops_the_inline_proof() -> None:
    # the candidate is proven inline only when its parent is plain; ``div`` may make it
    # a flex item, so the keep fails and the text is dropped as before
    markup = _doc(
        "div{display:flex}",
        '<div><span style="max-height:0;overflow:hidden;display:inline">T</span></div>',
    )
    assert _hidden_count(markup) == 1


@pytest.mark.parametrize(
    "selector",
    [
        "span[x]",
        "span[class=k]",
        "a:hover",
        "span:first-child",
        "span::before",
        "span::after",
        "div>span",
        "div > span",
        "div+span",
        "div~span",
        "*",
        "div *",
        ":is(span)",
        ":where(span)",
        ":not(.z)",
        "svg|span",
        "|span",
        "sp\\61n",
        "span\\",
        "spän",
        "span..k",
        "span.",
        "span#",
        ".k.",
        "span,",
        ",span",
        "html:root",
        "a b c d e f g h i",
    ],
)
def test_a_selector_outside_the_grammar_keeps_the_sheet_unmodelled(
    selector: str,
) -> None:
    markup = _doc(f"{selector}{{display:block}}")
    assert _hidden_count(markup) == 1
    assert hidden_report(markup)["gates"] == {"max_size:unmodelled_sheet": 1}


@pytest.mark.parametrize(
    "rule",
    [
        "@media screen{span{display:block}}",
        "@supports (display:flex){span{display:block}}",
        "@layer a{span{display:block}}",
        "@import 'x.css';",
        "@font-face{display:block}",
    ],
)
def test_an_at_rule_keeps_the_sheet_unmodelled(rule: str) -> None:
    assert _hidden_count(_doc(rule)) == 1


def test_a_selector_over_the_length_bound_keeps_the_sheet_unmodelled() -> None:
    long_selector = "div " * 70 + "span"
    assert len(long_selector) > _hidden._SELECTOR_MAX_CHARS
    assert _hidden_count(_doc(f"{long_selector}{{display:block}}")) == 1


def test_a_chain_over_the_compound_bound_keeps_the_sheet_unmodelled() -> None:
    chain = " ".join(["div"] * (_selectors.MAX_COMPOUNDS + 1))
    assert _hidden_count(_doc(f"{chain}{{display:block}}")) == 1
    chain = " ".join(["div"] * _selectors.MAX_COMPOUNDS)
    assert _hidden_count(_doc(f"{chain}{{display:block}}")) == 0


def test_more_pending_selectors_than_the_bound_keep_the_sheet_unmodelled() -> None:
    rules = "".join(f"t{n}{{display:block}}" for n in range(_selectors.MAX_PENDING))
    assert _hidden_count(_doc(rules)) == 0
    rules += "t999{display:block}"
    markup = _doc(rules)
    assert _hidden_count(markup) == 1
    assert "rule_too_many" in hidden_report(markup)["sheet_unmodelled"]


def test_pending_selectors_of_two_style_blocks_are_merged_and_bounded() -> None:
    first = "".join(f"a{n}{{margin:0}}" for n in range(_selectors.MAX_PENDING))
    markup = f"<style>{first}</style><style>x{{margin:0}}</style>{CANDIDATE}"
    assert _hidden_count(markup) == 1
    markup = f"<style>a0{{margin:0}}</style><style>span{{margin:0}}</style>{CANDIDATE}"
    assert _hidden_count(markup) == 1  # the second block's rule may match


def test_a_rule_whose_body_sets_nothing_a_proof_depends_on_is_not_pending() -> None:
    assert _hidden_count(_doc("td p, div>span{color:red}")) == 0


def test_a_mixed_list_keeps_its_plain_parts_in_the_tables() -> None:
    markup = _doc(".k, td p{max-height:0;overflow:hidden}", '<div class="k">TEXT</div>')
    assert _hidden_count(markup) == 1  # ``.k`` still clips the candidate


@pytest.mark.parametrize(
    ("css", "unmodelled"),
    [
        ("/* unterminated {display:none", False),
        ("x{y:z} /* unterminated", False),
        ('"/* inside a string */" x{y:z}', True),  # the string itself is a trigger
        ("'*/' x{y:z}", True),
        ("/* a */ /* b */ x{y:z}", False),
        ("/***/ x{y:z}", False),
        ("/* ; } { */ x{y:z}", False),
    ],
)
def test_every_comment_form_is_blanked_before_the_sheet_is_split(
    css: str, unmodelled: bool
) -> None:
    plain, structure = _hidden._masks(css)
    assert len(plain) == len(structure) == len(css)
    assert "/*" not in structure
    classes, ids = _hidden.style_rules(css, _noise.load())
    assert set(classes) == set()  # no comment content became a rule
    assert (_hidden.UNMODELLED in ids) is unmodelled


def test_a_comment_holding_a_rule_makes_no_rule() -> None:
    css = "/* .x{display:none} */ .y{margin:0}"
    classes, _ids = _hidden.style_rules(css, _noise.load())
    assert "x" not in classes
    assert classes["y"] == "margin:0"


def test_an_unterminated_comment_at_the_end_hides_what_follows_it() -> None:
    css = ".y{margin:0} /* .z{display:none}"
    classes, _ids = _hidden.style_rules(css, _noise.load())
    assert set(classes) - {"\x00ci:y"} == {"y"}


def test_a_string_holding_a_comment_opener_does_not_open_a_comment() -> None:
    plain, structure = _hidden._masks('a{content:"/*"} b{c:d}')
    assert "b{c:d}" in plain and "b{c:d}" in structure


def test_pending_selectors_do_not_become_element_ids() -> None:
    # an element whose id looks like the internal key must not read the pending list
    markup = _doc(
        "td{margin:0}", '<span id="\x00pending" style="max-height:0">T</span>'
    )
    assert _hidden_count(markup) == 0


def test_many_candidates_with_many_pending_selectors_stay_fast() -> None:
    rules = "".join(f"x{n} y{n}{{margin:0}}" for n in range(_selectors.MAX_PENDING))
    nest = (
        "<div>" * 200 + ('<span style="max-height:0">T</span>' * 2000) + "</div>" * 200
    )
    started = time.monotonic()
    _hidden_count(_doc(rules, nest))
    assert time.monotonic() - started < 10


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("td", (("td", (), ()),)),
        (".a", (("", ("a",), ()),)),
        ("#a", (("", (), ("a",)),)),
        ("TD.A#B", (("td", ("a",), ("b",)),)),
        ("a b", (("a", (), ()), ("b", (), ()))),
        ("a\n\tb", (("a", (), ()), ("b", (), ()))),
    ],
)
def test_the_grammar_reads_a_chain(selector: str, expected: object) -> None:
    assert _selectors.parse_chain(selector) == expected


@pytest.mark.parametrize("selector", ["", " ", "a > b", "a+b", "*", "a[b]", "a:b", "é"])
def test_the_grammar_refuses_what_it_does_not_read(selector: str) -> None:
    assert _selectors.parse_chain(selector) is None


class _Element:
    """The little a selector reads of an element: a name, attributes and ancestors."""

    def __init__(self, name: str, attrs: dict[str, str] | None = None) -> None:
        self.name = name
        self.parents: list[_Element] = []
        self._attrs = attrs or {}

    def get(self, key: str, default: object = None) -> object:
        return self._attrs.get(key, default)


def test_an_unreadable_pending_selector_cannot_be_proven_not_to_match() -> None:
    # unreachable through a sheet (the grammar is checked first); defence in depth
    ids = {_selectors.PENDING: "a > b"}
    assert _selectors.may_match(_Element("span"), ids)


def test_a_selector_is_compared_with_the_names_of_the_element_in_any_case() -> None:
    ids = {_selectors.PENDING: "SPAN.Kk#Ii"}
    assert _selectors.may_match(_Element("SPAN", {"class": "kK", "id": "iI"}), ids)
    assert not _selectors.may_match(_Element("span", {"class": "k", "id": "i"}), ids)


def test_a_class_or_id_of_another_case_matches_in_a_parsed_document() -> None:
    markup = _doc(
        "span.k{display:block}", '<span class="K" style="max-height:0">T</span>'
    )
    assert _hidden_count(markup) == 1
    markup = _doc("span#i{display:block}", '<span id="I" style="max-height:0">T</span>')
    assert _hidden_count(markup) == 1


def test_an_element_id_equal_to_the_internal_key_reads_no_rule() -> None:
    import bs4

    tag = bs4.BeautifulSoup("<span>", "html.parser").find("span")
    assert tag is not None
    tag["id"] = _selectors.PENDING
    bodies, _provable, _oversize = _hidden._rule_texts(
        tag, {}, {_selectors.PENDING: "td"}
    )
    assert bodies == []


def test_a_plain_class_in_a_mixed_list_is_not_also_a_pending_selector() -> None:
    markup = _doc(
        ".k, td p{display:block}", '<span class="k" style="max-height:0">T</span>'
    )
    assert _hidden_count(markup) == 0
