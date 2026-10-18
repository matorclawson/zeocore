"""
Best-effort detection of text a reader cannot see (ZEOCORE-SOW-33, council M3).

Hidden text is how a message speaks to a machine and not to its reader. This finds
the common ways: ``hidden``, ``display:none``, ``visibility``, ``opacity:0``,
``font-size:0``, ``max-height:0`` and zero size (only on a clipping, non-inline box),
off-screen positioning (only when positioned, or an indent on a box), ``clip`` (only on
a positioned box; council zc-dtl-webkit-b312-v1 and follow-up),
text coloured like its own background, and the same declarations reached through a
class or id in a ``<style>`` block. Each kind is counted.
A style (an attribute or a rule body) longer than ``STYLE_MAX_CHARS`` is not parsed,
because padding must not cost time; it is treated AS HIDDEN (kind ``oversize``), so
padding after ``display:none`` cannot make hidden text visible.

It is BEST EFFORT: CSS has more ways to hide than any list covers. The guarantee is
not this filter; it is that nothing a message says is ever acted on (it comes out
as inert data, fenced downstream). A miss here costs a reader-invisible sentence in
the Markdown, never an instruction followed. ``mso-hide:all`` alone is NOT hidden: it is
Outlook-only and a browser paints the text.
"""

from __future__ import annotations

import re
from typing import Any, Final

from zeo_core.integrations.pandoc._noise import Noise

#: How far up to look for a background colour.
_ANCESTORS: Final = 20

#: The longest style attribute or style rule body that is examined. A longer one is
#: not parsed at all (an attacker's padding must not cost time), and it is treated AS
#: HIDDEN: padding after ``display:none`` must not make hidden text visible.
STYLE_MAX_CHARS: Final = 4096
_SELECTOR_MAX_CHARS: Final = 256

_NAMED: Final = {
    "white": "#ffffff",
    "black": "#000000",
    "transparent": "",
}
_HEX3: Final = re.compile(r"^#([0-9a-f])([0-9a-f])([0-9a-f])$")
_RGB: Final = re.compile(r"^rgba?\((\d{1,3}),(\d{1,3}),(\d{1,3})(?:,[0-9.]+)?\)$")
_SIMPLE_SELECTOR: Final = re.compile(r"^[.#][A-Za-z_][\w-]*$")


def squash(style: str) -> str:
    """A style attribute without whitespace, lower-cased."""
    return re.sub(r"\s+", "", style.lower())


def declarations(style: str) -> dict[str, str]:
    """The ``name: value`` pairs of a style, lower-cased, without ``!important``."""
    out: dict[str, str] = {}
    for part in squash(style).split(";"):
        name, sep, value = part.partition(":")
        if sep and name:
            out[name] = value.removesuffix("!important")
    return out


def _norm_color(value: str) -> str:
    value = _NAMED.get(value, value)
    short = _HEX3.match(value)
    if short:
        return "#" + "".join(c * 2 for c in short.groups())
    rgb = _RGB.match(value)
    if rgb:
        r, g, b = (min(int(x), 255) for x in rgb.groups())
        return f"#{r:02x}{g:02x}{b:02x}"
    return value


def _background(style: str, attr: Any) -> str:  # noqa: ANN401
    if len(style) > STYLE_MAX_CHARS:
        return ""
    decl = declarations(style)
    value = decl.get("background-color") or decl.get("background", "")
    if not value and attr:
        value = squash(str(attr))
    return _norm_color(value.split(" ")[0]) if value else ""


def _bg_of_ancestors(tag: Any) -> str:  # noqa: ANN401
    """The nearest declared background colour on the tag or its ancestors."""
    node = tag
    for _ in range(_ANCESTORS):
        if node is None or getattr(node, "name", None) in (None, "[document]"):
            return ""
        found = _background(str(node.get("style", "")), node.get("bgcolor"))
        if found:
            return found
        node = node.parent
    return ""


