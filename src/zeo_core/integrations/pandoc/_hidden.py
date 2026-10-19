"""
What the converter decides about hidden text, and what it only reports (zeocore-SOW-41).

DECIDED, from the element alone (no stylesheet, no cascade, no inheritance): an
element is not converted when the HTML itself marks it not rendered:

* the ``hidden`` attribute;
* its own ``style`` attribute declares ``display:none``;
* its own ``style`` attribute declares ``visibility:hidden`` or ``collapse``.

In a ``style`` attribute the last declaration of a property wins and ``!important`` is
read; an attribute over ``STYLE_MAX_CHARS`` is not parsed (padding must not cost time)
and is reported, not decided.

NOT EVALUATED, only reported: everything that depends on CSS beyond that. Class and id
rules, selectors, ``@media``, inheritance, ``<link>`` and ``@import`` stylesheets,
clipping and positioning, ``opacity``, ``font-size:0``, zero or maximum sizes,
off-screen offsets, colour on colour. For those the converter keeps the text and
returns counts under fixed names (``diagnostics``): how many kept elements name a
technique in their own style, how often each technique appears in the ``<style>``
text, and how many stylesheets, links, imports and ``@media`` rules were present and
not evaluated. The caller decides what a non-zero count means.

The converter makes no claim that kept text is visible to a reader, or that dropped
text was hidden in every client. What it guarantees is the safety of the result
(bounded input, nothing fetched or run, inert typed output), see ``newsletter``.
"""

from __future__ import annotations

import re
from typing import Any, Final

from zeo_core.integrations.pandoc._noise import Noise

#: The longest style attribute that is parsed. A longer one is reported, not decided.
STYLE_MAX_CHARS: Final = 4096
#: The most ``<style>`` text that is scanned for the counts (all blocks together).
SHEET_SCAN_CHARS: Final = 262_144

#: What is DECIDED, and counted as ``hidden_<kind>``.
HIDDEN_KINDS: Final = ("attribute", "display", "visibility")
#: The techniques the data file names (``hidden_style_patterns``), in its order.
TECHNIQUES: Final = (
    "display",
    "visibility",
    "opacity",
    "font_size",
    "max_size",
    "zero_size",
    "offscreen",
    "clip",
)
#: The techniques that are only reported when a kept element names them inline.
_REPORTED_INLINE: Final = TECHNIQUES[2:]

#: Every key of ``diagnostics``, always present (zero when absent).
DIAGNOSTIC_KEYS: Final = (
    "stylesheet_blocks",
    "stylesheet_links",
    "stylesheet_imports",
    "stylesheet_media",
    "stylesheet_scan_truncated",
    "inline_style_unparsed",
    *(f"inline_{name}" for name in _REPORTED_INLINE),
    *(f"sheet_{name}" for name in TECHNIQUES),
)


def new_diagnostics() -> dict[str, int]:
    """A fresh ``diagnostics`` with every key at zero."""
    return dict.fromkeys(DIAGNOSTIC_KEYS, 0)


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


def hidden_kind(tag: Any) -> str | None:  # noqa: ANN401
    """Why the HTML itself marks this element not rendered, or None."""
    if tag.has_attr("hidden"):
        return "attribute"
    style = tag.get("style")
    if not isinstance(style, str) or not style or len(style) > STYLE_MAX_CHARS:
        return None
    declared = declarations(style)
    if declared.get("display") == "none":
        return "display"
    if declared.get("visibility") in ("hidden", "collapse"):
        return "visibility"
    return None


def note_inline(tag: Any, noise: Noise, diagnostics: dict[str, int]) -> None:  # noqa: ANN401
    """Count a KEPT element that names a hiding technique in its own style."""
    style = tag.get("style")
    if not isinstance(style, str) or not style:
        return
    if len(style) > STYLE_MAX_CHARS:
        diagnostics["inline_style_unparsed"] += 1
        return
    squashed = squash(style)
    for kind, pattern in noise.hidden_patterns:
        if kind in _REPORTED_INLINE and pattern.search(squashed):
            diagnostics[f"inline_{kind}"] += 1


def note_sheets(
    soup: Any,  # noqa: ANN401
    noise: Noise,
    diagnostics: dict[str, int],
    deadline: Any,  # noqa: ANN401
) -> None:
    """Count the stylesheets that are present and not evaluated, and what they name."""
    diagnostics["stylesheet_links"] = len(soup.find_all("link"))
    budget = SHEET_SCAN_CHARS
    for block in soup.find_all("style"):
        deadline.check()
        diagnostics["stylesheet_blocks"] += 1
        if block.has_attr("media"):
            diagnostics["stylesheet_media"] += 1
        text = squash(block.get_text())
        if len(text) > budget:
            diagnostics["stylesheet_scan_truncated"] = 1
            text = text[:budget]
        budget -= len(text)
        diagnostics["stylesheet_imports"] += text.count("@import")
        diagnostics["stylesheet_media"] += text.count("@media")
        for kind, pattern in noise.hidden_patterns:
            if kind in TECHNIQUES:
                diagnostics[f"sheet_{kind}"] += len(pattern.findall(text))
