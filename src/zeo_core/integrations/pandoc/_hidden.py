"""
Best-effort detection of text a reader cannot see (ZEOCORE-SOW-33, council M3).

Hidden text is how a message speaks to a machine and not to its reader. This finds
the common ways: ``hidden``, ``display:none``, ``visibility``, ``opacity:0``,
``font-size:0``, ``max-height:0`` (only on a clipping, non-inline box; council
zc-dtl-webkit-b312-v1), zero size, off-screen positioning,
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
_BOX_DISPLAYS: Final = frozenset(
    {"block", "inline-block", "flex", "inline-flex", "grid", "inline-grid", "table",
     "list-item", "flow-root"}
)  # fmt: skip
_CLIPPING: Final = frozenset({"hidden", "clip"})


def _clips(decl: dict[str, str]) -> bool:
    """``overflow`` (any axis) hides or clips what spills out of the box."""
    return any(
        decl.get(name) in _CLIPPING for name in ("overflow", "overflow-x", "overflow-y")
    )


def _max_size_applies(decl: dict[str, str], tag: Any) -> bool:  # noqa: ANN401
    """
    A zero ``max-height``/``max-width`` hides text only when the box clips it
    (``overflow:hidden`` or ``clip``) and is not an inline element. Council
    zc-dtl-webkit-b312-v1: a bare ``max-height:0`` on a span paints in a browser.
    ``tag`` is None for a ``<style>`` rule, whose elements are not known yet: the
    inline test then happens where the rule is applied.
    """
    if not _clips(decl):
        return False
    if tag is None:
        return True
    display = decl.get("display", "")
    if display in _BOX_DISPLAYS:
        return True
    if display == "inline":
        return False
    return getattr(tag, "name", "") not in _INLINE_TAGS


def _style_kind(
    style: str,
    noise: Noise,
    tag: Any = None,  # noqa: ANN401
) -> str | None:
    if len(style) > STYLE_MAX_CHARS:
        return None
    squashed = squash(style)
    for kind, pattern in noise.hidden_patterns:
        if not pattern.search(squashed):
            continue
        if kind == "max_size" and not _max_size_applies(declarations(style), tag):
            continue  # not hiding: look at the other patterns
        return kind
    return None


def _same_colour(tag: Any, style: str) -> bool:  # noqa: ANN401
    colour = _norm_color(declarations(style).get("color", ""))
    return bool(colour) and colour == _bg_of_ancestors(tag)


def style_rules(css: str, noise: Noise) -> tuple[dict[str, str], dict[str, str]]:
    """
    The top-level ``.class`` and ``#id`` rules in a ``<style>`` block that hide.

    Rules inside an at-rule (``@media``) are skipped: they may hide content only
    on a small screen, and removing it would lose what a desktop reader sees.
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
                _record(selector, css[start:index], noise, classes, ids)
                start = index + 1
    return classes, ids


def _record(
    selector: str,
    body: str,
    noise: Noise,
    classes: dict[str, str],
    ids: dict[str, str],
) -> None:
    if selector.startswith("@") or len(selector) > _SELECTOR_MAX_CHARS:
        return
    kind = "oversize" if len(body) > STYLE_MAX_CHARS else _style_kind(body, noise)
    if kind is None:
        return
    for part in (s.strip() for s in selector.split(",")):
        if _SIMPLE_SELECTOR.match(part):
            (classes if part[0] == "." else ids)[part[1:]] = kind


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
    return _by_class_or_id(tag, classes, ids)


def _by_class_or_id(
    tag: Any,  # noqa: ANN401
    classes: dict[str, str],
    ids: dict[str, str],
) -> str | None:
    inline = getattr(tag, "name", "") in _INLINE_TAGS

    def applies(kind: str) -> bool:
        # A max-size rule (already known to clip) does not hide an inline element.
        return not (kind == "max_size" and inline)

    tag_id = tag.get("id")
    if isinstance(tag_id, str) and tag_id in ids and applies(ids[tag_id]):
        return "style_class"
    for name in tag.get("class") or ():
        if name in classes and applies(classes[name]):
            return "style_class"
    return None