#: Elements that are inline by default: ``max-height``/``max-width`` do not apply to
#: them, so a ``max-height:0`` on one hides nothing in a browser.
_INLINE_TAGS: Final = frozenset(
    "a abbr b bdi bdo big cite code dfn em font i kbd label mark q s samp small "
    "span strike strong sub sup time tt u var".split()
)
#: Table parts: ``max-height``/``max-width`` do not apply to a cell, row or row group.
_TABLE_TAGS: Final = frozenset("table colgroup col thead tbody tfoot tr td th".split())
_BOX_DISPLAYS: Final = frozenset(
    {"block", "inline-block", "flex", "inline-flex", "grid", "inline-grid", "table",
     "list-item", "flow-root"}
)  # fmt: skip
_TABLE_DISPLAYS: Final = frozenset(
    {"table-cell", "table-row", "table-row-group", "table-header-group",
     "table-footer-group", "table-column", "table-column-group"}
)  # fmt: skip
_POSITIONED: Final = frozenset({"absolute", "fixed"})
_OFFSET_POSITIONS: Final = frozenset({"absolute", "fixed", "relative", "sticky"})
_ZERO: Final = re.compile(r"^0(px|pt|em|rem)?$")
_NEGATIVE_OFFSET: Final = re.compile(r"^-[0-9]{3,}(px|em|pt)$")


def _clips(decl: dict[str, str]) -> bool:
    """
    ``overflow`` (any axis, one or two values) hides or clips what spills out.

    ``declarations`` squashes whitespace, so ``overflow:hidden hidden`` reads
    ``hiddenhidden``: any ``hidden`` or ``clip`` in the value counts.
    """
    return any(
        "hidden" in decl.get(name, "") or "clip" in decl.get(name, "")
        for name in ("overflow", "overflow-x", "overflow-y")
    )


def _is_box(decl: dict[str, str], tag: Any) -> bool:  # noqa: ANN401
    """The element generates a block-level or inline-block box (not a plain inline)."""
    display = decl.get("display", "")
    if display in _BOX_DISPLAYS or display in _TABLE_DISPLAYS:
        return True
    if display == "inline":
        # an absolutely positioned or floated element is blockified anyway
        return decl.get("position") in _POSITIONED or decl.get("float") in (
            "left",
            "right",
        )
    if decl.get("position") in _POSITIONED or decl.get("float") in ("left", "right"):
        return True
    return getattr(tag, "name", "") not in _INLINE_TAGS


def _is_table_part(decl: dict[str, str], tag: Any) -> bool:  # noqa: ANN401
    return (
        decl.get("display", "") in _TABLE_DISPLAYS
        or getattr(tag, "name", "") in _TABLE_TAGS
    )


def _kind_applies(kind: str, decl: dict[str, str], tag: Any) -> bool:  # noqa: ANN401
    """
    Whether a style that matched the pattern of ``kind`` hides text in a browser.

    Council zc-dtl-webkit-b312-v1 and its follow-ups: each of these only works under
    a condition, and without it the browser paints the text. ``max-height/width:0``
    and ``width/height/line-height:0`` hide only a box that clips (``overflow``
    hidden or clip; a ``line-height:0`` clips whatever its element) and is not
    inline; ``max-*`` also never applies to a table cell, row or row group;
    ``left``/``top`` offsets need a positioned element (absolute, fixed, relative or
    sticky) and ``margin-left``/``-top`` do not; ``text-indent`` needs a non-inline
    element; ``clip`` needs ``position:absolute`` or ``fixed``. ``decl`` is the
    element's own style merged over the rules that match it (``hidden_kind``), so an
    inline style and a ``<style>`` rule are judged the same way.
    """
    if kind in ("max_size", "zero_size"):
        if not _clips(decl):
            return False
        if not _ZERO.match(decl.get("line-height", "x")) and not _is_box(decl, tag):
            return False
        return not (kind == "max_size" and _is_table_part(decl, tag))
    if kind == "offscreen":
        shifted = decl.get("position") in _OFFSET_POSITIONS and any(
            _NEGATIVE_OFFSET.match(decl.get(name, "")) for name in ("left", "top")
        )
        margin = any(
            _NEGATIVE_OFFSET.match(decl.get(name, ""))
            for name in ("margin-left", "margin-top")
        )
        indented = _NEGATIVE_OFFSET.match(
            decl.get("text-indent", "")
        ) is not None and _is_box(decl, tag)
        return shifted or margin or indented
    if kind == "clip":
        return decl.get("position") in _POSITIONED
    return True


