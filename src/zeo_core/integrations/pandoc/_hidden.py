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


# ---------------------------------------------------------------------------
# The narrowing of the hiding rules: WHITELIST, narrow only on proof.
#
# Four kinds of style (``max_size``, ``zero_size``, ``offscreen``, ``clip``) hide
# text only under a condition; without it a browser paints the text (council
# zc-dtl-webkit-b312-v1 and its follow-up). A kind that matches is dropped as before
# UNLESS the condition is PROVEN false from fully modelled styles. The proof is a
# whitelist: every occurrence of every property it depends on must hold a value from
# a small closed set, and anything else (a ``var()``, a keyword, a later invalid
# value, an ``!important``, a comment, a rule we do not model) is "not proven", which
# hides the text exactly as before the narrowing. The council's review
# zc-zcore144-780e-v3 (B1 to B10, A1) is the specification.
# ---------------------------------------------------------------------------

#: Elements that are inline by default (so ``max-*`` and ``text-indent`` do not apply
#: to them). A custom or unknown element is NOT in this set: it is not proven inline.
_INLINE_TAGS: Final = frozenset(
    "a abbr acronym b bdi bdo big cite code data del dfn em font i ins kbd label "
    "mark nobr output q s samp small span strike strong sub sup time tt u var".split()
)
#: Table parts: ``max-height``/``max-width`` do not apply to a cell, row or row group
#: (a caption is a block and is not here).
_TABLE_TAGS: Final = frozenset("table colgroup col thead tbody tfoot tr td th".split())
_TABLE_DISPLAYS: Final = frozenset(
    {"table-cell", "table-row", "table-row-group", "table-header-group",
     "table-footer-group", "table-column", "table-column-group"}
)  # fmt: skip
#: The only ``display`` values of a PARENT after which a child is not blockified
#: (a flex, grid, ``-webkit-box`` or ``contents`` parent is not in the set).
_PLAIN_PARENT_DISPLAYS: Final = frozenset(
    {"block", "inline", "inline-block", "none", "table", "list-item", "flow-root"}
    | _TABLE_DISPLAYS
)  # fmt: skip
_NEGATIVE_OFFSET: Final = re.compile(r"^-[0-9]{3,}(px|em|pt)$")
_OFFSET_PROP: Final = re.compile(r"(left|top|text-indent)$")
_ZERO_LINE: Final = re.compile(r"^0(px|em|pt|%)?$")
_VISIBLE: Final = re.compile(r"^(visible)+$")
#: How many classes of one element that have a rule are examined.
_MAX_MATCHED_CLASSES: Final = 32

#: The narrowable kinds; every other kind (display, visibility, opacity, font size,
#: colour match, attribute) hides exactly as it always did.
_NARROWABLE: Final = frozenset({"max_size", "zero_size", "offscreen", "clip"})
#: A key of the id table (never a valid name): set when something not modelled could
#: set a property a proof depends on, so no narrowing may rely on the stylesheet.
UNMODELLED: Final = "\x00unmodelled"
#: Prefix of the case-folded alias of a rule name (the browser matches class and id
#: names case-insensitively in quirks mode, which an HTML fragment may be in).
FOLD: Final = "\x00ci:"
#: A rule body longer than ``STYLE_MAX_CHARS`` (or a pile of rules that gets there).
OVERSIZE_RULE: Final = "\x00oversize"

#: What a rule or a style we do not model could set that a proof depends on.
_CONDITION_PROPS: Final = (
    "overflow", "contain", "content-visibility", "position", "float", "display",
    "left", "top", "text-indent", "margin", "all",
)  # fmt: skip


def _unprovable(text: str) -> bool:
    """A style we cannot read as a browser does: `!important`, a comment, an escape."""
    return "!" in text or "/*" in text or "\\" in text


def _occurrences(texts: list[str]) -> dict[str, list[str]]:
    """Every ``name: value`` of the styles, in order: ALL occurrences, not the last."""
    found: dict[str, list[str]] = {}
    for text in texts:
        for part in squash(text).split(";"):
            name, sep, value = part.partition(":")
            if sep and name:
                found.setdefault(name, []).append(value)
    return found


def _only(occ: dict[str, list[str]], name: str, allowed: frozenset[str]) -> bool:
    """Every occurrence of ``name`` (there may be none) holds a value of ``allowed``."""
    return all(value in allowed for value in occ.get(name, ()))


def _no_clip(occ: dict[str, list[str]]) -> bool:
    """PROVEN: the box does not clip (overflow, contain and content-visibility)."""
    overflow = all(
        _VISIBLE.match(value)
        for name in ("overflow", "overflow-x", "overflow-y")
        for value in occ.get(name, ())
    )
    return (
        overflow
        and _only(occ, "contain", frozenset({"none"}))
        and _only(occ, "content-visibility", frozenset({"visible"}))
        and "all" not in occ
    )


