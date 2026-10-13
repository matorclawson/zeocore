"""decode_raw_message: raw RFC 822 bytes to bodies and headers (ZEOCORE-SOW-33, 2A).

Every message here is built in the test from example.com and example.org addresses.
"""

from __future__ import annotations

import email
from email.message import EmailMessage

import pytest

from zeo_core.integrations.pandoc import _mime
from zeo_core.integrations.pandoc.newsletter import (
    MAX_MIME_DEPTH,
    MAX_MIME_PARTS,
    decode_raw_message,
)
from zeo_core.integrations.pandoc.refusals import NewsletterRefusalError

_HTML = "<html><body><p>Café 日本</p></body></html>"
_TEXT = "Café 日本 plain"


def _alternative() -> EmailMessage:
    message = EmailMessage()
    message["From"] = "News <news@example.org>"
    message["To"] = "reader@example.com"
    message["Subject"] = "A weekly note"
    message["Date"] = "Tue, 13 Oct 2026 08:00:00 +0000"
    message["Message-ID"] = "<abc123@example.org>"
    message["List-Unsubscribe"] = "<https://example.org/unsub?u=1>"
    message.set_content(_TEXT)
    message.add_alternative(_HTML, subtype="html")
    return message


def test_multipart_alternative_gives_both_bodies_and_the_headers() -> None:
    decoded = decode_raw_message(bytes(_alternative()))
    assert decoded.html is not None
    assert "Café 日本" in decoded.html
    assert decoded.text is not None
    assert "plain" in decoded.text
    assert decoded.sender == "News <news@example.org>"
    assert decoded.subject == "A weekly note"
    assert decoded.message_id == "<abc123@example.org>"
    assert decoded.list_unsubscribe == "<https://example.org/unsub?u=1>"
    assert decoded.date == "2026-10-13T08:00:00+00:00"
    assert decoded.warnings == ()


@pytest.mark.parametrize("cte", ["quoted-printable", "base64", "8bit"])
def test_transfer_encodings_are_decoded(cte: str) -> None:
    message = EmailMessage()
    message["From"] = "n@example.org"
    message.set_content(_HTML, subtype="html", cte=cte)
    decoded = decode_raw_message(bytes(message))
    assert decoded.html is not None
    assert "Café 日本" in decoded.html


def test_a_latin1_charset_is_decoded() -> None:
    message = EmailMessage()
    message["From"] = "n@example.org"
    message.set_content("<p>Café</p>", subtype="html", charset="iso-8859-1")
    assert b"iso-8859-1" in bytes(message)
    decoded = decode_raw_message(bytes(message))
    assert decoded.html is not None
    assert "Café" in decoded.html


def test_a_message_with_no_html_part_gives_only_text() -> None:
    message = EmailMessage()
    message["From"] = "n@example.org"
    message.set_content("just text")
    decoded = decode_raw_message(bytes(message))
    assert decoded.html is None
    assert decoded.text is not None
    assert "just text" in decoded.text


def test_a_message_with_no_body_warns() -> None:
    raw = b"From: n@example.org\r\nSubject: empty\r\n\r\n"
    decoded = decode_raw_message(raw)
    assert decoded.html is None
    assert decoded.text in (None, "")
    assert decoded.sender == "n@example.org"


def test_an_attachment_is_ignored_and_never_extracted() -> None:
    message = _alternative()
    message.add_attachment(
        b"ATTACHMENT-SENTINEL-BYTES",
        maintype="application",
        subtype="pdf",
        filename="report.pdf",
    )
    decoded = decode_raw_message(bytes(message))
    assert decoded.html is not None
    fields = (decoded.html, decoded.text, decoded.sender, decoded.subject)
    assert not any("ATTACHMENT-SENTINEL" in str(f) for f in fields)
    assert "report.pdf" not in str(decoded)


def test_an_unparseable_date_is_kept_as_written_with_a_warning() -> None:
    raw = b"From: n@example.org\r\nDate: sometime last week\r\n\r\nbody\r\n"
    decoded = decode_raw_message(raw)
    assert decoded.date == "sometime last week"
    assert any("Date" in w for w in decoded.warnings)


def test_a_missing_header_is_an_empty_string() -> None:
    message = EmailMessage()
    message.set_content("x")
    decoded = decode_raw_message(bytes(message))
    assert decoded.sender == ""
    assert decoded.message_id == ""


def test_headers_are_single_line_and_bounded() -> None:
    message = _alternative()
    del message["Subject"]
    message["Subject"] = "S" * 5000
    decoded = decode_raw_message(bytes(message))
    assert len(decoded.subject) <= 1000
    assert "\n" not in decoded.subject


def test_an_empty_message_is_refused() -> None:
    with pytest.raises(NewsletterRefusalError) as caught:
        decode_raw_message(b"")
    assert caught.value.code == "message_empty"


def test_an_oversized_message_is_refused() -> None:
    with pytest.raises(NewsletterRefusalError) as caught:
        decode_raw_message(b"x" * 1000, max_bytes=999)
    assert caught.value.code == "message_too_large"


def test_too_many_parts_are_refused() -> None:
    message = EmailMessage()
    message["From"] = "n@example.org"
    message.set_content("first")
    for index in range(MAX_MIME_PARTS + 1):
        message.add_attachment(
            f"part {index}".encode(), maintype="text", subtype="plain"
        )
    with pytest.raises(NewsletterRefusalError) as caught:
        decode_raw_message(bytes(message))
    assert caught.value.code == "message_too_complex"


