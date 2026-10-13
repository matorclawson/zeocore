"""
Markdown to email-safe HTML, with a pinned renderer (ZEOCORE-SOW-33, 5A and 5B).

zeonewsroom keeps a newsletter issue as Markdown and needs a small, email-safe HTML
subset, the same bytes every time, because the operator marks exact bytes. The gate
that judges the HTML is the guarantee; this renderer is an untrusted producer held
to a contract, and it returns the evidence the gate reads: the renderer tuple.

How it works:

1. The Markdown is read by pandoc's ``markdown_strict`` reader with raw HTML and
   every attribute feature off, so HTML written in the Markdown is text, never markup.
2. A Lua filter (package data, never read from the caller's directory) REBUILDS the
   document from only the allowed nodes and refuses anything else. Pandoc's own
   writer would add ``figure``, ``section``, ``span``, ``div``, ``class``, ``id`` and
   footnotes; none of that can reach the output.
3. A second line in Python parses the HTML and refuses any tag, attribute, link or
   image outside the subset, so a bug in step 2 still cannot ship markup.

Anything outside the subset is a typed refusal that names the construct, never a
silent rewrite. The output is deterministic for a given pandoc binary, and the
renderer tuple names that binary, the filter and the exact flags. An upgrade of
any of them is a new tuple, so a new artifact.
"""

from __future__ import annotations

import hashlib
import html as html_module
import ipaddress
import json
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path
from typing import Final
from urllib.parse import unquote, urlsplit

from zeo_core.integrations.pandoc._runner import find_pandoc, run_pandoc
from zeo_core.integrations.pandoc.refusals import refusal

DEFAULT_MAX_BYTES: Final = 1_000_000
DEFAULT_TIMEOUT_S: Final = 30.0

#: The subset's name; a change to the allowed tags or rules is a new version.
SUBSET_VERSION: Final = "email-subset-1"

#: The Markdown reader: ``markdown_strict`` with raw HTML and every attribute feature
#: off. (``gfm`` and ``commonmark`` ignore ``-raw_html``, so they are not used.)
READER: Final = (
    "markdown_strict-raw_html-raw_attribute-native_divs-native_spans"
    "-link_attributes-header_attributes-auto_identifiers-implicit_figures"
)

#: The fixed flags, apart from the paths pandoc is also given.
FLAGS: Final[tuple[str, ...]] = (
    f"--from={READER}",
    "--to=html5",
    "--wrap=none",
    "--eol=lf",
    "--no-highlight",
)

ALLOWED_TAGS: Final = (
    "h2", "h3", "p", "br", "hr", "ul", "ol", "li", "blockquote",
    "strong", "em", "code", "a", "img",
)  # fmt: skip
_VOID: Final = frozenset({"br", "hr", "img"})
_ATTRS: Final = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"src", "alt", "width", "height"}),
}
_SIZE_MIN: Final = 2
_SIZE_MAX: Final = 10_000
#: C0 controls (but not tab, line feed or carriage return), DEL, the C1 controls, and
#: the bidirectional marks, embeddings, overrides and isolates: none may reach a reader
#: or a link, because each can make text read differently from what it is.
_CONTROL: Final = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]"
)
_DECIMAL_HOST: Final = re.compile(r"^(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+))*$", re.I)
#: Characters that read as nothing: a link whose text is only these has no text.
_BLANK: Final = re.compile(
    "[\\s\u00a0\u200b-\u200d\u2060\ufeff\u00ad\u034f\u115f\u1160\u3164]*"
)
_ORDERED_ITEM: Final = re.compile(r"^ {0,3}(\d{1,9})[.)](?:[ \t]|$)")
_MAILBOX: Final = re.compile(r"^[^@,;\s]+@[^@,;\s]+$")


@dataclass(frozen=True, slots=True)
class RenderedHtml:
    """The rendered issue body, its digest, and the renderer tuple that made it."""

    html: str
    sha256: str
    pandoc_version: str
    renderer: dict[str, object]
    renderer_digest: str
    warnings: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# The second line: the subset check on the HTML
# ---------------------------------------------------------------------------


def _is_ip_host(host: str) -> bool:
    bare = host.strip("[]").rstrip(".")
    try:
        ipaddress.ip_address(bare)
    except ValueError:
        return bool(_DECIMAL_HOST.match(bare))
    return True


def _check_host(host: str, ip_code: str) -> None:
    """A host must be plain ASCII, never percent-encoded, and never an IP address."""
    if not host.isascii():
        raise refusal("link_invalid")
    decoded = host
    for _ in range(3):  # a host can be encoded more than once
        again = unquote(decoded)
        if again == decoded:
            break
        decoded = again
    if decoded != host or "%" in host:
        raise refusal(ip_code if _is_ip_host(decoded) else "link_invalid")
    if _is_ip_host(host):
        raise refusal(ip_code)


def _check_mailto(url: str) -> None:
    """One plain address: no parameters, no empty address, no second recipient."""
    rest = url.partition(":")[2]
    if "?" in rest or "#" in rest:
        raise refusal("link_mailto_query")
    address = unquote(rest)
    if _CONTROL.search(address) or not _MAILBOX.fullmatch(address):
        raise refusal("link_mailto_invalid")


