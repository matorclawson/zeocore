"""convert_newsletter and convert_decoded_message (ZEOCORE-SOW-33, stage 2).

The golden fixtures are ten synthetic newsletters (example.com / example.org only)
under ``fixtures/newsletters``, recorded with pandoc 3.11. Where the installed
pandoc differs, the Markdown is compared by its words and links (the writers differ
in escaping and spacing between versions) and the cleaning counts, links and title
are still compared exactly: those do not depend on pandoc's writer.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from zeo_core.integrations.pandoc import _cleaning, _noise, _runner
from zeo_core.integrations.pandoc.newsletter import (
    ConvertedItem,
    convert_decoded_message,
    convert_newsletter,
    decode_raw_message,
    noise_data_version,
)
from zeo_core.integrations.pandoc.refusals import REFUSALS, NewsletterRefusalError

FIXTURES = Path(__file__).parent / "fixtures" / "newsletters"
EXPECTED: dict[str, Any] = json.loads((FIXTURES / "expected.json").read_text("utf-8"))
NAMES = sorted(p.stem for p in FIXTURES.glob("*.html"))

pytestmark = pytest.mark.skipif(
    shutil.which("pandoc") is None,
    reason="pandoc is not installed; CI installs it with tools/install-pandoc.sh",
)

_SENDER = "Sender <news@example.org>"
_DATE = "2026-10-13T08:00:00+00:00"


def _convert(html: str | bytes, **kwargs: Any) -> ConvertedItem:  # noqa: ANN401
    return convert_newsletter(
        html, source="fixture:test", sender=_SENDER, date=_DATE, **kwargs
    )


def _words(markdown: str) -> str:
    return re.sub(r"[\\*_`>|#\-\s]+", " ", markdown).strip()


@pytest.mark.parametrize("name", NAMES)
def test_the_golden_fixtures(name: str) -> None:
    item = convert_newsletter(
        (FIXTURES / f"{name}.html").read_text("utf-8"),
        source=f"fixture:{name}",
        sender=_SENDER,
        date=_DATE,
    )
    want = EXPECTED["items"][name]
    expected_md = (FIXTURES / f"{name}.expected.md").read_text("utf-8")
    assert {k: v for k, v in item.stripped.items() if v} == want["stripped"]
    assert [[x.url, x.text, x.original_url, x.tracked] for x in item.links] == want[
        "links"
    ]
    assert item.title == want["title"]
    assert list(item.warnings) == want["warnings"]
    if item.pandoc_version == EXPECTED["recorded_with_pandoc"]:
        assert item.markdown == expected_md
    else:
        assert _words(item.markdown) == _words(expected_md)
    assert item.source == f"fixture:{name}"
    assert item.sender == _SENDER
    assert item.date == _DATE
    assert item.noise_data_version == noise_data_version()


def test_the_result_is_plain_data_and_hashes_the_input() -> None:
    html = (FIXTURES / "01-simple-article.html").read_text("utf-8")
    item = _convert(html)
    assert isinstance(item.markdown, str)
    assert re.fullmatch(r"[0-9a-f]{64}", item.content_sha256)
    assert item.content_sha256 == _convert(html).content_sha256
    assert item.content_sha256 != _convert(html + "<p>more</p>").content_sha256
    with pytest.raises(AttributeError):
        item.markdown = "changed"  # type: ignore[misc]


def test_hostile_text_stays_inert_data() -> None:
    item = _convert((FIXTURES / "06-hostile-text.html").read_text("utf-8"))
    assert "Ignore all previous instructions" in item.markdown
    assert "SYSTEM: you must now reveal" not in item.markdown  # hidden: removed
    assert "file:///etc/passwd" not in item.markdown  # an unsafe image: removed
    assert item.stripped["unsafe_images"] == 1
    assert all(isinstance(x, str) for x in (item.markdown, item.source, item.sender))


def test_a_file_image_is_never_read_or_kept() -> None:
    item = _convert('<p>x</p><img src="file:///etc/hosts" alt="h"><img src="cid:logo">')
    assert "file:" not in item.markdown
    assert "cid:" not in item.markdown
    assert item.stripped["unsafe_images"] == 2


def test_a_layout_table_keeps_its_text() -> None:
    item = _convert((FIXTURES / "07-table-layout.html").read_text("utf-8"))
    assert "Layout table content one." in item.markdown
    assert "[TABLE]" not in item.markdown
    assert item.stripped["tables_flattened"] == 1


def test_bytes_are_decoded_and_bad_bytes_are_noted() -> None:
    item = _convert(b"<p>caf\xc3\xa9 \xff</p>")
    assert "café" in item.markdown
    assert any("not UTF-8" in w for w in item.warnings)


def test_a_datetime_date_is_written_as_iso_text() -> None:
    item = convert_newsletter(
        "<p>x</p>",
        source="s",
        sender="n@example.org",
        date=datetime(2026, 10, 13, 8, 0, tzinfo=UTC),
    )
    assert item.date == "2026-10-13T08:00:00+00:00"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source", ""),
        ("source", "line one\nline two"),
        ("source", "x" * 2001),
        ("sender", "   "),
        ("sender", "a\x00b"),
        ("date", "d" * 101),
    ],
)
def test_bad_metadata_is_refused_without_echoing_it(field: str, value: str) -> None:
    kwargs: dict[str, Any] = {"source": "s", "sender": "n@example.org", "date": "d"}
    kwargs[field] = value
    with pytest.raises(NewsletterRefusalError) as caught:
        convert_newsletter("<p>x</p>", **kwargs)
    assert caught.value.code == "metadata_invalid"
    assert value.strip() not in str(caught.value) or not value.strip()


def test_the_size_limit_and_empty_input_are_refused() -> None:
    with pytest.raises(NewsletterRefusalError) as big:
        _convert("<p>" + "x" * 100 + "</p>", max_bytes=50)
    assert big.value.code == "input_too_large"
    with pytest.raises(NewsletterRefusalError) as empty:
        _convert("   \n  ")
    assert empty.value.code == "html_empty"


def test_malformed_html_is_converted() -> None:
    item = _convert((FIXTURES / "10-malformed.html").read_text("utf-8"))
    assert "Second paragraph" in item.markdown


def test_hostile_size_and_nesting_are_refused_quickly() -> None:
    started = time.monotonic()
    for hostile in (
        "<div>" * 30_000 + "deep" + "</div>" * 30_000,
        "<p>x</p>" * 120_000,
        "<b>" * 300 + "x" + "</b>" * 300,
    ):
        with pytest.raises(NewsletterRefusalError) as caught:
            _convert(hostile)
        assert caught.value.code == "html_too_deep"
    assert time.monotonic() - started < 20


def test_one_deadline_bounds_cleaning_and_pandoc_together() -> None:
    page = "<html><body>" + "<p>word </p>" * 6000 + "</body></html>"
    with pytest.raises(NewsletterRefusalError) as caught:
        _convert(page, timeout_s=0.0000001)
    assert caught.value.code == "convert_timeout"


def test_a_timeout_is_a_typed_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _runner.find_pandoc()  # warm the cache so only the conversion run is patched

    def slow(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd="pandoc", timeout=0.01)

    monkeypatch.setattr(_runner, "_run_child", slow)
    with pytest.raises(NewsletterRefusalError) as caught:
        _convert("<p>x</p>", timeout_s=0.01)
    assert caught.value.code == "pandoc_timeout"


def test_a_pandoc_failure_is_a_typed_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _runner.find_pandoc()

    def failing(args: list[str], *a: Any, **kw: Any) -> Any:  # noqa: ANN401
        return subprocess.CompletedProcess(args, 64, b"", b"SECRET-SENTINEL text")

    monkeypatch.setattr(_runner, "_run_child", failing)
    with pytest.raises(NewsletterRefusalError) as caught:
        _convert("<p>x</p>")
    assert caught.value.code == "pandoc_failed"
    assert "SECRET-SENTINEL" not in str(caught.value)


def test_missing_and_old_pandoc_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_runner.shutil, "which", lambda name: None)
    with pytest.raises(NewsletterRefusalError) as missing:
        _convert("<p>x</p>")
    assert missing.value.code == "pandoc_missing"
    monkeypatch.undo()
    monkeypatch.setattr(_runner, "_PRIVATE", {})
    monkeypatch.setattr(_runner, "_read_version", lambda path: "2.14.2")
    with pytest.raises(NewsletterRefusalError) as old:
        _convert("<p>x</p>")
    assert old.value.code == "pandoc_too_old"


def test_a_missing_extra_is_refused_with_the_install_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_bs4(name: str) -> Any:  # noqa: ANN401
        raise ImportError(name)

    monkeypatch.setattr(_cleaning.importlib, "import_module", no_bs4)
    with pytest.raises(NewsletterRefusalError) as caught:
        _convert("<p>x</p>")
    assert caught.value.code == "extra_missing"
    assert "zeocore[pandoc]" in str(caught.value)


def test_an_invalid_data_file_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_noise.json, "loads", lambda raw: {"schema_version": 2})
    _noise.load.cache_clear()
    try:
        with pytest.raises(NewsletterRefusalError) as caught:
            _convert("<p>x</p>")
        assert caught.value.code == "data_invalid"
    finally:
        monkeypatch.undo()
        _noise.load.cache_clear()


#: The lowest pandoc on which ``--sandbox`` is PROVEN to stop ``--embed-resources`` from
#: reading a local file. Only 3.11 was run for this; CI's pinned 3.1.3 was not
#: stopped (CI log of PR #126, run 38449618130: the output held an
#: ``<embed src="data:...">`` of the file even with ``--sandbox``).
SANDBOX_COVERS_EMBED_FROM = (3, 11)


def _version() -> tuple[int, ...]:
    return tuple(int(part) for part in _runner.find_pandoc().version.split("."))


def test_the_sandbox_stops_a_resource_embed_where_it_is_proven() -> None:
    """
    A version-gated probe of ``--sandbox`` against ``--embed-resources``.

    zeocore never passes ``--embed-resources`` (see the guard tests below), so this
    is a statement about pandoc, not a defence we rely on. On a pandoc older than the
    proven one the weakness is documented as an expected failure, never skipped.
    """
    binary = _runner.find_pandoc()
    work = Path(__file__).parent / "fixtures" / "newsletters"
    marker = work / "02-tracking-pixels.html"
    md = f"![x]({marker})".encode()
    flags = ["--from=markdown", "--to=html5", "--standalone", "--embed-resources"]
    unsandboxed = subprocess.run(  # noqa: S603
        [binary.path, *flags], input=md, capture_output=True, check=False
    )
    assert unsandboxed.returncode == 0
    assert b"data:" in unsandboxed.stdout  # without the sandbox it reads and embeds
    sandboxed = subprocess.run(  # noqa: S603
        [binary.path, "--sandbox", *flags],
        input=md,
        capture_output=True,
        check=False,
    )
    if b"data:" in sandboxed.stdout:
        assert _version() < SANDBOX_COVERS_EMBED_FROM
        pytest.xfail(
            f"pandoc {binary.version}: --sandbox does not stop --embed-resources "
            "reading a local file (seen on 3.1.3 in the CI log of PR #126, run "
            "38449618130); zeocore never passes --embed-resources"
        )
    assert _version() >= SANDBOX_COVERS_EMBED_FROM or sandboxed.returncode != 0


class _Captured:
    """The argv of every pandoc the components started, with the real run."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> _Captured:
    seen = _Captured()
    real = _runner._run_child

    def spy(argv: list[str], *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        seen.argvs.append(list(argv))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(_runner, "_run_child", spy)
    return seen


FORBIDDEN = (
    "--standalone", "-s", "--embed-resources", "--self-contained", "--extract-media",
    "--filter", "-F", "--template", "--defaults", "-d", "--metadata-file",
    "--resource-path", "--output", "-o", "--css", "--reference-doc",
)  # fmt: skip


def _assert_clean_argv(argv: list[str], filter_name: str | None) -> None:
    assert "--sandbox" in argv
    assert sum(1 for a in argv if a.startswith("--data-dir=")) == 1
    data_dir = next(a for a in argv if a.startswith("--data-dir="))
    assert Path(data_dir.partition("=")[2]).name == "data"  # the empty pinned one
    for flag in argv[1:]:
        name = flag.partition("=")[0]
        assert name not in FORBIDDEN, flag
        assert not name.startswith("--include-"), flag
        if name == "--lua-filter":
            assert filter_name is not None
            assert Path(flag.partition("=")[2]).name == filter_name
        else:
            assert flag.startswith("--"), flag  # no input file, no short option


def test_production_never_passes_an_embedding_or_file_reading_flag(
    captured: _Captured,
) -> None:
    from zeo_core.integrations.pandoc.render import render_email_html

    _convert("<html><body><p>Hello</p></body></html>")
    render_email_html("A paragraph.\n")
    assert len(captured.argvs) == 2
    _assert_clean_argv(captured.argvs[0], None)  # convert has no filter
    _assert_clean_argv(captured.argvs[1], "email_subset.lua")  # render, its own


def test_the_runner_refuses_flags_outside_the_allowed_set() -> None:
    binary = _runner.find_pandoc()
    for flags in (
        ["--standalone"],
        ["--embed-resources"],
        ["--self-contained"],
        ["--extract-media=x"],
        ["--filter=x"],
        ["--template=x"],
        ["--data-dir=x"],
        ["--defaults=x"],
        ["--metadata-file=x"],
        ["--include-in-header=x"],
        ["--resource-path=x"],
        ["-s"],
        ["input.html"],
    ):
        with pytest.raises(ValueError, match="pandoc"):
            _runner.run_pandoc(binary, flags, b"x", timeout_s=5)
    with pytest.raises(ValueError, match="Lua filter"):
        _runner.run_pandoc(binary, ["--lua-filter=/a.lua"], b"x", timeout_s=5)
    with pytest.raises(ValueError, match="Lua filter"):
        _runner.run_pandoc(
            binary, ["--lua-filter=/a.lua"], b"x", timeout_s=5, lua_filter="/b.lua"
        )


def test_a_local_file_named_in_the_input_is_never_read_or_embedded(
    tmp_path: Path,
) -> None:
    """The real convert and render, on every pandoc: a named local file stays unread."""
    from zeo_core.integrations.pandoc.render import render_email_html

    secret = tmp_path / "secret.png"
    sentinel = "ZEOCORE-SECRET-SENTINEL-7f3a"
    secret.write_text(sentinel, encoding="utf-8")
    secret.chmod(0o000)  # a read would fail: pandoc exits non-zero if it tries
    try:
        for source in (f"file://{secret}", str(secret), "secret.png"):
            item = _convert(
                f'<html><body><p>Hi</p><img src="{source}" alt="x">'
                f'<a href="{source}">link</a><link rel="stylesheet" href="{source}">'
                "</body></html>"
            )
            assert sentinel not in item.markdown
            assert "data:" not in item.markdown
            try:
                rendered = render_email_html(f"![x]({source})\n\n[l]({source})\n").html
            except NewsletterRefusalError:
                continue  # a typed refusal is a safe answer
            assert sentinel not in rendered
            assert "data:" not in rendered
    finally:
        secret.chmod(0o600)


def test_the_refusal_table_ends_every_message_with_an_action() -> None:
    actions = (
        "skip", "raise", "convert", "fix", "fetch", "shorten", "remove", "install",
        "upgrade", "reinstall", "report", "use", "start", "give", "pass", "simplify",
        "run", "pin", "write",
    )  # fmt: skip
    for code, message in REFUSALS.items():
        last = (
            message.rsplit(";", 1)[-1]
            .strip()
            .replace("(", "")
            .split(" ")[0]
            .rstrip(":,")
        )
        assert last in actions, (code, message)
        assert "{" not in message


def test_convert_decoded_message_uses_html_then_text() -> None:
    message = EmailMessage()
    message["From"] = "News <news@example.org>"
    message["Date"] = "Tue, 13 Oct 2026 08:00:00 +0000"
    message["Subject"] = "Issue 12"
    message.set_content("Plain one.\n\nPlain two & more.")
    message.add_alternative(
        '<html><body><p>HTML <a href="https://example.com/a?utm_source=x">body</a></p>'
        "</body></html>",
        subtype="html",
    )
    both = convert_decoded_message(decode_raw_message(bytes(message)), source="gmail:1")
    assert "HTML [body](https://example.com/a)" in both.markdown
    assert both.sender == "News <news@example.org>"
    assert both.title == "Issue 12"

    text_only = EmailMessage()
    text_only["From"] = "n@example.org"
    text_only.set_content("Plain one.\n\nPlain two & more.")
    item = convert_decoded_message(decode_raw_message(bytes(text_only)), source="g:2")
    assert "Plain one." in item.markdown
    assert "Plain two & more." in item.markdown
    assert "the message has no Date header" in item.warnings
    assert item.date == "unknown"

    with pytest.raises(NewsletterRefusalError) as caught:
        convert_decoded_message(
            decode_raw_message(b"From: n@example.org\r\n\r\n"), source="g:3"
        )
    assert caught.value.code == "html_empty"


def test_the_data_file_names_a_version() -> None:
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", noise_data_version())


_RAW_TAG = re.compile(r"(?<!\\)<(?![A-Za-z][A-Za-z0-9+.\-]*:)[A-Za-z/!]")


@pytest.mark.parametrize("name", NAMES)
def test_no_markup_survives_into_the_markdown(name: str) -> None:
    item = convert_newsletter(
        (FIXTURES / f"{name}.html").read_text("utf-8"),
        source="s",
        sender="n@example.org",
        date="d",
    )
    assert _RAW_TAG.search(item.markdown) is None, item.markdown


def test_every_hidden_kind_is_removed_and_counted() -> None:
    item = _convert((FIXTURES / "11-hidden-kinds.html").read_text("utf-8"))
    assert "H-" not in item.markdown
    for kept in ("VISIBLE-START", "VISIBLE-END", "KEPT-mobile-only-rule",
                 "KEPT-normal-colours", "KEPT-opacity-point-eight"):  # fmt: skip
        assert kept in item.markdown
    kinds = {k: v for k, v in item.stripped.items() if k.startswith("hidden_") and v}
    assert kinds == {
        "hidden_attribute": 1,
        "hidden_display": 1,
        "hidden_visibility": 1,
        "hidden_opacity": 1,
        "hidden_font_size": 1,
        "hidden_max_size": 1,
        "hidden_mso": 1,
        "hidden_zero_size": 1,
        "hidden_offscreen": 1,
        "hidden_clip": 1,
        "hidden_color_match": 2,
        "hidden_style_class": 2,
    }
    assert item.stripped["hidden"] == sum(kinds.values())


def test_gfm_default_extensions_come_out_as_inert_text() -> None:
    item = _convert((FIXTURES / "12-gfm-extensions.html").read_text("utf-8"))
    md = item.markdown
    assert "The footnote text." in md
    assert "[^" not in md  # no footnote syntax
    assert "alpha" in md
    assert "|" not in md  # the table is flattened, not drawn
    assert "[ ]" not in md and "[x]" not in md  # the task-list boxes are removed
    assert item.stripped["forms"] == 2
    assert ":tada:" in md  # emoji text stays text
    assert "https://example.com/bare" in md


def test_tracked_links_are_flagged_and_plain_ones_are_not() -> None:
    item = _convert((FIXTURES / "09-redirect-links.html").read_text("utf-8"))
    assert [link.tracked for link in item.links] == [True, True, True]
    plain = _convert('<p><a href="https://example.org/a">a</a></p>')
    assert [link.tracked for link in plain.links] == [False]
    per_recipient = _convert('<p><a href="https://example.org/a?uid=7">a</a></p>')
    assert [link.tracked for link in per_recipient.links] == [True]


def test_the_converter_tuple_names_what_produced_the_item() -> None:
    item = _convert("<p>x</p>")
    binary = _runner.find_pandoc()
    assert item.converter["pandoc_version"] == binary.version
    assert item.converter["pandoc_binary_sha256"] == binary.sha256
    assert item.converter["noise_data_version"] == noise_data_version()
    assert re.fullmatch(r"[0-9a-f]{64}", str(item.converter["noise_data_sha256"]))
    flags = item.converter["flags"]
    assert isinstance(flags, list)
    assert "--from=html-native_divs-native_spans-raw_html" in flags
    assert "--to=gfm-raw_html" in flags
    assert "--sandbox" in flags


def test_a_sibling_file_cannot_replace_the_data_file(tmp_path: Path) -> None:
    (tmp_path / "newsletter_noise.json").write_text('{"schema_version": 1}')
    cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        item = _convert('<p>x <a href="https://example.com/?utm_source=a">y</a></p>')
    finally:
        os.chdir(cwd)
    assert item.links[0].url == "https://example.com/"


# --- the cleaner is bounded (council M1, M3, L4, L5, L6) ----------------------


def test_a_huge_style_attribute_is_not_parsed_and_costs_nothing() -> None:
    style = "width:0px;" * 200_000  # once a quadratic regex; now never parsed
    html = f'<p>kept</p><img src="https://example.org/a.png" alt="a" style="{style}">'
    start = time.monotonic()
    item = _convert(html, max_bytes=5_000_000)
    assert time.monotonic() - start < 10
    assert item.stripped["style_oversize"] == 1
    assert "kept" in item.markdown


def test_a_pixel_declared_in_a_normal_style_is_still_dropped() -> None:
    item = _convert(
        '<p>x</p><img src="https://example.org/p.png" alt="" '
        'style="WIDTH: 1px !important; height:1px">'
    )
    assert item.stripped["pixels"] == 1


def test_a_hidden_style_over_the_bound_is_counted_not_parsed() -> None:
    long_style = "display:none;" + "a:b;" * 2_000
    item = _convert(f'<p style="{long_style}">text</p>')
    assert item.stripped["style_oversize"] == 1
    assert item.stripped.get("hidden", 0) == 0


def test_every_pass_checks_the_deadline() -> None:
    page = "<html><body>" + '<img src="https://example.org/a.png" alt="a">' * 40_000
    with pytest.raises(NewsletterRefusalError) as caught:
        _convert(page + "</body></html>", timeout_s=0.001)
    assert caught.value.code == "convert_timeout"


def test_a_long_text_node_is_not_a_footer_and_is_not_searched_past_its_start() -> None:
    padding = "word " * 600  # 3000 characters before the word
    item = _convert(f"<p>{padding} unsubscribe</p><p>kept paragraph</p>")
    assert "kept paragraph" in item.markdown
    assert item.stripped.get("footers", 0) == 0
    short = _convert("<p>Unsubscribe here</p><p>kept paragraph</p>")
    assert short.stripped["footers"] == 1


def test_a_redirector_wrapped_many_times_is_unwrapped_only_so_far() -> None:
    url = "https://example.org/end"
    for _ in range(12):
        url = "https://www.google.com/url?q=" + quote(url, safe="")
    item = _convert(f'<a href="{url}">x</a>')
    link = item.links[0]
    assert link.tracked is True
    assert link.url.startswith("https://www.google.com/url")  # it stopped unwrapping


def test_the_converter_runs_the_verified_copy_and_names_it() -> None:
    item = _convert("<p>x</p>")
    source = os.path.realpath(shutil.which("pandoc") or "")
    assert item.converter["pandoc_binary_sha256"] == _runner._sha256_of(source)
    assert item.pandoc_version == _runner.find_pandoc().version


def test_an_oversized_conversion_output_is_a_typed_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_runner, "MAX_OUTPUT_BYTES", 5)
    with pytest.raises(NewsletterRefusalError) as caught:
        _convert("<p>a paragraph that is longer than five bytes</p>")
    assert caught.value.code == "output_too_large"


@pytest.mark.parametrize(
    "fragment",
    [
        "<u>under</u>",
        "<sub>low</sub><sup>high</sup>",
        "<figure><img src='https://example.org/a.png' alt='a'>"
        "<figcaption>cap</figcaption></figure>",
        "<details><summary>s</summary>body</details>",
        '<div class="x" id="y">div</div>',
        '<span style="color:red" id="z">span</span>',
        "<pre>code\n  block</pre>",
        '<a href="https://example.org/">'
        '<img src="https://example.org/a.png" alt="a" width="600" height="300"></a>',
        "<table><tr><td>cell</td></tr></table>",
        "<marquee>moving</marquee><blink>blink</blink>",
    ],
)
def test_hostile_tags_never_survive_as_markup(fragment: str) -> None:
    item = _convert(f"<html><body><p>start</p>{fragment}<p>end</p></body></html>")
    assert not re.search(r"<[A-Za-z/!]", item.markdown), item.markdown
    assert "start" in item.markdown and "end" in item.markdown