def _proven_inline(
    tag: Any,  # noqa: ANN401
    occ: dict[str, list[str]],
    parent_plain: bool,
) -> bool:
    """PROVEN: an inline box (``max-*``, a zero size, ``text-indent`` do not apply)."""
    shown = occ.get("display", [])
    inline_box = (
        all(value == "inline" for value in shown)
        if shown
        else getattr(tag, "name", "") in _INLINE_TAGS
    )
    return (
        inline_box
        and _only(occ, "position", frozenset({"static", "relative", "sticky"}))
        and _only(occ, "float", frozenset({"none"}))
        and "all" not in occ
        and parent_plain
    )


def _proven_table_part(
    tag: Any,  # noqa: ANN401
    occ: dict[str, list[str]],
) -> bool:
    """PROVEN: a table cell, row or row group, decided by ``display`` when it is set."""
    shown = occ.get("display", [])
    if shown:
        return all(value in _TABLE_DISPLAYS for value in shown) and "all" not in occ
    return getattr(tag, "name", "") in _TABLE_TAGS and "all" not in occ


def _keep(  # noqa: C901 -- one branch per kind of the council table
    kind: str,
    tag: Any,  # noqa: ANN401
    occ: dict[str, list[str]],
    parent_plain: bool,
) -> bool:
    """Whether the text is PROVEN visible although a style of ``kind`` matched."""
    if kind == "max_size":
        return (
            _no_clip(occ)
            or _proven_inline(tag, occ, parent_plain)
            or _proven_table_part(tag, occ)
        )
    if kind == "zero_size":
        # line-height applies to an inline box, so an inline proof does not cover it
        shrunk = any(_ZERO_LINE.match(v) for v in occ.get("line-height", ()))
        return _no_clip(occ) or (not shrunk and _proven_inline(tag, occ, parent_plain))
    static = _only(occ, "position", frozenset({"static"})) and "all" not in occ
    if kind == "clip":
        # ``clip`` applies to absolutely positioned boxes only
        return _only(occ, "position", frozenset({"static", "relative", "sticky"})) and (
            "all" not in occ
        )
    if kind == "offscreen":
        for name, values in occ.items():
            if not _OFFSET_PROP.search(name):
                continue
            for value in values:
                if not _NEGATIVE_OFFSET.match(value):
                    continue
                if name in ("left", "top"):
                    if not static:  # a positioned box moves
                        return False
                elif name == "text-indent":
                    if not _proven_inline(tag, occ, parent_plain):
                        return False
                else:  # margin-left, margin-top and the rest move any box
                    return False
        return True
    return False


def _same_colour(tag: Any, style: str) -> bool:  # noqa: ANN401
    colour = _norm_color(declarations(style).get("color", ""))
    return bool(colour) and colour == _bg_of_ancestors(tag)


def style_rules(
    css: str,
    noise: Noise,  # noqa: ARG001 -- kept so the caller's signature does not change
    deadline: Any = None,  # noqa: ANN401
) -> tuple[dict[str, str], dict[str, str]]:
    """
    The declarations of the top-level ``.class`` and ``#id`` rules of a ``<style>``.

    Each rule's declarations are kept (later rules for a selector are appended), so an
    element is judged by its own style and its rules together. A rule body longer than
    ``STYLE_MAX_CHARS`` is not parsed and is marked as hiding. Rules inside an at-rule
    (``@media``) do not hide, as before, but they and any rule with a selector not
    modelled here, a nested rule, a quote, a comment or an escape set ``UNMODELLED``
    when they could change a property a proof depends on.
    """
    classes: dict[str, str] = {}
    ids: dict[str, str] = {}
    if "/*" in css or "\\" in css or '"' in css or "'" in css:
        ids[UNMODELLED] = (
            "1"  # a comment, an escape or a string: the split is untrusted
        )
    depth, start, selector = 0, 0, ""
    for index, char in enumerate(css):
        if char == "{":
            if depth == 0:
                selector = css[start:index].strip()
                start = index + 1
            elif depth == 1 and not selector.startswith("@"):
                ids[UNMODELLED] = "1"  # CSS nesting: a rule inside a rule
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                if deadline is not None:
                    deadline.check()
                _record(selector, css[start:index], classes, ids)
                start = index + 1
    return classes, ids


