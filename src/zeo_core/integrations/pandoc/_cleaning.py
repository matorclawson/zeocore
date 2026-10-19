"""
Cleaning one message's HTML before pandoc sees it (ZEOCORE-SOW-33, section 3).

Every pass is linear in the size of the document, and the work is bounded before it
starts (``MAX_ELEMENTS`` and ``MAX_DEPTH``) and against one deadline, so a hostile
message cannot make the cleaner slow. Nothing is fetched, run or followed: addresses
are only rewritten, and a redirector is unwrapped only when its target is written in
the address itself.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from zeo_core.integrations.pandoc import _hidden, _selectors
from zeo_core.integrations.pandoc._noise import Noise
from zeo_core.integrations.pandoc._runner import Deadline
from zeo_core.integrations.pandoc.refusals import refusal

#: The most elements and the deepest nesting one message may have; more is refused.
MAX_ELEMENTS: Final = 100_000
MAX_DEPTH: Final = 256

_FOOTER_MAX_CHARS: Final = 400
_BLOCKS: Final = frozenset({"p", "li", "div", "td", "tr", "section", "footer"})
_DROPPED: Final = {
    "script": "scripts",
    "style": "styles",
    "iframe": "frames",
    "object": "frames",
    "embed": "frames",
    "form": "forms",
    "noscript": "scripts",
    "template": "scripts",
    "svg": "scripts",
}
_SILENT: Final = ("head", "meta", "link", "base", "title")
_TABLE_TAGS: Final = ("table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption")
HIDDEN_KINDS: Final = (
    "attribute",
    "display",
    "visibility",
    "opacity",
    "font_size",
    "max_size",
    "mso",
    "zero_size",
    "offscreen",
    "clip",
    "color_match",
    "style_class",
    "oversize",
)
COUNT_KEYS: Final = (
    "scripts",
    "styles",
    "frames",
    "forms",
    "comments",
    "hidden",
    *(f"hidden_{kind}" for kind in HIDDEN_KINDS),
    "pixels",
    "footers",
    "tracked_links",
    "unsafe_links",
    "unsafe_images",
    "tables_flattened",
    "style_oversize",
)
_PIXEL_SIZES: Final = frozenset({"0", "0px", "1", "1px"})

#: A footer line is short; a longer text node is a paragraph, and only its first
#: characters are searched, so a hostile node cannot make a pattern slow.
_FOOTER_SCAN_CHARS: Final = 2000

#: How many redirector wrappers one address may be unwrapped through.
MAX_UNWRAP: Final = 3


@dataclass(frozen=True, slots=True)
class ConvertedLink:
    """
    A link kept in the Markdown: the clean address, its text, and the original.

    ``tracked`` is True for a link that goes through a mail service's click tracker,
    or carries a per-recipient parameter, or had tracking removed from it. Such a
    link identifies the reader or the send, so it must never become a pick's link.
    """

    url: str
    text: str
    original_url: str | None = None
    tracked: bool = False


def bs4_module() -> Any:  # noqa: ANN401 -- an optional third-party module
    """The optional ``bs4`` module, or a refusal naming the install command."""
    try:
        return importlib.import_module("bs4")
    except ImportError as error:
        raise refusal("extra_missing") from error


def gone(node: Any) -> bool:  # noqa: ANN401
    """
    True once a node has been decomposed.

    Read from the node's own dictionary: ``Tag.decomposed`` asks for an attribute a
    live tag lacks, and ``Tag.__getattr__`` answers by searching its whole subtree,
    which makes a pass over deeply nested HTML quadratic.
    """
    return bool(getattr(node, "__dict__", {}).get("_decomposed", False))


def host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    """True when ``host`` is one of ``suffixes`` or a subdomain of one."""
    host = host.lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in suffixes)


# --- the bounds --------------------------------------------------------------


def check_bounds(text: str, soup: Any, deadline: Deadline) -> None:  # noqa: ANN401
    """Refuse a document with too many elements or too deep a nesting."""
    count, deepest, steps = 0, 0, 0
    stack: list[tuple[Any, int]] = [(soup, 0)]
    while stack:
        steps += 1
        if steps % 256 == 0:
            deadline.check()
        node, depth = stack.pop()
        deepest = max(deepest, depth)
        for child in getattr(node, "children", ()):
            if getattr(child, "name", None) is not None:
                count += 1
                stack.append((child, depth + 1))
        if count > MAX_ELEMENTS or deepest > MAX_DEPTH:
            raise refusal("html_too_deep")


def precheck(text: str) -> None:
    """A cheap bound before parsing: tag openers can't outnumber twice the cap."""
    if text.count("<") > 2 * MAX_ELEMENTS:
        raise refusal("html_too_deep")


# --- passes ------------------------------------------------------------------


def drop_tags(
    soup: Any,  # noqa: ANN401
    counts: dict[str, int],
    noise: Noise,
    deadline: Deadline,
) -> None:
    """Remove scripts, styles, frames, forms and form controls, and the head."""
    for tag in soup.find_all([*_DROPPED, *noise.form_tags]):
        deadline.check()
        if gone(tag):
            continue
        counts[_DROPPED.get(tag.name, "forms")] += 1
        tag.decompose()
    for tag in soup.find_all(list(_SILENT)):  # the head: its title is already taken
        deadline.check()
        if not gone(tag):
            tag.decompose()