def _check_url(url: str, *, image: bool) -> None:
    """A link or image address: https (links may be mailto), no IP host, no userinfo."""
    scheme_code, ip_code = (
        ("image_scheme", "image_ip_host") if image else ("link_scheme", "link_ip_host")
    )
    if not url or re.search(r"\s", url) or _CONTROL.search(url):
        raise refusal("link_invalid")
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        userinfo = parts.username is not None or parts.password is not None
    except ValueError as error:
        raise refusal("link_invalid") from error
    scheme = parts.scheme.lower()
    if scheme == "mailto" and not image:
        _check_mailto(url)
        return
    if scheme != "https":
        raise refusal(scheme_code)
    if not host:
        raise refusal("link_invalid")
    if userinfo:
        raise refusal("link_userinfo")
    _check_host(host, ip_code)


def _check_image(attrs: dict[str, str]) -> None:
    if not attrs.get("alt", "").strip():
        raise refusal("image_alt_missing")
    for key in ("src", "width", "height"):
        if key not in attrs:
            raise refusal(
                "image_size_missing" if key != "src" else "attribute_not_allowed"
            )
    _check_url(attrs["src"], image=True)
    sizes = []
    for key in ("width", "height"):
        value = attrs[key]
        if not value.isascii() or not value.isdigit():
            raise refusal("image_size_invalid")
        sizes.append(int(value))
    if min(sizes) <= 1:
        raise refusal("image_pixel")
    if max(sizes) > _SIZE_MAX:
        raise refusal("image_size_invalid")


class _SubsetChecker(HTMLParser):
    """Refuses any tag, attribute, link or image outside the email subset."""

    def __init__(self) -> None:
        # Entities are NOT converted: a real ``<`` in the text is always written
        # ``&lt;``, so a bare ``<`` here is a tag that never finished.
        super().__init__(convert_charrefs=False)
        self._stack: list[str] = []
        self._anchors: list[list[str]] = []  # the visible text of each open <a>

    def _text(self, text: str, *, literal: bool = False) -> None:
        if _CONTROL.search(text):
            raise refusal("control_character")
        if "<" in text and not literal:
            raise refusal("html_unbalanced")
        for buffer in self._anchors:
            buffer.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_TAGS:
            raise refusal("tag_not_allowed")
        values: dict[str, str] = {}
        for name, value in attrs:
            if name not in _ATTRS.get(tag, frozenset()) or name in values:
                raise refusal("attribute_not_allowed")
            values[name] = value or ""
        if tag == "a":
            _check_url(values.get("href", ""), image=False)
            self._anchors.append([])
        elif tag == "img":
            _check_image(values)
            self._text(values["alt"])  # an image link's text is its alt text
        if tag not in _VOID:
            self._stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:  # ``<a/>`` is an open tag in HTML, never a closed one
            raise refusal("html_unbalanced")

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID or not self._stack or self._stack.pop() != tag:
            raise refusal("html_unbalanced")
        if tag == "a":
            if _BLANK.fullmatch("".join(self._anchors.pop())):
                raise refusal("link_text_missing")

    def handle_data(self, data: str) -> None:
        self._text(data)

    def handle_entityref(self, name: str) -> None:
        self._text(html_module.unescape(f"&{name};"), literal=True)

    def handle_charref(self, name: str) -> None:
        # The number itself is judged: HTML maps ``&#x85;`` to an ellipsis and drops
        # ``&#1;``, but another reader may take the control character.
        try:
            code = int(name[1:], 16) if name[:1] in "xX" else int(name)
        except ValueError as error:
            raise refusal("html_unbalanced") from error
        if code <= 0x10FFFF and _CONTROL.search(chr(code)):
            raise refusal("control_character")
        self._text(html_module.unescape(f"&#{name};"), literal=True)

    def handle_comment(self, data: str) -> None:
        raise refusal("html_comment")

    def handle_decl(self, decl: str) -> None:
        raise refusal("html_comment")

    def handle_pi(self, data: str) -> None:
        raise refusal("html_comment")

    def unknown_decl(self, data: str) -> None:
        raise refusal("html_comment")

    def finish(self) -> None:
        self.close()
        if self._stack or self._anchors or self.rawdata.strip():
            raise refusal("html_unbalanced")


def check_email_subset(html_text: str) -> None:
    """Refuse HTML that holds anything outside the email subset; return if clean."""
    checker = _SubsetChecker()
    checker.feed(html_text)
    checker.finish()
    # Whatever the parser dropped at the end, a ``<`` is only ever a whole tag.
    if "<" in re.sub(r"<[^<>]*>", "", html_text):
        raise refusal("html_unbalanced")


# ---------------------------------------------------------------------------
# The render
# ---------------------------------------------------------------------------


