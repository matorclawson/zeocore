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
                 "KEPT-normal-colours", "KEPT-opacity-point-eight",
                 "KEPT-mso-alone"):  # fmt: skip
        assert kept in item.markdown
    kinds = {k: v for k, v in item.stripped.items() if k.startswith("hidden_") and v}
    assert kinds == {
        "hidden_attribute": 1,
        "hidden_display": 1,
        "hidden_visibility": 1,
        "hidden_opacity": 1,
        "hidden_font_size": 1,
        "hidden_max_size": 1,
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


def test_a_style_over_the_bound_is_not_parsed_and_is_treated_as_hidden() -> None:
    """Padding after ``display:none`` must not make hidden text visible (council N3)."""
    long_style = "display:none;" + "a:b;" * 2_000
    item = _convert(f'<p>kept</p><p style="{long_style}">secret instruction</p>')
    assert item.stripped["style_oversize"] == 1
    assert item.stripped["hidden"] == 1
    assert item.stripped["hidden_oversize"] == 1
    assert "secret instruction" not in item.markdown
    assert "kept" in item.markdown


def test_a_style_rule_body_over_the_bound_hides_what_it_selects() -> None:
    pad = "a:b;" * 2_000
    page = (
        f"<style>.h {{ display:none; {pad} }} .v {{ color:red }}</style>"
        '<p class="h">secret instruction</p><p class="v">visible</p>'
    )
    item = _convert(page)
    assert "secret instruction" not in item.markdown
    assert "visible" in item.markdown
    assert item.stripped["hidden_style_class"] == 1


class _CountingDeadline(_runner.Deadline):
    """A deadline that has room for ``room`` checks and refuses the next one."""

    def __init__(self, room: int) -> None:
        super().__init__(30.0)
        self.room = room
        self.checks = 0

    def check(self) -> None:
        self.checks += 1
        if self.checks > self.room:
            raise NewsletterRefusalError("convert_timeout")


def _soup(html: str) -> Any:  # noqa: ANN401
    return _cleaning.bs4_module().BeautifulSoup(html, "html.parser")


@pytest.mark.parametrize(
    "name",
    [
        "drop_tags", "drop_comments", "css_rules", "drop_hidden", "drop_images",
        "flatten_tables", "drop_footers", "clean_links",
    ],
)  # fmt: skip
def test_each_pass_checks_the_deadline_per_element_not_just_once(name: str) -> None:
    """Without the per-element checks these passes finish and never refuse."""
    noise = _noise.load()
    counts = dict.fromkeys(_cleaning.COUNT_KEYS, 0)
    bs4 = _cleaning.bs4_module()
    body = {
        "drop_tags": "<script>x</script>" * 600,
        "drop_comments": "<!-- c -->" * 600,
        "css_rules": "<style>.a{color:red}</style>" * 600,
        "drop_hidden": "<p>x</p>" * 600,
        "drop_images": '<img src="https://example.org/a.png" alt="a">' * 600,
        "flatten_tables": "<table><tr><td>x</td></tr></table>" * 600,
        "drop_footers": "<p>text</p>" * 2000,
        "clean_links": '<a href="https://example.org/">x</a>' * 600,
    }[name]
    soup = _soup(f"<html><body>{body}</body></html>")
    room = _CountingDeadline(3)
    args: tuple[Any, ...] = {
        "drop_tags": (soup, counts, noise, room),
        "drop_comments": (soup, bs4, counts, room),
        "css_rules": (soup, noise, room),
        "drop_hidden": (soup, noise, counts, ({}, {}), room),
        "drop_images": (soup, noise, counts, room),
        "flatten_tables": (soup, counts, room),
        "drop_footers": (soup, noise, counts, room),
        "clean_links": (soup, noise, counts, room),
    }[name]
    with pytest.raises(NewsletterRefusalError) as caught:
        getattr(_cleaning, name)(*args)
    assert caught.value.code == "convert_timeout"
    assert room.checks > 3


def test_the_bounds_pass_checks_the_deadline_while_it_walks() -> None:
    soup = _soup("<div>" + "<p>x</p>" * 3000 + "</div>")
    room = _CountingDeadline(3)
    with pytest.raises(NewsletterRefusalError) as caught:
        _cleaning.check_bounds("", soup, room)
    assert caught.value.code == "convert_timeout"


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


def test_a_fixed_absolute_pandoc_path_replaces_the_path_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller names the binary and its digest; nothing is looked up on PATH."""
    real = shutil.which("pandoc")
    assert real is not None
    monkeypatch.setattr(_runner, "_PRIVATE", {})
    monkeypatch.setenv("PATH", "")  # a PATH lookup would now find nothing
    digest = _runner.find_pandoc(path=real).sha256
    item = convert_newsletter(
        "<p>Hello <a href='https://example.com/x'>there</a></p>",
        source="s",
        sender="a@example.com",
        date="2026-10-18",
        pandoc_path=real,
        expected_pandoc_sha256=digest,
    )
    assert "Hello" in item.markdown
    assert item.converter["pandoc_binary_sha256"] == digest


def test_a_relative_or_mismatched_pandoc_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = shutil.which("pandoc")
    assert real is not None
    monkeypatch.setattr(_runner, "_PRIVATE", {})
    with pytest.raises(NewsletterRefusalError) as relative:
        convert_newsletter(
            "<p>x</p>",
            source="s",
            sender="a@example.com",
            date="d",
            pandoc_path="pandoc",
        )
    assert relative.value.code == "pandoc_missing"
    with pytest.raises(NewsletterRefusalError) as wrong:
        convert_newsletter(
            "<p>x</p>",
            source="s",
            sender="a@example.com",
            date="d",
            pandoc_path=real,
            expected_pandoc_sha256="0" * 64,
        )
    assert wrong.value.code == "renderer_mismatch"


def _visible(markup: str) -> bool:
    item = _convert(markup)
    return "SENTINEL" in item.markdown


def test_max_size_hides_only_a_clipping_box_that_is_not_inline() -> None:
    """Council zc-dtl-webkit-b312-v1: WebKit paints the other cases."""
    # Hidden: a block box that clips.
    assert not _visible('<div style="max-height:0;overflow:hidden">SENTINEL</div>')
    assert not _visible('<p style="max-width:0;overflow:clip">SENTINEL</p>')
    assert not _visible(
        '<span style="display:block;max-height:0;overflow:hidden">SENTINEL</span>'
    )
    # Painted: no clipping, or an inline element, or an explicit inline display.
    assert _visible('<p style="max-height:0">SENTINEL</p>')
    assert _visible('<span style="max-height:0;overflow:hidden">SENTINEL</span>')
    assert _visible(
        '<div style="display:inline;max-height:0;overflow:hidden">SENTINEL</div>'
    )


def test_a_style_rule_with_max_size_follows_the_same_rule() -> None:
    css = "<style>.z{max-height:0;overflow:hidden}.y{max-height:0}</style>"
    assert not _visible(css + '<div class="z">SENTINEL</div>')
    assert _visible(css + '<span class="z">SENTINEL</span>')
    assert _visible(css + '<div class="y">SENTINEL</div>')


def test_mso_hide_alone_is_not_hidden() -> None:
    item = _convert('<p style="mso-hide:all">SENTINEL</p>')
    assert "SENTINEL" in item.markdown
    assert not item.stripped.get("hidden_mso")
    # A different hiding rule next to it still hides.
    assert not _visible('<p style="mso-hide:all;display:none">SENTINEL</p>')


def test_the_webkit_sibling_counterexamples_are_kept_and_their_controls_hidden() -> (
    None
):
    """Council follow-up to zc-dtl-webkit-b312-v1: the eight losses WebKit paints."""
    kept = [
        '<div style="max-width:0">SENTINEL</div>',
        '<div style="height:0">SENTINEL</div>',
        '<div style="line-height:0">SENTINEL</div>',
        '<div style="width:0;height:0">SENTINEL</div>',
        '<div style="left:-9999px">SENTINEL</div>',
        '<div style="top:-9999px">SENTINEL</div>',
        '<span style="text-indent:-9999px">SENTINEL</span>',
        '<div style="clip:rect(0,0,0,0)">SENTINEL</div>',
        '<table><tr><td style="max-height:0;overflow:hidden">'
        "SENTINEL</td></tr></table>",
        '<table><tr><td style="display:table-cell;max-width:0;overflow:hidden">'
        "SENTINEL</td></tr></table>",
    ]
    for markup in kept:
        assert _visible(markup), markup
    hidden = [
        '<div style="width:0;height:0;overflow:hidden">SENTINEL</div>',
        '<div style="position:absolute;left:-9999px">SENTINEL</div>',
        '<div style="position:fixed;top:-9999px">SENTINEL</div>',
        '<div style="text-indent:-9999px;overflow:hidden">SENTINEL</div>',
        '<div style="position:absolute;clip:rect(0,0,0,0)">SENTINEL</div>',
        '<div style="max-height:0;overflow:hidden">SENTINEL</div>',
    ]
    for markup in hidden:
        assert not _visible(markup), markup


# (declarations, tag, hidden in a browser). Council zc-zcore144-765d-v1: an element
# is judged the same way whether the declarations are inline, in a class rule or in
# an id rule.
_PARITY = [
    ("display:block;max-height:0;overflow:hidden", "span", True),
    ("max-height:0;overflow:hidden", "span", False),
    ("max-height:0;overflow:hidden hidden", "div", True),
    ("max-height:0;overflow:clip clip", "div", True),
    ("max-height:0", "div", False),
    ("max-height:0;overflow:hidden", "td", False),
    ("max-height:0;overflow:hidden", "caption", True),
    ("display:table-cell;max-height:0;overflow:hidden", "div", False),
    ("display:table-row;max-width:0;overflow:hidden", "div", False),
    ("width:0;height:0;overflow:hidden", "div", True),
    ("width:0;height:0;overflow:hidden", "span", False),
    ("position:absolute;width:0;height:0;overflow:hidden", "span", True),
    ("float:left;max-height:0;overflow:hidden", "span", True),
    ("line-height:0;overflow:hidden", "div", True),
    ("line-height:0;overflow:hidden", "span", True),
    ("line-height:0", "div", False),
    ("height:0", "div", False),
    ("text-indent:-9999px", "div", True),
    ("text-indent:-9999px", "span", False),
    ("text-indent:-9999px;overflow:hidden", "span", False),
    ("position:relative;left:-9999px", "div", True),
    ("position:sticky;top:-9999px", "div", True),
    ("position:absolute;left:-9999px", "div", True),
    ("left:-9999px", "div", False),
    ("margin-left:-9999px", "div", True),
    ("margin-top:-9999px", "div", True),
    ("position:absolute;clip:rect(0,0,0,0)", "div", True),
    ("position:absolute;clip:rect(0,0,0,0)", "span", True),
    ("position:relative;clip:rect(0,0,0,0)", "div", False),
    ("clip:rect(0,0,0,0)", "div", False),
    ("mso-hide:all", "div", False),
    ("mso-hide:all;display:none", "div", True),
]


@pytest.mark.parametrize(("decls", "tag", "hidden"), _PARITY)
def test_inline_class_and_id_judge_an_element_the_same_way(
    decls: str, tag: str, hidden: bool
) -> None:
    inner = "<tr><td>SENTINEL</td></tr>" if tag == "caption" else "SENTINEL"

    def wrap(open_tag: str) -> str:
        body = f"{open_tag}{inner}</{tag}>"
        if tag in ("td", "caption"):
            body = (
                f"<table>{open_tag}SENTINEL</{tag}></table>"
                if tag == "caption"
                else f"<table><tr>{open_tag}SENTINEL</{tag}></tr></table>"
            )
        return body

    inline = wrap(f'<{tag} style="{decls}">')
    by_class = f"<style>.r{{{decls}}}</style>" + wrap(f'<{tag} class="r">')
    by_id = f"<style>#r{{{decls}}}</style>" + wrap(f'<{tag} id="r">')
    for markup in (inline, by_class, by_id):
        assert _visible(markup) is (not hidden), markup


def test_two_style_blocks_for_one_class_are_merged() -> None:
    markup = (
        "<style>.a{display:block}</style><style>.a{max-height:0;overflow:hidden}</style>"
        '<span class="a">SENTINEL</span>'
    )
    assert not _visible(markup)


def test_an_oversize_style_rule_hides() -> None:
    padding = "x:y;" * 2000
    markup = f"<style>.big{{{padding}}}</style><div class='big'>SENTINEL</div>"
    assert not _visible(markup)


# "Narrow only on proof" (council zc-zcore144-47f4-v2): where something unmodelled
# could set the condition, the text is hidden as before the narrowing.
_UNPROVABLE = [
    # !important in a rule beats an inline value
    (
        "<style>.c{overflow:hidden!important}</style>"
        '<div class="c" style="max-height:0;overflow:visible">SENTINEL</div>'
    ),
    # two class rules disagree: the stylesheet order, not the class order, decides
    (
        "<style>.a{overflow:visible}.b{overflow:hidden}</style>"
        '<div class="a b" style="max-height:0">SENTINEL</div>'
    ),
    # another selector, a universal selector, an element selector, an at-rule
    '<style>div{overflow:hidden}</style><div style="max-height:0">SENTINEL</div>',
    '<style>*{overflow:hidden}</style><div style="max-height:0">SENTINEL</div>',
    ('<style>p{position:absolute}</style><p style="left:-9999px">SENTINEL</p>'),
    (
        "<style>@media screen{.x{overflow:hidden}}</style>"
        '<div class="x" style="max-height:0">SENTINEL</div>'
    ),
    # auto, scroll and contain clip a zero-height box
    '<div style="max-height:0;overflow:auto">SENTINEL</div>',
    '<div style="max-height:0;overflow:scroll">SENTINEL</div>',
    '<div style="max-height:0;contain:paint">SENTINEL</div>',
    # a comment or an escape in a style
    '<span style="max-height:0;/**/overflow:hidden">SENTINEL</span>',
    '<span style="max-height:0;overflow:\\68idden">SENTINEL</span>',
    # an unknown display is not proven inline
    '<span style="display:foo;max-height:0;overflow:hidden">SENTINEL</span>',
]


@pytest.mark.parametrize("markup", _UNPROVABLE)
def test_a_narrowing_applies_only_where_the_condition_is_shown_false(
    markup: str,
) -> None:
    assert not _visible(markup), markup


def test_a_pile_of_rules_for_one_selector_is_bounded_and_hides() -> None:
    rules = "".join(f".p{{width:{n}px}}" for n in range(3000))
    started = time.monotonic()
    item = _convert(f"<style>{rules}</style><div class='p'>SENTINEL</div>")
    assert time.monotonic() - started < 5
    assert "SENTINEL" not in item.markdown


_WHITELIST_ROWS = [
    # kept: the condition is proven false
    ('<p style="max-height:0">SENTINEL</p>', True),
    ('<span style="max-height:0;overflow:hidden">SENTINEL</span>', True),
    (
        '<div><span style="max-height:0;overflow:hidden">SENTINEL</span></div>',
        True,
    ),
    (
        '<table><tr><td style="max-height:0;overflow:hidden">'
        "SENTINEL</td></tr></table>",
        True,
    ),
    # hidden: anything we cannot prove
    ('<p style="max-height:0 ! important;overflow:hidden">SENTINEL</p>', False),
    ('<p style="max-height:0;overflow:hidden;overflow:none">SENTINEL</p>', False),
    ('<p style="max-height:0;overflow:var(--o)">SENTINEL</p>', False),
    ('<p style="max-height:0;overflow:inherit">SENTINEL</p>', False),
    ('<p style="max-height:0;content-visibility:hidden">SENTINEL</p>', False),
    ('<p style="max-height:0;contain:paint">SENTINEL</p>', False),
    ('<p style="max-height:0;all:unset;overflow:hidden">SENTINEL</p>', False),
    (
        '<div style="display:flex"><span style="max-height:0;overflow:hidden">'
        "SENTINEL</span></div>",
        False,
    ),
    (
        '<table><tr><td style="display:block;max-height:0;overflow:hidden">'
        "SENTINEL</td></tr></table>",
        False,
    ),
    (
        "<style>@media print{.x{overflow:visible}}</style>"
        '<p class="x" style="max-height:0;overflow:hidden">SENTINEL</p>',
        False,
    ),
    (
        '<style media="print">.x{overflow:hidden}</style>'
        '<p class="x" style="max-height:0">SENTINEL</p>',
        False,
    ),
    (
        '<link rel="stylesheet" href="a.css"><p style="max-height:0">SENTINEL</p>',
        False,
    ),
    (
        "<style>.x{overflow:hidden}</style>"
        '<p class="X" style="max-height:0">SENTINEL</p>',
        False,
    ),
    (
        '<style>.x{overflow:hidden}</style><p class="x" style="max-height:0">'
        "SENTINEL</p>",
        False,
    ),
]


@pytest.mark.parametrize(("markup", "visible"), _WHITELIST_ROWS)
def test_whitelist_keeps_text_only_on_proof(markup: str, visible: bool) -> None:
    assert _visible(markup) is visible, markup


def test_a_pile_of_repeated_classes_is_bounded_and_hidden() -> None:
    body = "x{max-height:0;overflow:hidden}"
    markup = f'<style>.{body}</style><p class="{" ".join(["x"] * 5000)}">SENTINEL</p>'
    assert not _visible(markup)


_COUNCIL_146_ROWS = [
    # P0-1: every kind of one style is checked, a non-narrowable one hides at once
    ('<div style="height:0;position:absolute;left:-9999px">SENTINEL</div>', False),
    ('<div style="height:0;display:none">SENTINEL</div>', False),
    # P0-2: a `;` inside brackets or quotes does not end a declaration
    (
        '<div style="max-height:0;overflow:hidden;x:(;display:inline;)">SENTINEL</div>',
        False,
    ),
    (
        "<div style=\"max-height:0;overflow:hidden;x:'a;display:inline'\">"
        "SENTINEL</div>",
        False,
    ),
    # P1-3: a closed grammar for every offset value
    ('<span style="text-indent:-9999px each-line">SENTINEL</span>', False),
    ('<span style="text-indent:-9999px hanging">SENTINEL</span>', False),
    # P1-4: text-indent is inherited by block children
    ('<span style="text-indent:-9999px"><div>SENTINEL</div></span>', False),
    # P1-5: a table part under a flex parent is blockified
    (
        '<table><tr style="display:flex"><td style="max-height:0;overflow:hidden">'
        "SENTINEL</td></tr></table>",
        False,
    ),
    # P1-6: two spellings of one class name are merged in quirks mode
    (
        "<style>.foo{overflow:visible}.FOO{overflow:hidden}</style>"
        '<p class="foo" style="max-height:0">SENTINEL</p>',
        False,
    ),
    # P1-7: a browser splits a class on ASCII whitespace only
    (
        "<style>.foo{overflow:hidden}</style>"
        '<p class="foo\u00a0z" style="max-height:0">SENTINEL</p>',
        True,
    ),
    # P1-8: a browser keeps the first of two equal attributes
    ('<div style="display:none" style="color:red">SENTINEL</div>', False),
    # P2-9: any overflow property clips
    ('<p style="max-height:0;overflow-block:hidden">SENTINEL</p>', False),
    ('<textarea style="max-height:0">SENTINEL</textarea>', False),
    # P2-10: a leading comment token or statement at-rule does not eat a selector
    (
        '<style><!-- .pre{display:none} --></style><p class="pre">SENTINEL</p>',
        False,
    ),
    (
        '<style>@layer a;.pre{display:none}</style><p class="pre">SENTINEL</p>',
        False,
    ),
    # zeofetch receipt 0118f101: a preheader is hidden by display; an inline-block with
    # max-width:0 and no clipping is painted
    (
        '<div style="display:none;max-height:0px;max-width:0px;opacity:0;'
        'overflow:hidden">SENTINEL</div>',
        False,
    ),
    ('<div style="display:inline-block;max-width:0">SENTINEL</div>', True),
    # round 2: N1 ordering of parentheses, N2 junk before a rule, N3 blockified cells,
    # N4 a comment before a rule
    (
        '<div style="max-height:0;overflow:hidden;x:)(;display:inline;)(">'
        "SENTINEL</div>",
        False,
    ),
    (
        "<style>.x{max-height:0;overflow:hidden;y:)(;display:inline;)(}</style>"
        '<div class="x">SENTINEL</div>',
        False,
    ),
    (
        "<style>junk;.x{display:inline}</style>"
        '<div class="x" style="max-height:0;overflow:hidden">SENTINEL</div>',
        False,
    ),
    (
        "<style>.x-->{display:inline}</style>"
        '<div class="x" style="max-height:0;overflow:hidden">SENTINEL</div>',
        False,
    ),
    (
        "<style>.x<!--{display:inline}</style>"
        '<div class="x" style="max-height:0;overflow:hidden">SENTINEL</div>',
        False,
    ),
    (
        '<table><tr><td style="float:left;max-height:0;overflow:hidden">'
        "SENTINEL</td></tr></table>",
        False,
    ),
    (
        '<div style="display:table-cell;position:absolute;max-height:0;'
        'overflow:hidden">'
        "SENTINEL</div>",
        False,
    ),
    (
        '<style>/* preheader */ .pre{display:none}</style><p class="pre">SENTINEL</p>',
        False,
    ),
    (
        '<style>@charset "x";/*c*/.pre{display:none}</style>'
        '<p class="pre">SENTINEL</p>',
        False,
    ),
    (
        '<style><!-- .pre{display:none} --></style><p class="pre">SENTINEL</p>',
        False,
    ),
    # round 3: an open bracket in one rule swallows the rules after it in a browser
    (
        "<style>.q{b:url(a}.x{display:inline}</style>"
        '<div class="x" style="max-height:0;overflow:hidden">SENTINEL</div>',
        False,
    ),
    (
        "<style>.q{b:url(a})}.x{display:inline}</style>"
        '<div class="x" style="max-height:0;overflow:hidden">SENTINEL</div>',
        False,
    ),
    (
        "<style>.q{b:(a}.x{display:inline}</style>"
        '<div class="x" style="max-height:0;overflow:hidden">SENTINEL</div>',
        False,
    ),
    (
        "<style>.q{b:[a}.x{display:inline}</style>"
        '<div class="x" style="max-height:0;overflow:hidden">SENTINEL</div>',
        False,
    ),
    # a string is one token: its braces and comment marks are not structure
    (
        '<style>.a{content:"/*"}.pre{display:none}.b{content:"*/"}</style>'
        '<p class="pre">SENTINEL</p>',
        False,
    ),
    (
        "<style>.a{content:'}'}.pre{display:none}</style><p class=\"pre\">SENTINEL</p>",
        False,
    ),
    # round 4: the ORDER of brackets, also across at-rules
    (
        "<style>@media y{.c{d:(}}.x{display:inline}@media x{.a{b:)}}</style>"
        '<div class="x" style="max-height:0;overflow:hidden">SENTINEL</div>',
        False,
    ),
    (
        "<style>@media y{.c{d:[}}.x{display:inline}@media x{.a{b:]}}</style>"
        '<div class="x" style="max-height:0;overflow:hidden">SENTINEL</div>',
        False,
    ),
    (
        "<style>.c{d:(]}.x{display:inline}</style>"
        '<div class="x" style="max-height:0;overflow:hidden">SENTINEL</div>',
        False,
    ),
    # B3 keywords, B4 auto, B7, B8, B10
    ('<p style="max-height:0;overflow:initial">SENTINEL</p>', False),
    ('<p style="max-height:0;overflow:unset">SENTINEL</p>', False),
    ('<p style="max-height:0;overflow:revert">SENTINEL</p>', False),
    ('<p style="max-height:0;overflow:auto">SENTINEL</p>', False),
    (
        "<style>div > p{overflow:hidden}</style>"
        '<div><p style="max-height:0">SENTINEL</p></div>',
        False,
    ),
    (
        "<template><style>.x{overflow:hidden}</style></template>"
        '<p class="x" style="max-height:0">SENTINEL</p>',
        False,
    ),
    (
        '<style type="text/plain">.x{overflow:hidden}</style>'
        '<p class="x" style="max-height:0">SENTINEL</p>',
        False,
    ),
    (
        "<style>@import url(a.css);.x{overflow:hidden}</style>"
        '<p class="x" style="max-height:0">SENTINEL</p>',
        False,
    ),
]


@pytest.mark.parametrize(("markup", "visible"), _COUNCIL_146_ROWS)
def test_council_146_findings(markup: str, visible: bool) -> None:
    assert _visible(markup) is visible, markup


def test_a_sheet_of_many_strings_is_masked_in_linear_time() -> None:
    sheet = ".a{content:" + "''" * 200_000 + "}.pre{display:none}"
    markup = f'<style>{sheet}</style><p class="pre">SENTINEL</p>'
    started = time.monotonic()
    assert not _visible(markup)
    assert time.monotonic() - started < 5
