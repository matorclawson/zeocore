"""
A body-free report of WHY the hidden-text filter hid what it hid.

``hidden_report(html)`` runs the same parse, the same stylesheet reading and the same
decision as ``convert_newsletter``, and returns only counts under fixed codes: which
gate refused to narrow a rule (``max_size:unmodelled_sheet``), why the stylesheet was
not modelled (``sheet_quote``), and the tag names (from a fixed list) of the elements
it hid. It never returns, logs or raises with text from the document, so its output
can be recorded next to a held original without recording the original.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Final

from zeo_core.integrations.pandoc import _cleaning, _hidden, _noise
from zeo_core.integrations.pandoc._runner import Deadline

#: A tag name outside this list is reported as ``other``, so no document text leaks.
_TAGS: Final = frozenset({
    "a", "b", "blockquote", "body", "br", "center", "div", "em", "font", "h1", "h2",
    "h3", "h4", "h5", "h6", "i", "img", "li", "ol", "p", "span", "strong", "table",
    "tbody", "td", "th", "thead", "tr", "u", "ul",
})  # fmt: skip


def hidden_report(html: str | bytes, timeout_seconds: float = 30.0) -> dict[str, Any]:
    """
    Gate counts for one document; see the module docstring.

    The keys: ``hidden`` (kind -> count, as ``stripped`` reports), ``gates``
    (``kind:gate`` -> count), ``gate_tags`` (``kind:gate:tag`` -> count),
    ``sheet_unmodelled`` (reason -> 1; empty when the stylesheet is modelled),
    ``style_blocks`` and ``named_rules`` (counts only).
    """
    text = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    noise = _noise.load()
    deadline = Deadline(timeout_seconds)
    bs4 = _cleaning.bs4_module()
    _cleaning.precheck(text)
    soup = bs4.BeautifulSoup(
        text, "html.parser", on_duplicate_attribute="ignore", multi_valued_attributes=None
    )
    _cleaning.check_bounds(text, soup, deadline)
    style_blocks = len(soup.find_all("style"))
    classes, ids = _cleaning.css_rules(soup, noise, deadline)
    counts = dict.fromkeys(_cleaning.COUNT_KEYS, 0)
    _cleaning.drop_comments(soup, bs4, counts, deadline)
    _cleaning.drop_tags(soup, counts, noise, deadline)
    hidden: Counter[str] = Counter()
    gates: Counter[str] = Counter()
    gate_tags: Counter[str] = Counter()
    for tag in soup.find_all(True):
        deadline.check()
        if _cleaning.gone(tag):
            continue
        why: list[str] = []
        kind = _hidden.hidden_kind(tag, noise, classes, ids, why)
        if kind is None:
            continue
        hidden[kind] += 1
        name = tag.name if tag.name in _TAGS else "other"
        for gate in why:
            gates[gate] += 1
            gate_tags[f"{gate}:{name}"] += 1
        tag.decompose()
    reasons = ids.get(_hidden.UNMODELLED, "")
    named = {k for k in (*classes, *ids) if not k.startswith("\x00")}
    return {
        "hidden": dict(hidden),
        "gates": dict(gates),
        "gate_tags": dict(gate_tags),
        "sheet_unmodelled": {r: 1 for r in reasons.split(",") if r},
        "style_blocks": style_blocks,
        "named_rules": len(named),
    }