def _sizes_argument(image_sizes: Mapping[str, tuple[int, int]] | None) -> str | None:
    """The image sizes as ``src<TAB>width<TAB>height`` lines, validated."""
    if not image_sizes:
        return None
    lines = []
    for src, size in sorted(image_sizes.items()):
        if not isinstance(src, str) or not src.startswith("https://"):
            raise refusal("image_sizes_malformed")
        if re.search(r"[\x00-\x1f\x7f]", src):
            raise refusal("image_src_unsupported")
        width, height = size
        if not all(
            isinstance(v, int) and _SIZE_MIN <= v <= _SIZE_MAX for v in (width, height)
        ):
            raise refusal("image_sizes_malformed")
        lines.append(f"{src}\t{width}\t{height}")
    return "\n".join(lines)


def _check_ordered_lists(markdown: str) -> None:
    """
    Refuse an ordered list that does not start at 1.

    ``markdown_strict`` keeps no start number, so ``3.`` would be silently renumbered
    from 1. A line starts a list when it follows a blank line (or the start), or
    continues a list already open; its number must then be 1.
    """
    blank, open_list = True, False
    for line in markdown.split("\n"):
        if not line.strip():
            blank = True
            continue
        found = _ORDERED_ITEM.match(line)
        if found is not None and (blank or open_list):
            if not open_list and int(found.group(1)) != 1:
                raise refusal("ordered_list_start")
            open_list = True
        elif blank and not line.startswith((" ", "\t")):
            open_list = False  # a paragraph after a blank line ends the list
        blank = False


def _check_markdown(markdown: str, max_bytes: int) -> bytes:
    if not isinstance(markdown, str) or not markdown.strip():
        raise refusal("markdown_empty")
    if _CONTROL.search(markdown):
        raise refusal("markdown_invalid")
    data = markdown.encode("utf-8", "replace")
    if len(data) > max_bytes:
        raise refusal("markdown_too_large")
    _check_ordered_lists(markdown)
    return data


def _tuple(
    binary_version: str, binary_sha256: str, filter_sha256: str
) -> dict[str, object]:
    return {
        "pandoc_version": binary_version,
        "pandoc_binary_sha256": binary_sha256,
        "lua_filter_sha256": filter_sha256,
        "data_files": [],
        "data_dir": "empty",
        "reader": READER,
        "writer": "html5",
        "flags": [*FLAGS, "--sandbox", "--data-dir=<empty>", "--lua-filter=<package>"],
        "subset": SUBSET_VERSION,
        "allowed_tags": list(ALLOWED_TAGS),
        "link_schemes": ["https", "mailto"],
        "image_schemes": ["https"],
    }


def _digest_of(renderer: dict[str, object]) -> str:
    canonical = json.dumps(renderer, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _private_filter(work: Path) -> tuple[Path, str]:
    """A private copy of the package's Lua filter, and the digest of that copy."""
    source = resources.files("zeo_core.integrations.pandoc").joinpath(
        "data", "email_subset.lua"
    )
    try:
        data = source.read_bytes()
    except OSError as error:
        raise refusal("filter_missing") from error
    target = work / "email_subset.lua"
    target.write_bytes(data)
    target.chmod(0o400)
    return target, hashlib.sha256(target.read_bytes()).hexdigest()


def render_email_html(
    markdown: str,
    *,
    image_sizes: Mapping[str, tuple[int, int]] | None = None,
    expected_renderer_digest: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> RenderedHtml:
    """
    Render an issue's Markdown to the email subset, deterministically.

    ``image_sizes`` gives each image's width and height by its exact source, because
    Markdown can't carry them once attributes are off; an image with no entry is
    refused. The subset: ``h2 h3 p br hr ul ol li blockquote strong em code``, ``a``
    (https or mailto, link text required) and ``img`` (https, alt, width and height).
    Anything else, including raw HTML typed into the Markdown (kept as text), is
    refused, never rewritten.

    The result carries the renderer tuple: the pandoc version and binary digest, the
    filter digest and the exact flags. The binary and the filter are COPIED into a
    private directory and hashed and run as the copies, so the tuple names the bytes
    that ran. With ``expected_renderer_digest``, a different renderer is refused
    before anything runs.
    """
    data = _check_markdown(markdown, max_bytes)
    sizes = _sizes_argument(image_sizes)
    binary = find_pandoc()
    with tempfile.TemporaryDirectory(prefix="zeocore-filter-") as work:
        filter_path, filter_sha256 = _private_filter(Path(work))
        renderer = _tuple(binary.version, binary.sha256, filter_sha256)
        digest = _digest_of(renderer)
        if expected_renderer_digest is not None and expected_renderer_digest != digest:
            raise refusal("renderer_mismatch")
        flags = [*FLAGS, f"--lua-filter={filter_path}"]
        if sizes is not None:
            flags.append(f"--metadata=zeocore_image_sizes={sizes}")
        out = run_pandoc(
            binary, flags, data, timeout_s=timeout_s, lua_filter=str(filter_path)
        )
    html_text = out.decode("utf-8", "replace")
    check_email_subset(html_text)
    return RenderedHtml(
        html=html_text,
        sha256=hashlib.sha256(html_text.encode("utf-8")).hexdigest(),
        pandoc_version=binary.version,
        renderer=renderer,
        renderer_digest=digest,
    )
