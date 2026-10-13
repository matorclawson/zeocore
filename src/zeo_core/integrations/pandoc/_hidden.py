"""
Best-effort detection of text a reader cannot see (ZEOCORE-SOW-33, council M3).

Hidden text is how a message speaks to a machine and not to its reader. This finds
the common ways: ``hidden``, ``display:none``, ``visibility``, ``opacity:0``,
``font-size:0``, ``max-height:0``, ``mso-hide``, zero size, off-screen positioning,
text coloured like its own background, and the same declarations reached through a
class or id in a ``<style>`` block. Each kind is counted.

It is BEST EFFORT: CSS has more ways to hide than any list covers. The guarantee is
not this filter; it is that nothing a message says is ever acted on (it comes out
as inert data, fenced downstream). A miss here costs a reader-invisible sentence in
the Markdown, never an instruction followed.
"""

from __future__ import annotations

import re
from typing import Any, Final

from zeo_core.integrations.pandoc._noise import Noise

#: How far up to look for a background colour.
_ANCESTORS: Final = 20

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


def _declarations(style: str) -> dict[str, str]:
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
    decl = _declarations(style)
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


def _style_kind(style: str, noise: Noise) -> str | None:
    squashed = squash(style)
    for kind, pattern in noise.hidden_patterns:
        if pattern.search(squashed):
            return kind
    return None


def _same_colour(tag: Any, style: str) -> bool:  # noqa: ANN401
    colour = _norm_color(_declarations(style).get("color", ""))
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
    if selector.startswith("@"):
        return
    kind = _style_kind(body, noise)
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
    if style:
        kind = _style_kind(style, noise)
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
    tag_id = tag.get("id")
    if isinstance(tag_id, str) and tag_id in ids:
        return "style_class"
    for name in tag.get("class") or ():
        if name in classes:
            return "style_class"
    return None