def test_deep_nesting_is_refused() -> None:
    inner = "Content-Type: text/plain\r\n\r\nbottom\r\n"
    for level in range(MAX_MIME_DEPTH + 2):
        boundary = f"b{level}"
        inner = (
            f'Content-Type: multipart/mixed; boundary="{boundary}"\r\n\r\n'
            f"--{boundary}\r\n{inner}\r\n--{boundary}--\r\n"
        )
    raw = ("From: n@example.org\r\n" + inner).encode()
    with pytest.raises(NewsletterRefusalError) as caught:
        decode_raw_message(raw)
    assert caught.value.code == "message_too_complex"


def test_an_unknown_charset_does_not_crash() -> None:
    raw = (
        b"From: n@example.org\r\n"
        b"Content-Type: text/html; charset=x-no-such-charset\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n<p>body</p>\r\n"
    )
    decoded = decode_raw_message(raw)
    assert decoded.sender == "n@example.org"


# --- the sender is a claim (council M2) --------------------------------------

_GMAIL_OK = (
    "Authentication-Results: mx.google.com;\r\n"
    "       dkim=pass header.i=@example.org header.s=s1 header.b=abc;\r\n"
    "       spf=pass (google.com: domain of n@example.org designates 1.2.3.4 as "
    "permitted sender) smtp.mailfrom=n@example.org;\r\n"
    "       dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=example.org\r\n"
)
_GMAIL_FAIL = (
    "Authentication-Results: mx.google.com;\r\n"
    "       dkim=fail header.i=@example.org;\r\n"
    "       spf=softfail smtp.mailfrom=ceo@example.org;\r\n"
    "       dmarc=fail (p=REJECT sp=REJECT dis=NONE) header.from=example.org\r\n"
)
_PLANTED = (
    "Authentication-Results: mx.google.com; dmarc=pass header.from=example.org\r\n"
)


def _raw(top: str, sender: str, *, lower: str = "") -> bytes:
    return (
        f"{top}{lower}From: {sender}\r\nSubject: s\r\n"
        "Content-Type: text/plain\r\n\r\nbody\r\n"
    ).encode()


def test_a_dmarc_pass_aligned_with_from_is_a_verified_sender() -> None:
    decoded = decode_raw_message(_raw(_GMAIL_OK, "News <n@example.org>"))
    assert decoded.authentication["dmarc"] == "pass"
    assert decoded.authentication["dkim"] == "pass"
    assert decoded.authentication["spf"] == "pass"
    assert decoded.authentication["dmarc_header_from"] == "example.org"
    assert decoded.sender_verified is True


def test_a_forged_from_is_not_verified() -> None:
    decoded = decode_raw_message(_raw(_GMAIL_FAIL, "CEO <ceo@example.org>"))
    assert decoded.authentication["dmarc"] == "fail"
    assert decoded.sender_verified is False


def test_a_dmarc_pass_for_another_domain_is_not_verified() -> None:
    decoded = decode_raw_message(_raw(_GMAIL_OK, "Bank <alerts@example.com>"))
    assert decoded.authentication["dmarc"] == "pass"
    assert decoded.sender_verified is False  # the pass was for example.org


def test_a_planted_second_header_cannot_override_gmails() -> None:
    raw = _raw(_GMAIL_FAIL, "CEO <ceo@example.org>", lower=_PLANTED)
    decoded = decode_raw_message(raw)
    assert decoded.authentication["dmarc"] == "fail"
    assert decoded.sender_verified is False


def test_a_planted_header_with_another_authserv_id_is_ignored() -> None:
    top = "Authentication-Results: evil.example; dmarc=pass header.from=example.org\r\n"
    decoded = decode_raw_message(_raw(top, "CEO <ceo@example.org>"))
    assert decoded.authentication == {}
    assert decoded.sender_verified is False


def test_a_planted_header_alone_is_not_gmails_and_no_header_is_unverified() -> None:
    decoded = decode_raw_message(_raw("", "CEO <ceo@example.org>"))
    assert decoded.authentication == {}
    assert decoded.sender_verified is False


# --- every email-module failure is a typed refusal (council L2) ---------------


@pytest.mark.parametrize(
    "error",
    [ValueError("x"), TypeError("x"), RecursionError(), LookupError("x"),
     email.errors.MessageError("x"), AttributeError("x")],
)  # fmt: skip
def test_any_email_module_exception_is_a_typed_refusal(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def broken(*args: object, **kwargs: object) -> object:
        raise error

    monkeypatch.setattr(_mime.email, "message_from_bytes", broken)
    with pytest.raises(NewsletterRefusalError) as caught:
        decode_raw_message(b"From: n@example.org\r\n\r\nbody")
    assert caught.value.code == "message_too_complex"


def test_oversized_headers_and_too_many_headers_are_refused() -> None:
    big = b"From: n@example.org\r\nX-Pad: " + b"a" * (_mime.MAX_HEADER_BYTES + 10)
    with pytest.raises(NewsletterRefusalError) as huge:
        decode_raw_message(big + b"\r\n\r\nbody")
    assert huge.value.code == "message_too_complex"
    many = b"".join(b"X-%d: v\r\n" % i for i in range(_mime.MAX_HEADERS + 5))
    with pytest.raises(NewsletterRefusalError) as lots:
        decode_raw_message(b"From: n@example.org\r\n" + many + b"\r\nbody")
    assert lots.value.code == "message_too_complex"


def test_list_unsubscribe_is_data_not_an_action() -> None:
    decoded = decode_raw_message(bytes(_alternative()))
    assert isinstance(decoded.list_unsubscribe, str)
    assert "unsub" in decoded.list_unsubscribe  # kept as text for the record only
