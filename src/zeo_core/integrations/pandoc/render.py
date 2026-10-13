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
from urllib.parse import urlsplit

from zeo_core.integrations.pandoc._runner import find_pandoc, private_copy, run_pandoc
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
_CONTROL: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DECIMAL_HOST: Final = re.compile(r"^(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+))*$", re.I)


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
    bare = host.strip("[]")
    try:
        ipaddress.ip_address(bare)
    except ValueError:
        return bool(_DECIMAL_HOST.match(bare))
    return True


def _check_url(url: str, *, image: bool) -> None:
    """A link or image address: https (links may be mailto), no IP host, no userinfo."""
    scheme_code, ip_code = (
        ("image_scheme", "image_ip_host") if image else ("link_scheme", "link_ip_host")
    )
    if not url or re.search(r"\s", url) or _CONTROL.search(url):
        raise refusal("link_invalid")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme == "mailto" and not image:
        if "?" in url:
            raise refusal("link_mailto_query")
        return
    if scheme != "https":
        raise refusal(scheme_code)
    host = parts.hostname or ""
    if not host:
        raise refusal("link_invalid")
    if parts.username is not None or parts.password is not None:
        raise refusal("link_userinfo")
    if _is_ip_host(host):
        raise refusal(ip_code)


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
        super().__init__(convert_charrefs=True)
        self._stack: list[str] = []

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
        elif tag == "img":
            _check_image(values)
        if tag not in _VOID:
            self._stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID or not self._stack or self._stack.pop() != tag:
            raise refusal("html_unbalanced")

    def handle_data(self, data: str) -> None:
        if _CONTROL.search(data):
            raise refusal("control_character")

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
        if self._stack:
            raise refusal("html_unbalanced")


def check_email_subset(html_text: str) -> None:
    """Refuse HTML that holds anything outside the email subset; return if clean."""
    checker = _SubsetChecker()
    checker.feed(html_text)
    checker.finish()


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


def _check_markdown(markdown: str, max_bytes: int) -> bytes:
    if not isinstance(markdown, str) or not markdown.strip():
        raise refusal("markdown_empty")
    if _CONTROL.search(markdown):
        raise refusal("markdown_invalid")
    data = markdown.encode("utf-8", "replace")
    if len(data) > max_bytes:
        raise refusal("markdown_too_large")
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
    found = find_pandoc()
    binary = private_copy(found)
    with tempfile.TemporaryDirectory(prefix="zeocore-filter-") as work:
        filter_path, filter_sha256 = _private_filter(Path(work))
        renderer = _tuple(binary.version, binary.sha256, filter_sha256)
        digest = _digest_of(renderer)
        if expected_renderer_digest is not None and expected_renderer_digest != digest:
            raise refusal("renderer_mismatch")
        flags = [*FLAGS, f"--lua-filter={filter_path}"]
        if sizes is not None:
            flags.append(f"--metadata=zeocore_image_sizes={sizes}")
        out = run_pandoc(binary, flags, data, timeout_s=timeout_s)
    html_text = out.decode("utf-8", "replace")
    check_email_subset(html_text)
    return RenderedHtml(
        html=html_text,
        sha256=hashlib.sha256(html_text.encode("utf-8")).hexdigest(),
        pandoc_version=binary.version,
        renderer=renderer,
        renderer_digest=digest,
    )
