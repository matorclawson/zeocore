"""render_email_html: Markdown to the email subset, pinned (ZEOCORE-SOW-33, 5A and 5B).

The golden issue was recorded with pandoc 3.11. Where the installed pandoc differs,
its HTML is compared by shape (tags, attributes and text) and the renderer digest is
expected to differ: a new pandoc is a new renderer, which is the point of the tuple.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from zeo_core.integrations.pandoc import _runner, render
from zeo_core.integrations.pandoc.refusals import NewsletterRefusalError
from zeo_core.integrations.pandoc.render import (
    ALLOWED_TAGS,
    FLAGS,
    READER,
    check_email_subset,
    render_email_html,
)

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN: dict[str, Any] = json.loads(
    (FIXTURES / "issue-five-picks.expected.json").read_text()
)
SIZES = {"https://example.org/img/parse-times.png": (600, 300)}

pytestmark = pytest.mark.skipif(
    shutil.which("pandoc") is None,
    reason="pandoc is not installed; CI installs it with tools/install-pandoc.sh",
)


class _Shape(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[str, ...]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.items.append(("start", tag, *sorted(f"{k}={v}" for k, v in attrs)))

    def handle_endtag(self, tag: str) -> None:
        self.items.append(("end", tag))

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.items.append(("text", " ".join(data.split())))


def _shape(html: str) -> list[tuple[str, ...]]:
    parser = _Shape()
    parser.feed(html)
    return parser.items


def _code(markdown: str, **kwargs: Any) -> str:  # noqa: ANN401
    with pytest.raises(NewsletterRefusalError) as caught:
        render_email_html(markdown, **kwargs)
    return caught.value.code


def test_the_golden_issue() -> None:
    issue = (FIXTURES / "issue-five-picks.md").read_text("utf-8")
    expected = (FIXTURES / "issue-five-picks.expected.html").read_text("utf-8")
    rendered = render_email_html(issue, image_sizes=SIZES)
    if rendered.pandoc_version == GOLDEN["recorded_with_pandoc"]:
        assert rendered.html == expected
        assert rendered.sha256 == GOLDEN["sha256"]
        assert rendered.renderer_digest == GOLDEN["renderer_digest"]
    else:
        assert _shape(rendered.html) == _shape(expected)
        assert rendered.renderer_digest != GOLDEN["renderer_digest"]
    assert rendered.sha256 == hashlib.sha256(rendered.html.encode()).hexdigest()
    assert rendered.pandoc_version == rendered.renderer["pandoc_version"]
    assert rendered.html.count("<h3>") == 5
    assert "<img " in rendered.html


def test_the_renderer_tuple_names_what_ran() -> None:
    rendered = render_email_html("## Hi\n\ntext\n")
    renderer = rendered.renderer
    original = _runner.find_pandoc()
    assert renderer["pandoc_version"] == original.version
    assert renderer["pandoc_binary_sha256"] == original.sha256  # the copy is identical
    filter_bytes = (
        Path(render.__file__).parent / "data" / "email_subset.lua"
    ).read_bytes()
    assert renderer["lua_filter_sha256"] == hashlib.sha256(filter_bytes).hexdigest()
    assert renderer["data_files"] == []
    assert renderer["data_dir"] == "empty"
    assert renderer["reader"] == READER
    flags = renderer["flags"]
    assert isinstance(flags, list)
    for flag in (*FLAGS, "--sandbox", "--data-dir=<empty>", "--lua-filter=<package>"):
        assert flag in flags
    for extension in (
        "raw_html", "raw_attribute", "native_divs", "native_spans", "link_attributes",
        "header_attributes", "auto_identifiers", "implicit_figures",
    ):  # fmt: skip
        assert f"-{extension}" in READER
    assert "--wrap=none" in flags
    assert "--eol=lf" in flags
    assert renderer["allowed_tags"] == list(ALLOWED_TAGS)
    canonical = json.dumps(renderer, sort_keys=True, separators=(",", ":"))
    assert rendered.renderer_digest == hashlib.sha256(canonical.encode()).hexdigest()


def test_the_reader_flags_are_accepted_by_the_installed_pandoc() -> None:
    """Every extension in the argv exists (pandoc refuses unknown ones)."""
    binary = _runner.find_pandoc()
    out = _runner.run_pandoc(binary, [*FLAGS], b"text\n", timeout_s=30)
    assert out.strip() == b"<p>text</p>"
    with pytest.raises(NewsletterRefusalError):  # control: an unsupported one fails
        _runner.run_pandoc(
            binary,
            ["--from=markdown_strict-attributes", "--to=html5"],
            b"x",
            timeout_s=30,
        )


def test_the_renderer_is_deterministic_in_clean_environments(tmp_path: Path) -> None:
    issue = (FIXTURES / "issue-five-picks.md").read_text("utf-8")
    first = render_email_html(issue, image_sizes=SIZES)
    second = render_email_html(issue, image_sizes=SIZES)
    assert first.html == second.html
    program = (
        "import json,sys;"
        "from zeo_core.integrations.pandoc.render import render_email_html;"
        "r=render_email_html(sys.stdin.read(), image_sizes="
        "{'https://example.org/img/parse-times.png': (600, 300)});"
        "sys.stdout.write(json.dumps([r.html, r.renderer_digest]))"
    )
    outputs = []
    for index in range(2):
        work = tmp_path / f"clean-{index}"
        work.mkdir()
        done = subprocess.run(  # noqa: S603
            [sys.executable, "-c", program],
            input=issue,
            capture_output=True,
            text=True,
            check=True,
            cwd=work,
            env={"PATH": os.environ["PATH"], "HOME": str(work)},
        )
        outputs.append(done.stdout)
    assert outputs[0] == outputs[1]
    assert json.loads(outputs[0]) == [first.html, first.renderer_digest]


def test_the_filter_comes_from_the_package_not_the_callers_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "email_subset.lua").write_text("function Pandoc(doc) return doc end")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "email_subset.lua").write_text(
        "function Pandoc(doc) return doc end"
    )
    monkeypatch.chdir(tmp_path)
    assert _code("# A level one heading\n") == "heading_level_1"


def test_an_expected_digest_is_checked_before_anything_runs() -> None:
    rendered = render_email_html("## Hi\n")
    again = render_email_html(
        "## Hi\n", expected_renderer_digest=rendered.renderer_digest
    )
    assert again.sha256 == rendered.sha256
    assert _code("## Hi\n", expected_renderer_digest="0" * 64) == "renderer_mismatch"


def test_the_private_copy_is_what_ran_and_a_changed_binary_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _runner.find_pandoc()
    copy = _runner.private_copy(original)
    assert copy.path != original.path
    assert copy.sha256 == original.sha256
    assert (os.stat(copy.path).st_mode & 0o077) == 0  # only this user
    monkeypatch.setattr(_runner, "_sha256_of", lambda path: "0" * 64)
    monkeypatch.setattr(_runner, "_PRIVATE", {})
    with pytest.raises(NewsletterRefusalError):
        _runner.private_copy(original)  # the copy no longer matches the original


# --- the subset: every refused construct, by name -----------------------------


@pytest.mark.parametrize(
    ("markdown", "code"),
    [
        ("# Level one\n", "heading_level_1"),
        ("#### Level four\n", "heading_level_deep"),
        ("[](https://example.com/x)\n", "link_text_missing"),
        ("![](https://example.org/i.png)\n", "image_alt_missing"),
        ("![no size](https://example.org/i.png)\n", "image_size_missing"),
        ("[x](javascript:alert(1))\n", "link_scheme"),
        ("[x](http://example.com/a)\n", "link_scheme"),
        ("[x](data:text/html,hi)\n", "link_scheme"),
        ("[x](ftp://example.com/a)\n", "link_scheme"),
        ("[x](https://127.0.0.1/a)\n", "link_ip_host"),
        ("[x](https://2130706433/a)\n", "link_ip_host"),
        ("[x](https://0x7f.0.0.1/a)\n", "link_ip_host"),
        ("[x](https://user:pw@example.com/a)\n", "link_userinfo"),
        ("[x](mailto:a@example.org?subject=hi)\n", "link_mailto_query"),
        ("    an indented code block\n", "block_codeblock"),
        ("line one\\\nline two\n\n    code\n", "block_codeblock"),
    ],
)
def test_a_construct_outside_the_subset_is_refused_by_name(
    markdown: str, code: str
) -> None:
    assert _code(markdown, image_sizes=SIZES) == code


def test_an_image_with_a_title_or_an_http_source_is_refused() -> None:
    url = "https://example.org/img/parse-times.png"
    assert _code(f'![a]({url} "t")\n', image_sizes=SIZES) == "attribute_not_allowed"
    http = "http://example.org/i.png"
    assert (
        _code(f"![a]({http})\n", image_sizes={http: (10, 10)})
        == "image_sizes_malformed"
    )


@pytest.mark.parametrize(
    "markdown",
    [
        "<script>alert(1)</script>\n",
        '<div class="x" onclick="y()">block</div>\n',
        'a <b onclick="x()">raw</b> b\n',
        '<img src="https://example.org/i.png" onerror="x()">\n',
        "<!-- a comment -->\n",
        "<style>p{color:red}</style>\n",
        '<iframe src="https://example.com"></iframe>\n',
    ],
)
def test_raw_html_typed_into_the_markdown_stays_text(markdown: str) -> None:
    html = render_email_html(markdown).html
    assert "&lt;" in html
    check_email_subset(html)
    for tag in ("<script", "<div", "<b ", "<img", "<!--", "<style", "<iframe"):
        assert tag not in html


@pytest.mark.parametrize(
    "markdown",
    [
        "| a | b |\n|---|---|\n| 1 | 2 |\n",
        "- [ ] open\n- [x] done\n",
        "Emoji :tada: here\n",
        "A bare address https://example.com/bare here\n",
        "Math $x^2$ and $$y$$\n",
        "~~struck~~ and ^sup^ and ~sub~\n",
        "Term\n: definition\n",
    ],
)
def test_gfm_extension_syntax_is_plain_text_not_a_construct(markdown: str) -> None:
    html = render_email_html(markdown).html
    check_email_subset(html)
    for tag in (
        "<table",
        "<input",
        "<sup",
        "<sub",
        "<del",
        "<dl",
        "<section",
        "<figure",
    ):
        assert tag not in html
    assert "<a " not in html  # a bare address is not turned into a link


def test_a_footnote_reference_becomes_a_link_that_is_refused() -> None:
    """In markdown_strict ``[^1]`` with a definition is a reference link, not a note."""
    assert _code("Footnote[^1]\n\n[^1]: the note\n") == "link_scheme"


def test_an_ordered_list_is_always_numbered_from_one() -> None:
    """markdown_strict keeps no start number, so ``3.`` is the first item."""
    html = render_email_html("3. starts at three\n4. next\n").html
    assert "<ol>" in html and "start=" not in html


def test_the_filter_itself_refuses_a_list_that_does_not_start_at_one() -> None:
    """The guard is for a reader that keeps start numbers; prove it fires on one."""
    source = Path(render.__file__).parent / "data" / "email_subset.lua"
    binary = _runner.find_pandoc()
    flags = ["--from=markdown", "--to=html5", f"--lua-filter={source}"]
    with pytest.raises(NewsletterRefusalError) as caught:
        _runner.run_pandoc(
            binary,
            flags,
            b"3. starts at three\n4. next\n",
            timeout_s=30,
            lua_filter=str(source),
        )
    assert caught.value.code == "ordered_list_start"


def test_pandoc_attribute_syntax_adds_no_attributes() -> None:
    html = render_email_html(
        "## Heading {#custom .cls}\n\n[a](https://example.com/){.btn}\n"
    ).html
    assert 'id="' not in html and 'class="' not in html
    check_email_subset(html)


# --- the second line: the subset check on HTML --------------------------------


@pytest.mark.parametrize(
    ("html", "code"),
    [
        ("<script>x</script>", "tag_not_allowed"),
        ("<h1>t</h1>", "tag_not_allowed"),
        ("<table></table>", "tag_not_allowed"),
        ('<p class="x">t</p>', "attribute_not_allowed"),
        ('<p style="x">t</p>', "attribute_not_allowed"),
        ('<a href="https://e.com/" onclick="x">t</a>', "attribute_not_allowed"),
        (
            '<a href="https://e.com/" href="https://e.org/">t</a>',
            "attribute_not_allowed",
        ),
        ('<a href="javascript:x">t</a>', "link_scheme"),
        ('<a href="https://e.com/ x">t</a>', "link_invalid"),
        ("<a>t</a>", "link_invalid"),
        ('<img src="https://e.com/a" alt="a" width="1" height="1">', "image_pixel"),
        (
            '<img src="https://e.com/a" alt="a" width="abc" height="10">',
            "image_size_invalid",
        ),
        (
            '<img src="https://e.com/a" alt="a" width="20000" height="10">',
            "image_size_invalid",
        ),
        (
            '<img src="https://e.com/a" alt="" width="10" height="10">',
            "image_alt_missing",
        ),
        ('<img src="https://e.com/a" alt="a" height="10">', "image_size_missing"),
        ('<img src="http://e.com/a" alt="a" width="10" height="10">', "image_scheme"),
        (
            '<img src="https://10.0.0.1/a" alt="a" width="10" height="10">',
            "image_ip_host",
        ),
        ('<a href="https://[::1]/a">t</a>', "link_ip_host"),
        ("<p>unclosed", "html_unbalanced"),
        ("<p></b></p>", "html_unbalanced"),
        ("<!-- c --><p>t</p>", "html_comment"),
        ("<!DOCTYPE html><p>t</p>", "html_comment"),
        ("<p>a\x01b</p>", "control_character"),
    ],
)
def test_the_html_check_refuses_by_name(html: str, code: str) -> None:
    with pytest.raises(NewsletterRefusalError) as caught:
        check_email_subset(html)
    assert caught.value.code == code


def test_the_html_check_accepts_the_whole_subset() -> None:
    check_email_subset(
        "<h2>t</h2><h3>t</h3>"
        "<p>a<br />b <strong>c</strong> <em>d</em> <code>e</code></p>"
        "<hr /><ul><li>x</li></ul><ol><li>y</li></ol>"
        "<blockquote><p>q</p></blockquote>"
        '<p><a href="https://example.com/x" title="t">l</a> '
        '<a href="mailto:a@example.org">m</a></p>'
        '<p><img src="https://example.com/i.png" alt="a" '
        'width="600" height="300" /></p>'
    )


# --- inputs ---------------------------------------------------------------------


def test_markdown_input_limits() -> None:
    assert _code("") == "markdown_empty"
    assert _code("   \n") == "markdown_empty"
    assert _code("a\x00b") == "markdown_invalid"
    assert _code("a\x1bb") == "markdown_invalid"
    assert _code("x" * 101, max_bytes=100) == "markdown_too_large"
    assert "tab here" in render_email_html("tab\there\n").html


@pytest.mark.parametrize(
    "sizes",
    [
        {"http://example.org/i.png": (10, 10)},
        {"https://example.org/i.png": (1, 10)},
        {"https://example.org/i.png": (10, 10001)},
        {"https://example.org/i.png": (10.5, 10)},
    ],
)
def test_malformed_image_sizes_are_refused(sizes: dict[str, tuple[int, int]]) -> None:
    assert _code("text\n", image_sizes=sizes) == "image_sizes_malformed"
    assert (
        _code("text\n", image_sizes={"https://e.org/a\tb": (10, 10)})
        == "image_src_unsupported"
    )


def test_a_timeout_is_a_typed_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _runner.private_copy(
        _runner.find_pandoc()
    )  # warm the copy so only the run is patched

    def slow(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="pandoc", timeout=0.01)

    monkeypatch.setattr(_runner.subprocess, "run", slow)
    assert _code("## Hi\n", timeout_s=0.01) == "pandoc_timeout"