def drop_comments(
    soup: Any,  # noqa: ANN401
    bs4: Any,  # noqa: ANN401
    counts: dict[str, int],
    deadline: Deadline,
) -> None:
    """Remove HTML comments."""
    for node in soup.find_all(string=lambda s: isinstance(s, bs4.Comment)):
        deadline.check()
        counts["comments"] += 1
        node.extract()


def css_rules(  # noqa: C901 -- the document-level strict triggers of review B
    soup: Any,  # noqa: ANN401
    noise: Noise,
    deadline: Deadline,
) -> tuple[dict[str, str], dict[str, str]]:
    """The hiding ``.class`` and ``#id`` rules from every ``<style>`` block."""
    classes: dict[str, str] = {}
    ids: dict[str, str] = {}
    if soup.find("link") is not None:
        _hidden.flag(ids, "document_link")  # an external stylesheet we cannot read
    for block in soup.find_all("style"):
        deadline.check()
        kind = str(block.get("type", "text/css")).strip().lower()
        if (
            block.has_attr("media")
            or kind not in ("", "text/css")
            or block.find_parent("template") is not None
        ):
            _hidden.flag(ids, "style_media_type_or_template")  # may or may not apply
            continue
        text = block.get_text()
        if "@import" in text.lower():
            _hidden.flag(ids, "style_import")
        found_classes, found_ids = _hidden.style_rules(text, noise, deadline)
        for table, found in ((classes, found_classes), (ids, found_ids)):
            for name, body in found.items():
                if name == _selectors.PENDING:
                    for selector in body.split("\x01"):
                        if not _selectors.add_pending(table, selector):
                            _hidden.flag(table, "rule_too_many")
                    continue
                if name == _hidden.UNMODELLED:
                    for reason in body.split(","):
                        _hidden.flag(table, reason)  # keep the reasons of every block
                    continue
                previous = table.get(name)
                if name.startswith(_hidden.FOLD):
                    # an alias of the real name; two spellings are not provable
                    if previous is not None and previous != body:
                        _hidden.flag(ids, "rule_two_spellings")
                    table[name] = body
                # the same selector in two <style> blocks: the later declaration wins
                elif previous is None:
                    table[name] = body
                elif (
                    _hidden.OVERSIZE_RULE in (previous, body)
                    or len(previous) + len(body) + 1 > _hidden.STYLE_MAX_CHARS
                ):
                    table[name] = _hidden.OVERSIZE_RULE
                else:
                    table[name] = f"{previous};{body}"
    return classes, ids


def drop_hidden(
    soup: Any,  # noqa: ANN401
    noise: Noise,
    counts: dict[str, int],
    rules: tuple[dict[str, str], dict[str, str]],
    deadline: Deadline,
) -> None:
    """Remove elements a reader cannot see, counting each kind."""
    classes, ids = rules
    for tag in soup.find_all(True):
        deadline.check()
        if gone(tag):
            continue
        if len(str(tag.get("style", ""))) > _hidden.STYLE_MAX_CHARS:
            counts["style_oversize"] += 1  # not parsed: see STYLE_MAX_CHARS
        kind = _hidden.hidden_kind(tag, noise, classes, ids)
        if kind is not None:
            counts["hidden"] += 1
            counts[f"hidden_{kind}"] += 1
            tag.decompose()


def _size_of(value: Any) -> int | None:  # noqa: ANN401
    match = re.match(r"\s*(\d+)", str(value or ""))
    return int(match.group(1)) if match else None


def _is_pixel(img: Any, noise: Noise) -> bool:  # noqa: ANN401
    width, height = _size_of(img.get("width")), _size_of(img.get("height"))
    if width is not None and height is not None and width <= 1 and height <= 1:
        return True
    style = str(img.get("style", ""))
    if len(style) <= _hidden.STYLE_MAX_CHARS:  # a longer style is not parsed
        declared = _hidden.declarations(style)
        if declared.get("width") in _PIXEL_SIZES and declared.get("height") in (
            _PIXEL_SIZES
        ):
            return True
    host = urlsplit(str(img.get("src", "")).strip()).hostname or ""
    return bool(host) and host_matches(host, noise.pixel_hosts)


def drop_images(
    soup: Any,  # noqa: ANN401
    noise: Noise,
    counts: dict[str, int],
    deadline: Deadline,
) -> None:
    """Remove tracking pixels, and images whose source is not http(s)."""
    for img in soup.find_all("img"):
        deadline.check()
        if gone(img):
            continue
        src = str(img.get("src", "")).strip()
        if urlsplit(src).scheme.lower() not in ("http", "https"):
            counts["unsafe_images"] += 1
            img.decompose()
        elif _is_pixel(img, noise):
            counts["pixels"] += 1
            img.decompose()


