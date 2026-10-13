"""
Decoding one raw RFC 822 message (ZEOCORE-SOW-33, section 2A).

Standard-library ``email`` only: charset and transfer decoding. No attachment is
extracted, no link is followed, nothing is fetched. The message is bounded before
and while it is read (bytes, header bytes, number of headers, parts and nesting),
and every exception the email package can raise on a hostile message becomes a
typed refusal, so a caller sees ``NewsletterRefusalError`` or a result, never a
traceback.

The sender is a CLAIM. ``From`` can say anything, so ``DecodedMessage`` also carries
what Gmail's own mail server concluded: the TOPMOST ``Authentication-Results`` header
only, and only when it names ``mx.google.com`` as its authserv-id. Gmail prepends its
own header, so every header below it was written by the sender or a hop on the way,
and is ignored. ``sender_verified`` is True only for ``dmarc=pass`` where the domain
DMARC checked equals the domain in ``From``.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import re
from dataclasses import dataclass, field
from typing import Any, Final

from zeo_core.integrations.pandoc.refusals import refusal

RAW_MAX_BYTES: Final = 10_485_760
MAX_HEADER_BYTES: Final = 262_144
MAX_HEADERS: Final = 1_000
MAX_MIME_PARTS: Final = 64
MAX_MIME_DEPTH: Final = 8

#: The only authserv-id whose Authentication-Results is believed.
TRUSTED_AUTHSERV_ID: Final = "mx.google.com"

_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")
_METHOD: Final = re.compile(r"^\s*(dkim|spf|dmarc)\s*=\s*([a-z]+)", re.IGNORECASE)
_HEADER_FROM: Final = re.compile(r"header\.from\s*=\s*([^\s;)]+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class DecodedMessage:
    """
    The bodies and a few headers of one decoded RFC 822 message.

    ``list_unsubscribe`` is data and never an instruction: nothing may act on it.
    ``authentication`` and ``sender_verified`` say what Gmail concluded about the
    sender; ``sender`` itself is only what the message claims.
    """

    html: str | None
    text: str | None
    sender: str
    date: str
    subject: str
    message_id: str
    list_unsubscribe: str
    warnings: tuple[str, ...] = field(default=())
    authentication: dict[str, str] = field(default_factory=dict)
    sender_verified: bool = False


def _header(message: Any, name: str) -> str:  # noqa: ANN401 -- an email Message
    try:
        value = str(message.get(name, "") or "")
    except ValueError, LookupError, TypeError, AttributeError:
        return ""
    return _CONTROL.sub(" ", value).strip()[:1000]


def _walk_limits(message: Any, depth: int, seen: list[int]) -> None:  # noqa: ANN401
    seen[0] += 1
    if seen[0] > MAX_MIME_PARTS or depth > MAX_MIME_DEPTH:
        raise refusal("message_too_complex")
    if message.is_multipart():
        for part in message.get_payload():
            _walk_limits(part, depth + 1, seen)


def _body(message: Any, kind: str, warnings: list[str]) -> str | None:  # noqa: ANN401
    try:
        part = message.get_body(preferencelist=(kind,))
        if part is None:
            return None
        content = part.get_content()
    except LookupError, ValueError, UnicodeError, KeyError, AttributeError:
        warnings.append(f"the {kind} body could not be decoded and was skipped")
        return None
    return content if isinstance(content, str) else None


def _date_text(raw: str, warnings: list[str]) -> str:
    if not raw:
        return ""
    try:
        return email.utils.parsedate_to_datetime(raw).isoformat()
    except TypeError, ValueError, IndexError:
        warnings.append("the Date header is not a valid date and was kept as written")
        return raw


def parse_authentication(value: str) -> dict[str, str]:
    """
    The dkim, spf and dmarc results in ONE Authentication-Results value, or ``{}``.

    Empty unless the value's authserv-id is exactly ``mx.google.com``. Takes the first
    result for each method, and the ``header.from`` domain DMARC checked.
    """
    clauses = value.split(";")
    served = clauses[0].strip().split()
    if not served or served[0].lower() != TRUSTED_AUTHSERV_ID:
        return {}
    found: dict[str, str] = {"authserv_id": TRUSTED_AUTHSERV_ID}
    for clause in clauses[1:]:
        matched = _METHOD.match(clause)
        if matched is None:
            continue
        method, result = matched.group(1).lower(), matched.group(2).lower()
        if method in found:
            continue
        found[method] = result
        if method == "dmarc":
            domain = _HEADER_FROM.search(clause)
            if domain is not None:
                found["dmarc_header_from"] = domain.group(1).lower().rstrip(".")
    return found


def _authentication(message: Any, sender: str) -> tuple[dict[str, str], bool]:  # noqa: ANN401
    """Gmail's verdict on the sender, from the topmost Authentication-Results only."""
    try:
        values = message.get_all("Authentication-Results") or []
        top = _CONTROL.sub(" ", str(values[0])) if values else ""
    except ValueError, LookupError, TypeError, AttributeError, IndexError:
        return {}, False
    result = parse_authentication(top[:4000])
    if not result:
        return {}, False
    domain = email.utils.parseaddr(sender)[1].rpartition("@")[2].lower().rstrip(".")
    verified = (
        result.get("dmarc") == "pass"
        and bool(domain)
        and result.get("dmarc_header_from") == domain
    )
    return result, verified


def _bound(raw: bytes, max_bytes: int) -> None:
    if not raw:
        raise refusal("message_empty")
    if len(raw) > max_bytes:
        raise refusal("message_too_large")
    split = min(
        (i for i in (raw.find(b"\r\n\r\n"), raw.find(b"\n\n")) if i >= 0),
        default=len(raw),
    )
    if split > MAX_HEADER_BYTES:
        raise refusal("message_too_complex")


def decode_raw_message(raw: bytes, *, max_bytes: int = RAW_MAX_BYTES) -> DecodedMessage:
    """
    Decode one raw RFC 822 message into its bodies and headers.

    Uses the standard-library ``email`` package with ``policy.default`` and
    ``get_body``: charset and transfer decoding only. No attachment is extracted, no
    link is followed, nothing is fetched. A message over ``max_bytes``, with over
    256 KiB of headers or 1000 headers, with more than 64 parts, or nested deeper
    than 8 levels, is refused, and so is anything the email package cannot read.
    """
    _bound(raw, max_bytes)
    try:
        return _decode(raw)
    except Exception as error:  # noqa: BLE001 -- every email-package failure refuses
        if getattr(error, "code", None) is not None:
            raise
        raise refusal("message_too_complex") from error


def _decode(raw: bytes) -> DecodedMessage:
    message = email.message_from_bytes(raw, policy=email.policy.default)
    if len(message.keys()) > MAX_HEADERS:
        raise refusal("message_too_complex")
    _walk_limits(message, 1, [0])
    warnings: list[str] = []
    html_body = _body(message, "html", warnings)
    text_body = _body(message, "plain", warnings)
    if html_body is None and text_body is None:
        warnings.append("the message has no text or HTML body")
    sender = _header(message, "From")
    authentication, verified = _authentication(message, sender)
    return DecodedMessage(
        html=html_body,
        text=text_body,
        sender=sender,
        date=_date_text(_header(message, "Date"), warnings),
        subject=_header(message, "Subject"),
        message_id=_header(message, "Message-ID"),
        list_unsubscribe=_header(message, "List-Unsubscribe"),
        warnings=tuple(warnings),
        authentication=authentication,
        sender_verified=verified,
    )
