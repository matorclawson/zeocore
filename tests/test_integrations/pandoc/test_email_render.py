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
import time
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


def test_only_the_verified_private_copy_is_ever_run() -> None:
    source = os.path.realpath(shutil.which("pandoc") or "")
    copy = _runner.find_pandoc()
    assert copy.path != source
    assert copy.sha256 == _runner._sha256_of(source)
    assert (os.stat(copy.path).st_mode & 0o077) == 0  # only this user
    assert (os.stat(Path(copy.path).parent).st_mode & 0o077) == 0
    assert render_email_html("## Hi\n").renderer["pandoc_binary_sha256"] == copy.sha256


def test_the_file_on_path_is_hashed_and_copied_before_anything_is_executed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = os.path.realpath(shutil.which("pandoc") or "")
    monkeypatch.setattr(_runner, "_PRIVATE", {})
    events: list[tuple[str, str]] = []
    real_hash, real_run = _runner._sha256_of, _runner.subprocess.run

    def hashed(path: str) -> str:
        events.append(("hash", path))
        return real_hash(path)

    def ran(argv: list[str], *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        events.append(("run", argv[0]))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(_runner, "_sha256_of", hashed)
    monkeypatch.setattr(_runner.subprocess, "run", ran)
    copy = _runner.find_pandoc()
    assert events[0] == ("hash", source)  # a read of the file on PATH
    assert events[1] == ("hash", copy.path)  # the copy, hashed before it runs
    assert events[2] == ("run", copy.path)  # only the verified copy is executed
    assert all(kind == "hash" or path == copy.path for kind, path in events)


def test_a_file_that_changes_while_it_is_copied_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_runner, "_PRIVATE", {})
    digests = iter(["a" * 64, "b" * 64])
    monkeypatch.setattr(_runner, "_sha256_of", lambda path: next(digests))
    with pytest.raises(NewsletterRefusalError) as caught:
        _runner.find_pandoc()
    assert caught.value.code == "pandoc_failed"


def test_a_modified_copy_is_refused_before_it_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_runner, "_PRIVATE", {})  # a fresh copy; the real one is kept
    copy = _runner.find_pandoc()
    with open(copy.path, "ab") as handle:  # noqa: PTH123
        handle.write(b"\0")
    with pytest.raises(NewsletterRefusalError) as again:
        _runner.find_pandoc()
    assert again.value.code == "pandoc_failed"
    with pytest.raises(NewsletterRefusalError) as ran:
        _runner.run_pandoc(copy, ["--from=markdown"], b"x", timeout_s=5)
    assert ran.value.code == "pandoc_failed"


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


def test_an_ordered_list_that_does_not_start_at_one_is_refused() -> None:
    """``markdown_strict`` keeps no start number, so ``3.`` would be silently ``1.``."""
    assert _code("3. starts at three\n4. next\n") == "ordered_list_start"
    assert _code("Intro\n\n2) starts at two\n3) next\n") == "ordered_list_start"
    assert _code("1. one\n\npara\n\n5. a new list at five\n") == "ordered_list_start"
    html = render_email_html("1. one\n2. two\n\n3. three after a blank\n").html
    assert "<ol>" in html and "start=" not in html  # one list: it is not a new start
    assert "<ol>" not in render_email_html("a paragraph\n3. not a list start\n").html


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
    _runner.find_pandoc()  # warm the copy so only the run is patched

    def slow(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="pandoc", timeout=0.01)

    monkeypatch.setattr(_runner, "_run_child", slow)
    assert _code("## Hi\n", timeout_s=0.01) == "pandoc_timeout"


# --- the subset check, hardened (council L3) ----------------------------------


@pytest.mark.parametrize(
    ("html", "code"),
    [
        ("<p>t</p><script", "html_unbalanced"),
        ("<p>t</p><!-- never closed", "html_comment"),
        ("<p>a < b</p>", "html_unbalanced"),
        ('<p><a href="https://e.com/"/>t</a></p>', "html_unbalanced"),
        ('<p><a href="https://e.com/"> </a></p>', "link_text_missing"),
        ('<p><a href="https://e.com/">​⁠</a></p>', "link_text_missing"),
        ('<p><a href="mailto:">x</a></p>', "link_mailto_invalid"),
        ('<p><a href="mailto:a@e.org,b@e.org">x</a></p>', "link_mailto_invalid"),
        ('<p><a href="mailto:a@e.org%2Cb@e.org">x</a></p>', "link_mailto_invalid"),
        ('<p><a href="mailto:a@e.org#frag">x</a></p>', "link_mailto_query"),
        ('<p><a href="https://127.0.0.1./a">x</a></p>', "link_ip_host"),
        ('<p><a href="https://%31%32%37.0.0.1/a">x</a></p>', "link_ip_host"),
        ('<p><a href="https://0177.0.0.1/a">x</a></p>', "link_ip_host"),
        ('<p><a href="https://2130706433./a">x</a></p>', "link_ip_host"),
        ('<p><a href="https://exa%6Dple.com/a">x</a></p>', "link_invalid"),
        ('<p><a href="https://１２７.0.0.1/a">x</a></p>', "link_invalid"),
        ('<p><a href="https://[::1">x</a></p>', "link_invalid"),
        ('<p><a href="https://e.com/‮">x</a></p>', "link_invalid"),
        ("<p>a\x85b</p>", "control_character"),
        ("<p>a‮b</p>", "control_character"),
        ("<p>a⁦b</p>", "control_character"),
        ("<p>a&#8238;b</p>", "control_character"),
        ("<p>a&#x85;b</p>", "control_character"),
        ("<p>a&#x202e;b</p>", "control_character"),
        ("<u>x</u>", "tag_not_allowed"),
        ("<sub>x</sub>", "tag_not_allowed"),
        ("<figure>x</figure>", "tag_not_allowed"),
        ("<details>x</details>", "tag_not_allowed"),
        ("<pre>x</pre>", "tag_not_allowed"),
        ('<div class="x">x</div>', "tag_not_allowed"),
        ('<span id="y">x</span>', "tag_not_allowed"),
    ],
)
def test_the_hardened_html_check_refuses_by_name(html: str, code: str) -> None:
    with pytest.raises(NewsletterRefusalError) as caught:
        check_email_subset(html)
    assert caught.value.code == code