def _record(
    selector: str,
    body: str,
    classes: dict[str, str],
    ids: dict[str, str],
) -> None:
    parts = [part.strip() for part in selector.split(",")]
    simple = (
        not selector.startswith("@")
        and len(selector) <= _SELECTOR_MAX_CHARS
        and all(_SIMPLE_SELECTOR.match(part) for part in parts)
    )
    if not simple:
        lowered = body.lower()
        if any(name in lowered for name in _CONDITION_PROPS):
            ids[UNMODELLED] = "1"
        if selector.startswith("@") or len(selector) > _SELECTOR_MAX_CHARS:
            return
    for part in parts:
        if not _SIMPLE_SELECTOR.match(part):
            continue
        table = classes if part[0] == "." else ids
        name = part[1:]
        previous = table.get(name)
        if previous == OVERSIZE_RULE or len(body) > STYLE_MAX_CHARS:
            table[name] = OVERSIZE_RULE
        elif previous is None:
            table[name] = body
        elif len(previous) + len(body) + 1 > STYLE_MAX_CHARS:
            table[name] = OVERSIZE_RULE  # bounded: a pile of rules cannot cost time
        else:
            table[name] = f"{previous};{body}"
        table.setdefault(FOLD + name.lower(), name)


def _rule_texts(  # noqa: C901 -- the bounds of review A1 sit in this loop
    tag: Any,  # noqa: ANN401
    classes: dict[str, str],
    ids: dict[str, str],
) -> tuple[list[str], bool, bool]:
    """
    The rule bodies that match ``tag``: (bodies, provable, oversize).

    ``provable`` is False where the stylesheet cannot be trusted for a proof (a
    case-only match, which a browser makes in quirks mode, or two class rules whose
    order the class attribute does not give). ``oversize`` is True when a matching
    rule is over the bound, the element has more matching classes than are examined,
    or the bodies together exceed ``STYLE_MAX_CHARS``: all of that hides.
    """
    bodies: list[str] = []
    provable = UNMODELLED not in ids
    total = 0
    seen: set[str] = set()
    matched = 0
    for token in tag.get("class") or ():
        if token in seen:
            continue
        seen.add(token)
        name = token if token in classes else classes.get(FOLD + token.lower())
        if name is None or name not in classes:
            continue
        if name != token:
            provable = False
        body = classes[name]
        matched += 1
        total += len(body)
        if (
            body == OVERSIZE_RULE
            or matched > _MAX_MATCHED_CLASSES
            or total > STYLE_MAX_CHARS
        ):
            return bodies, False, True
        bodies.append(body)
    tag_id = tag.get("id")
    if isinstance(tag_id, str):
        name = tag_id if tag_id in ids else ids.get(FOLD + tag_id.lower())
        if name is not None and name in ids and name != UNMODELLED:
            if name != tag_id:
                provable = False
            body = ids[name]
            total += len(body)
            if body == OVERSIZE_RULE or total > STYLE_MAX_CHARS:
                return bodies, False, True
            bodies.append(body)
    if len(bodies) > 1:
        provable = False if _order_dependent(bodies) else provable
    return bodies, provable, False


def _order_dependent(bodies: list[str]) -> bool:
    """Two matching rules set one property a proof depends on (order decides)."""
    seen: dict[str, str] = {}
    for body in bodies:
        for name, value in declarations(body).items():
            if name in _CONDITION_PROPS or name.startswith("overflow"):
                if name in seen and seen[name] != value:
                    return True
                seen[name] = value
    return False


def _parent_plain(
    tag: Any,  # noqa: ANN401
    classes: dict[str, str],
    ids: dict[str, str],
) -> bool:
    """PROVEN: the parent does not make its children flex, grid or box items."""
    parent = getattr(tag, "parent", None)
    if parent is None or getattr(parent, "name", None) in (None, "[document]"):
        return True
    style = str(parent.get("style", ""))
    bodies, provable, oversize = _rule_texts(parent, classes, ids)
    if oversize or not provable or len(style) > STYLE_MAX_CHARS:
        return False
    texts = [*bodies, style]
    if any(_unprovable(text) for text in texts):
        return False
    occ = _occurrences(texts)
    return _only(occ, "display", _PLAIN_PARENT_DISPLAYS) and "all" not in occ


def hidden_kind(  # noqa: C901 -- the order of the council table
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
    bodies, provable, oversize = _rule_texts(tag, classes, ids)
    if oversize:
        return "style_class"
    texts = [style, *bodies] if style else bodies
    # What each source matches, as before the narrowing; a kind that is not narrowable
    # hides at once, a narrowable one only unless the condition is proven false.
    narrowable: list[str] = []
    for index, text in enumerate(texts):
        squashed = squash(text)
        for kind, pattern in noise.hidden_patterns:
            if not pattern.search(squashed):
                continue
            if kind not in _NARROWABLE:
                return kind if (style and index == 0) else "style_class"
            narrowable.append(kind if (style and index == 0) else "style_class")
            narrowable[-1] = kind
            break
    if narrowable:
        if (
            not provable
            or any(_unprovable(text) for text in texts)
            or not (occ := _occurrences(texts))
        ):
            return narrowable[0]
        plain = _parent_plain(tag, classes, ids)
        for kind in narrowable:
            if not _keep(kind, tag, occ, plain):
                return kind
    if style and _same_colour(tag, style):
        return "color_match"
    return None