def flatten_tables(
    soup: Any,  # noqa: ANN401
    counts: dict[str, int],
    deadline: Deadline,
) -> None:
    """
    Turn table markup into plain blocks.

    Newsletters lay themselves out in tables, and pandoc would print any table it
    cannot draw as a pipe table as ``[TABLE]``, losing the text. Flattening keeps
    every cell's text in reading order; a real data table loses its columns.
    """
    for table in soup.find_all("table"):
        deadline.check()
        if not gone(table):
            counts["tables_flattened"] += 1
    for tag in soup.find_all(list(_TABLE_TAGS)):
        deadline.check()
        if not gone(tag):
            tag.name = "div"


def _footer_target(node: Any) -> Any:  # noqa: ANN401
    """The block holding a footer line, if it is short; else the node's parent."""
    for ancestor in node.parents:
        if getattr(ancestor, "name", None) in _BLOCKS:
            if len(ancestor.get_text(" ", strip=True)) <= _FOOTER_MAX_CHARS:
                return ancestor
            break
    return node.parent


def _scanned(nodes: Any, deadline: Deadline) -> Any:  # noqa: ANN401
    for index, node in enumerate(nodes):
        if index % 100 == 0:
            deadline.check()
        yield node


def drop_footers(
    soup: Any,  # noqa: ANN401
    noise: Noise,
    counts: dict[str, int],
    deadline: Deadline,
) -> None:
    """Remove the unsubscribe, 'view in browser' and preference lines."""
    hits = [
        node
        for node in _scanned(soup.find_all(string=True), deadline)
        if any(p.search(str(node)[:_FOOTER_SCAN_CHARS]) for p in noise.footer_patterns)
    ]
    for index, node in enumerate(hits):
        if index % 100 == 0:
            deadline.check()
        if getattr(node, "parent", None) is None:
            continue  # inside a footer block already removed
        target = _footer_target(node)
        if target is None or target.name in ("html", "body", "[document]"):
            node.extract()
        else:
            target.decompose()
        counts["footers"] += 1


# --- links -------------------------------------------------------------------


def _unwrap_redirect(parts: Any, noise: Noise) -> str | None:  # noqa: ANN401
    host = (parts.hostname or "").lower()
    for r_host, r_path, r_param in noise.redirectors:
        if host == r_host and parts.path == r_path:
            for key, value in parse_qsl(parts.query, keep_blank_values=False):
                if key == r_param and urlsplit(value).scheme in ("http", "https"):
                    return str(value)
    return None


def clean_url(url: str, noise: Noise, _depth: int = 0) -> tuple[str, bool]:
    """
    The address without tracking parameters or a known redirector; and if changed.

    A redirector is unwrapped at most ``MAX_UNWRAP`` times, so an address that wraps
    itself again and again cannot recurse without end; past that it is only stripped.
    """
    parts = urlsplit(url)
    target = _unwrap_redirect(parts, noise) if _depth < MAX_UNWRAP else None
    if target is not None:
        return clean_url(target, noise, _depth + 1)[0], True
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in noise.tracking_params
    ]
    cleaned = urlunsplit(parts._replace(query=urlencode(kept)))
    return cleaned, cleaned != url


def _is_tracked(cleaned: str, changed: bool, noise: Noise) -> tuple[bool, bool]:
    """Whether a link identifies the send or the reader; and if its host is opaque."""
    parts = urlsplit(cleaned)
    host = parts.hostname or ""
    opaque = bool(host) and host_matches(host, noise.opaque_hosts)
    per_recipient = any(
        k.lower() in noise.per_recipient_params
        for k, _ in parse_qsl(parts.query, keep_blank_values=True)
    )
    return changed or opaque or per_recipient, opaque


def clean_links(
    soup: Any,  # noqa: ANN401
    noise: Noise,
    counts: dict[str, int],
    deadline: Deadline,
) -> tuple[list[ConvertedLink], int]:
    """
    Clean every link, and list them; and how many go through an opaque tracker.

    A link with an unsafe scheme loses its address and keeps its text.
    """
    links: list[ConvertedLink] = []
    opaque_count = 0
    for anchor in soup.find_all("a"):
        deadline.check()
        original = str(anchor.get("href", "")).strip()
        if not original:
            continue
        if urlsplit(original).scheme.lower() not in ("http", "https", "mailto"):
            counts["unsafe_links"] += 1
            del anchor["href"]
            continue
        cleaned, changed = clean_url(original, noise)
        tracked, opaque = _is_tracked(cleaned, changed, noise)
        opaque_count += int(opaque)
        if changed:
            counts["tracked_links"] += 1
            anchor["href"] = cleaned
        text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
        links.append(
            ConvertedLink(cleaned, text, original if changed else None, tracked)
        )
    return links, opaque_count


def title_of(soup: Any) -> str | None:  # noqa: ANN401
    """The ``<title>``, else the first ``<h1>``, as one line of at most 300 chars."""
    for name in ("title", "h1"):
        found = soup.find(name)
        if found is not None:
            text = re.sub(r"\s+", " ", found.get_text(" ", strip=True))
            if text:
                return text[:300]
    return None