def _style_kind(
    style: str,
    noise: Noise,
    tag: Any = None,  # noqa: ANN401
) -> str | None:
    if len(style) > STYLE_MAX_CHARS:
        return None
    squashed = squash(style)
    decl = declarations(style)
    for kind, pattern in noise.hidden_patterns:
        if not pattern.search(squashed):
            continue
        if not _kind_applies(kind, decl, tag):
            continue  # not hiding: look at the other patterns
        return kind
    return None


def _same_colour(tag: Any, style: str) -> bool:  # noqa: ANN401
    colour = _norm_color(declarations(style).get("color", ""))
    return bool(colour) and colour == _bg_of_ancestors(tag)


OVERSIZE_RULE: Final = "\x00oversize"


def style_rules(
    css: str,
    noise: Noise,  # noqa: ARG001 -- kept so the caller's signature does not change
) -> tuple[dict[str, str], dict[str, str]]:
    """
    The declarations of the top-level ``.class`` and ``#id`` rules of a ``<style>``.

    Each rule's declarations are kept (later rules for a selector are appended, so the
    later declaration wins), so that an element is judged by its inline style and its
    rules together. A rule body longer than ``STYLE_MAX_CHARS`` is not parsed and is
    marked, and treated as hiding. Rules inside an at-rule (``@media``) are skipped:
    they may hide content only on a small screen, and removing it would lose what a
    desktop reader sees.
    """
    classes: dict[str, str] = {}
    ids: dict[str, str] = {}
    depth, start, selector = 0, 0, ""
    for index, char in enumerate(css):
        if char == "{":
            if depth == 0:
                selector = css[start:index].strip()
                start = index + 1
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                _record(selector, css[start:index], classes, ids)
                start = index + 1
    return classes, ids


def _record(
    selector: str,
    body: str,
    classes: dict[str, str],
    ids: dict[str, str],
) -> None:
    if selector.startswith("@") or len(selector) > _SELECTOR_MAX_CHARS:
        return
    for part in (s.strip() for s in selector.split(",")):
        if not _SIMPLE_SELECTOR.match(part):
            continue
        table = classes if part[0] == "." else ids
        name = part[1:]
        if len(body) > STYLE_MAX_CHARS or table.get(name) == OVERSIZE_RULE:
            table[name] = OVERSIZE_RULE
        else:
            table[name] = f"{table.get(name, '')};{body}" if name in table else body


def hidden_kind(
    tag: Any,  # noqa: ANN401
    noise: Noise,
    classes: dict[str, str],
    ids: dict[str, str],
) -> str | None:
    """Why this element is hidden from a reader, or None."""
    if tag.has_attr("hidden"):
        return "attribute"
    style = str(tag.get("style", ""))
    if len(style) > STYLE_MAX_CHARS:
        return "oversize"  # not parsed, so not trusted: dropped, see STYLE_MAX_CHARS
    if style:
        kind = _style_kind(style, noise, tag)
        if kind is not None:
            return kind
        if _same_colour(tag, style):
            return "color_match"
    return _by_class_or_id(tag, noise, style, classes, ids)


def _by_class_or_id(
    tag: Any,  # noqa: ANN401
    noise: Noise,
    style: str,
    classes: dict[str, str],
    ids: dict[str, str],
) -> str | None:
    """
    Hidden by the rules that match the element, judged together with its own style.

    The matching class rules (in the element's class order), then the id rule, then
    the inline style are merged, the later declaration winning, and every kind is
    evaluated on the result exactly as for an inline style.
    """
    parts: list[str] = []
    for name in tag.get("class") or ():
        if name in classes:
            parts.append(classes[name])
    tag_id = tag.get("id")
    if isinstance(tag_id, str) and tag_id in ids:
        parts.append(ids[tag_id])
    if not parts:
        return None
    if OVERSIZE_RULE in parts:
        return "style_class"
    merged = ";".join([*parts, style])
    if len(merged) > STYLE_MAX_CHARS:
        return "style_class"  # too long to examine: treated as hidden (fail closed)
    return "style_class" if _style_kind(merged, noise, tag) is not None else None