def test_the_hardened_html_check_still_accepts_what_the_subset_allows() -> None:
    check_email_subset("<p>1 &lt; 2 &amp; 3 &gt; 2, &quot;q&quot;, &#233;</p>")
    check_email_subset('<p><a href="mailto:reader@example.org">write</a></p>')
    check_email_subset(  # an image link: its text is the image's alt text
        '<p><a href="https://e.com/"><img src="https://e.com/i.png" alt="Cover" '
        'width="600" height="300" /></a></p>'
    )
    check_email_subset("<p>one<br />two</p><hr />")


# --- the markdown input (council L3, L6) --------------------------------------


@pytest.mark.parametrize("char", ["\x85", "‮", "⁧", "‏", "\x9f"])
def test_c1_and_bidirectional_controls_in_the_markdown_are_refused(char: str) -> None:
    assert _code(f"## Hi\n\ntext {char} more\n") == "markdown_invalid"


# --- the child process: bounded, in its own group (council L2) ----------------


def _child(code: str, stdin: bytes = b"", **kwargs: Any) -> Any:  # noqa: ANN401
    options: dict[str, Any] = {
        "timeout_s": 10,
        "cwd": os.getcwd(),
        "env": {"PATH": os.environ["PATH"]},
        "out_cap": 10_000_000,
        "err_cap": 10_000_000,
    }
    options.update(kwargs)
    return _runner._run_child([sys.executable, "-c", code], stdin, **options)


def test_a_large_stdin_and_stdout_do_not_deadlock() -> None:
    data = b"x" * 3_000_000
    done = _child("import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())", data)
    assert done.returncode == 0
    assert done.stdout == data


def test_a_child_that_never_reads_its_input_is_not_a_hang() -> None:
    assert _child("pass", b"y" * 3_000_000).returncode == 0


def test_output_past_its_bound_stops_the_child() -> None:
    with pytest.raises(_runner._OutputOverrun) as out:
        _child("import sys;sys.stdout.write('x'*5_000_000)", out_cap=100_000)
    assert out.value.stdout is True
    with pytest.raises(_runner._OutputOverrun) as err:
        _child("import sys;sys.stderr.write('x'*5_000_000)", err_cap=100_000)
    assert err.value.stdout is False


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_a_timeout_kills_everything_the_child_started(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess,sys,time;"
        "p=subprocess.Popen(['sleep','60']);"
        f"open({str(pid_file)!r},'w').write(str(p.pid));"
        "time.sleep(60)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        _child(code, timeout_s=1.5)
    grandchild = int(pid_file.read_text())
    for _ in range(60):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    os.kill(grandchild, 9)  # do not leave it behind if the test is about to fail
    pytest.fail("the grandchild outlived the timeout")


def test_an_oversized_output_is_a_typed_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_runner, "MAX_OUTPUT_BYTES", 10)
    assert _code("## A heading that is longer than ten bytes\n") == "output_too_large"


def test_a_missing_filter_is_a_typed_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Gone:
        def joinpath(self, *parts: str) -> _Gone:
            return self

        def read_bytes(self) -> bytes:
            raise FileNotFoundError("gone")

    monkeypatch.setattr(render.resources, "files", lambda package: _Gone())
    assert _code("## Hi\n") == "filter_missing"


def test_the_reader_we_avoid_leaks_raw_html_which_is_why_it_is_avoided() -> None:
    """A negative control: the gfm reader's ``-raw_html`` is not honoured everywhere."""
    binary = _runner.find_pandoc()
    attack = "<script>alert(1)</script>\n"
    ours = render_email_html(attack).html
    assert "<script" not in ours
    assert "&lt;script&gt;" in ours  # the markup is text in our render
    leaked = subprocess.run(  # noqa: S603
        [binary.path, "--sandbox", "--from=gfm-raw_html", "--to=html5"],
        input=attack.encode(),
        capture_output=True,
        check=False,
    ).stdout
    if b"<script" not in leaked:
        pytest.skip(
            f"pandoc {binary.version}: the gfm reader honours -raw_html here, "
            "so there is no leak to contrast with"
        )
    assert b"<script" in leaked
